"""PancakeSwap V2 execution with built-in safety rails.

Safety invariants enforced here (see execution threat model):
  - both tokens must be in the curated tradable set (get_token raises otherwise)
  - amount_in must be positive
  - min_out is always derived from a live quote minus slippage, never 0
  - token approval is for the EXACT amount, never unlimited
  - the swap deadline is short
  - DRY_RUN hard-gates broadcasting — no transaction is ever sent in dry-run
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from web3 import Web3

from ..config import settings
from ..data.token_list import get_token
from ..monitor.logger import get_logger
from . import tx_builder

log = get_logger(__name__)

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _to_hex(h) -> str:
    if isinstance(h, str):
        return h
    hx = h.hex()
    return hx if hx.startswith("0x") else "0x" + hx


@dataclass
class Quote:
    token_in: str
    token_out: str
    amount_in_wei: int
    expected_out_wei: int
    min_out_wei: int
    path: list[str]


@dataclass
class SwapResult:
    token_in: str
    token_out: str
    amount_in_wei: int
    expected_out_wei: int
    min_out_wei: int
    simulated: bool
    tx_hash: str | None = None
    received_out_wei: int = 0


class PancakeSwap:
    def __init__(self, w3=None, account=None, dry_run: bool | None = None, slippage_bps: int | None = None) -> None:
        if w3 is None:
            from ..data.rpc import get_web3
            w3 = get_web3()
        self.w3 = w3
        self.account = account
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.slippage_bps = settings.slippage_bps if slippage_bps is None else slippage_bps
        self.router_addr = Web3.to_checksum_address(settings.pancake_router)
        self.router = self.w3.eth.contract(address=self.router_addr, abi=ROUTER_ABI)

    # --- routing ---
    def build_path(self, token_in: str, token_out: str) -> list[str]:
        a = get_token(token_in).address
        b = get_token(token_out).address
        wbnb = get_token("WBNB").address
        if a == wbnb or b == wbnb:
            return [a, b]
        return [a, wbnb, b]

    def get_amounts_out(self, amount_in_wei: int, path: list[str]) -> list[int]:
        return self.router.functions.getAmountsOut(amount_in_wei, path).call()

    # --- quoting ---
    def quote(self, token_in: str, token_out: str, amount_in_human: float) -> Quote:
        tin = get_token(token_in)   # KeyError if not whitelisted
        get_token(token_out)        # KeyError if not whitelisted
        if token_in.upper() == token_out.upper():
            raise ValueError("token_in and token_out must differ")
        if not isinstance(amount_in_human, (int, float)) or amount_in_human <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in_human!r}")

        path = self.build_path(token_in, token_out)
        amount_in_wei = tx_builder.to_wei_amount(amount_in_human, tin.decimals)
        amounts = self.get_amounts_out(amount_in_wei, path)
        expected_out_wei = amounts[-1]
        min_out_wei = tx_builder.apply_slippage(expected_out_wei, self.slippage_bps)
        if min_out_wei <= 0:
            raise ValueError("computed min_out is 0 — refusing unprotected swap")
        return Quote(token_in, token_out, amount_in_wei, expected_out_wei, min_out_wei, path)

    # --- execution ---
    def _clamp_to_balance(self, token_in: str, amount_in_human: float) -> float:
        """Never request more than the wallet holds. Reading balances as floats
        and converting back to wei can overshoot the true balance by a few wei
        and revert the swap (notably when selling an entire position, e.g. during
        derisk). If the request meets or exceeds the balance, sell 99.99% of it."""
        tok = get_token(token_in)
        erc20 = self.w3.eth.contract(address=tok.address, abi=ERC20_ABI)
        raw = erc20.functions.balanceOf(self.account.address).call()
        available = raw / (10 ** tok.decimals)
        if amount_in_human >= available:
            return available * 0.9999
        return amount_in_human

    def swap(self, token_in: str, token_out: str, amount_in_human: float) -> SwapResult:
        if not self.dry_run and self.account is not None:
            amount_in_human = self._clamp_to_balance(token_in, amount_in_human)
        q = self.quote(token_in, token_out, amount_in_human)

        if self.dry_run:
            log.info("dry_run_swap", token_in=token_in, token_out=token_out,
                     amount_in_wei=q.amount_in_wei, min_out_wei=q.min_out_wei)
            return SwapResult(q.token_in, q.token_out, q.amount_in_wei,
                              q.expected_out_wei, q.min_out_wei, simulated=True)

        if self.account is None:
            raise RuntimeError("no signing account configured for a live swap")

        out_tok = get_token(q.token_out)
        out_erc20 = self.w3.eth.contract(address=out_tok.address, abi=ERC20_ABI)
        bal_before = out_erc20.functions.balanceOf(self.account.address).call()
        self._approve(token_in, q.amount_in_wei)
        tx = self._build_swap_tx(q)
        tx_hash = self._sign_and_send(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        hash_str = _to_hex(tx_hash)
        if getattr(receipt, "status", 1) != 1:   # mined but reverted → treat as failure
            log.warning("swap_reverted", token_in=token_in, token_out=token_out, tx_hash=hash_str)
            raise RuntimeError(f"PancakeSwap swap reverted on-chain (status 0): {hash_str}")
        received = out_erc20.functions.balanceOf(self.account.address).call() - bal_before
        log.info("swap_sent", token_in=token_in, token_out=token_out, tx_hash=hash_str)
        return SwapResult(q.token_in, q.token_out, q.amount_in_wei,
                          q.expected_out_wei, q.min_out_wei, simulated=False,
                          tx_hash=hash_str, received_out_wei=received)

    def _approve(self, token_in: str, amount_wei: int) -> None:
        """Approve the router for EXACTLY amount_wei (never unlimited).

        Waits for the approval to be mined before returning, so the subsequent
        swap cannot revert for missing allowance or collide on the nonce.
        """
        tok = get_token(token_in)
        erc20 = self.w3.eth.contract(address=tok.address, abi=ERC20_ABI)
        current = erc20.functions.allowance(self.account.address, self.router_addr).call()
        if current >= amount_wei:
            return
        tx = erc20.functions.approve(self.router_addr, amount_wei).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "gas": 80_000,
            "gasPrice": int(self.w3.eth.gas_price * 1.2),
            "chainId": settings.bsc_chain_id,
        })
        tx_hash = self._sign_and_send(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if getattr(receipt, "status", 1) != 1:
            raise RuntimeError(f"token approval reverted for {token_in}")

    def _build_swap_tx(self, q: Quote) -> dict:
        deadline = tx_builder.swap_deadline(int(time.time()), seconds=120)
        return self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            q.amount_in_wei, q.min_out_wei, q.path, self.account.address, deadline
        ).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "gas": 300_000,
            "gasPrice": int(self.w3.eth.gas_price * 1.2),
            "chainId": settings.bsc_chain_id,
        })

    def _sign_and_send(self, tx: dict):
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return self.w3.eth.send_raw_transaction(raw)
