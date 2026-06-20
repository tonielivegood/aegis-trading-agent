"""1inch Classic Swap (v6) aggregator execution — same interface as PancakeSwap.

A second aggregator backend alongside OpenOcean (gold-standard routing). Self-
custody preserved: 1inch returns ready-to-sign calldata (tx.to/data/value); WE
sign locally and broadcast. DRY_RUN hard-gates broadcasting.

Needs a (free) API key from portal.1inch.dev → settings.oneinch_api_key. Dormant
until that is set. v6 endpoints (auth: Authorization: Bearer <key>; amount in WEI):
  GET {base}/quote?src&dst&amount
  GET {base}/swap?src&dst&amount&from&slippage&disableEstimate=true -> {dstAmount, tx:{to,data,value}}
AggregationRouterV6 (approve spender): 0x111111125421cA6dc452d289314280a0f8842A65
"""
from __future__ import annotations

import requests
from web3 import Web3

from ..config import settings
from ..data import price_feed
from ..data.token_list import get_token
from ..monitor.logger import get_logger
from .pancakeswap import ERC20_ABI, SwapResult, _to_hex
from .tx_builder import to_wei_amount

log = get_logger(__name__)

_BASE = "https://api.1inch.dev/swap/v6.0/56"          # 56 = BSC; configurable
_ROUTER_V6 = "0x111111125421cA6dc452d289314280a0f8842A65"
_TIMEOUT_S = 12


class OneInch:
    def __init__(self, w3=None, account=None, dry_run: bool | None = None,
                 api_key: str | None = None, base_url: str | None = None) -> None:
        if w3 is None:
            from ..data.rpc import get_web3
            w3 = get_web3()
        self.w3 = w3
        self.account = account
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.slippage_bps = settings.slippage_bps
        self.api_key = api_key if api_key is not None else getattr(settings, "oneinch_api_key", "")
        self.base = (base_url or getattr(settings, "oneinch_base_url", "") or _BASE).rstrip("/")

    def _get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("oneinch_api_key not set")
        r = requests.get(f"{self.base}/{path}", params=params,
                         headers={"Authorization": f"Bearer {self.api_key}"}, timeout=_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def _quote_out_wei(self, token_in: str, token_out: str, amount_in_human: float) -> int:
        tin, tout = get_token(token_in), get_token(token_out)
        amount_wei = to_wei_amount(amount_in_human, tin.decimals)
        data = self._get("quote", {"src": tin.address, "dst": tout.address, "amount": amount_wei})
        return int(data.get("dstAmount", 0) or 0)

    def price_impact(self, token_in: str, token_out: str, amount_in_human: float) -> float | None:
        """Slippage estimate (fraction) for the gate: compare the routed output value
        to the fair value implied by on-chain spot prices. 1inch gives no impact field."""
        try:
            out_wei = self._quote_out_wei(token_in, token_out, amount_in_human)
            tout = get_token(token_out)
            out_human = out_wei / (10 ** tout.decimals)
            p_in = price_feed.onchain_price_usd(token_in) or 0.0
            p_out = price_feed.onchain_price_usd(token_out) or 0.0
            if p_in <= 0 or p_out <= 0 or out_human <= 0:
                return None
            fair_out = (amount_in_human * p_in) / p_out
            return max(0.0, (fair_out - out_human) / fair_out)
        except Exception as e:  # noqa: BLE001
            log.debug("oneinch_quote_failed", token_out=token_out, error=type(e).__name__)
            return None

    def _clamp_to_balance(self, token_in: str, amount_in_human: float) -> float:
        tok = get_token(token_in)
        erc20 = self.w3.eth.contract(address=tok.address, abi=ERC20_ABI)
        available = erc20.functions.balanceOf(self.account.address).call() / (10 ** tok.decimals)
        return available * 0.9999 if amount_in_human >= available else amount_in_human

    def swap(self, token_in: str, token_out: str, amount_in_human: float) -> SwapResult:
        tin = get_token(token_in)
        get_token(token_out)
        if token_in.upper() == token_out.upper():
            raise ValueError("token_in and token_out must differ")
        if not isinstance(amount_in_human, (int, float)) or amount_in_human <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in_human!r}")

        if not self.dry_run and self.account is not None:
            amount_in_human = self._clamp_to_balance(token_in, amount_in_human)
        amount_wei = to_wei_amount(amount_in_human, tin.decimals)

        if self.dry_run:
            out_wei = self._quote_out_wei(token_in, token_out, amount_in_human)
            log.info("dry_run_swap", backend="1inch", token_in=token_in, token_out=token_out,
                     amount_in_wei=amount_wei, expected_out_wei=out_wei)
            return SwapResult(token_in, token_out, amount_wei, out_wei, 0, simulated=True)

        if self.account is None:
            raise RuntimeError("no signing account configured for a live swap")

        body = self._get("swap", {
            "src": tin.address, "dst": get_token(token_out).address, "amount": amount_wei,
            "from": self.account.address, "slippage": self.slippage_bps / 100.0,
            "disableEstimate": "true"})
        tx_api = body["tx"]
        out_wei = int(body.get("dstAmount", 0) or 0)
        spender = Web3.to_checksum_address(tx_api["to"])
        self._approve(token_in, amount_wei, spender)
        tx = {
            "from": self.account.address, "to": spender, "data": tx_api["data"],
            "value": int(tx_api.get("value", 0) or 0),
            "gas": int(int(tx_api.get("gas", 0) or 0) * 1.25) or 500_000,
            "gasPrice": int(self.w3.eth.gas_price * 1.2),
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": settings.bsc_chain_id,
        }
        tx_hash = self._sign_and_send(tx)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        hash_str = _to_hex(tx_hash)
        log.info("swap_sent", backend="1inch", token_in=token_in, token_out=token_out, tx_hash=hash_str)
        return SwapResult(token_in, token_out, amount_wei, out_wei, 0, simulated=False, tx_hash=hash_str)

    def _approve(self, token_in: str, amount_wei: int, spender: str) -> None:
        tok = get_token(token_in)
        erc20 = self.w3.eth.contract(address=tok.address, abi=ERC20_ABI)
        if erc20.functions.allowance(self.account.address, spender).call() >= amount_wei:
            return
        tx = erc20.functions.approve(spender, amount_wei).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "gas": 80_000, "gasPrice": int(self.w3.eth.gas_price * 1.2),
            "chainId": settings.bsc_chain_id})
        self.w3.eth.wait_for_transaction_receipt(self._sign_and_send(tx), timeout=180)

    def _sign_and_send(self, tx: dict):
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return self.w3.eth.send_raw_transaction(raw)
