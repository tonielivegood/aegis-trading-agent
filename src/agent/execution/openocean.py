"""OpenOcean DEX-aggregator execution — same interface as PancakeSwap.

Why: pricing/liquidity on BSC is fragmented across many DEXs (Pancake V2/V3,
Biswap, THENA...). Quoting Pancake V2 ONLY made most liquid tokens look untradable
(UNI/DOT/AAVE showed 30–93% "slippage" on V2 while an aggregator fills them at
<0.5%). OpenOcean routes across all of them, so this backend unlocks the real
tradable universe at far lower slippage.

Self-custody is preserved: OpenOcean returns ready-to-sign calldata ({to,data,
value}); WE sign it locally with our own key and broadcast it — OpenOcean never
holds funds or keys. DRY_RUN hard-gates broadcasting, exactly like PancakeSwap.

Endpoints (v3, amount in HUMAN token units):
  GET /v3/bsc/quote?inTokenAddress&outTokenAddress&amount&gasPrice
  GET /v3/bsc/swap_quote?...&slippage&account={wallet}  -> {to,data,value,gasPrice,estimatedGas,outAmount}
"""
from __future__ import annotations

import requests
from web3 import Web3

from ..config import settings
from ..data.token_list import get_token
from ..monitor.logger import get_logger
from .pancakeswap import ERC20_ABI, SwapResult, _to_hex
from .tx_builder import to_wei_amount

log = get_logger(__name__)

_BASE = "https://open-api.openocean.finance/v3/bsc"
_TIMEOUT_S = 12


class OpenOcean:
    def __init__(self, w3=None, account=None, dry_run: bool | None = None,
                 base_url: str | None = None) -> None:
        if w3 is None:
            from ..data.rpc import get_web3
            w3 = get_web3()
        self.w3 = w3
        self.account = account
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.slippage_bps = settings.slippage_bps
        self.base = (base_url or _BASE).rstrip("/")

    # --- aggregator HTTP (read-only; never sends funds) ---
    def _get(self, path: str, params: dict) -> dict:
        r = requests.get(f"{self.base}/{path}", params=params, timeout=_TIMEOUT_S)
        r.raise_for_status()
        body = r.json()
        if str(body.get("code")) not in ("200", "0", "None") and body.get("data") is None:
            raise RuntimeError(f"openocean {path} error: {body.get('msg') or body.get('error') or body}")
        return body.get("data") or {}

    def _gas_gwei(self) -> str:
        try:
            return str(max(1, int(self.w3.eth.gas_price / 1e9)))
        except Exception:  # noqa: BLE001
            return "1"

    def quote(self, token_in: str, token_out: str, amount_in_human: float) -> dict:
        """Aggregator quote (best route across all BSC DEXs). Returns the raw data
        dict incl. outAmount and price_impact — used for the gate and dry-run preview."""
        tin, tout = get_token(token_in), get_token(token_out)
        if amount_in_human <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in_human!r}")
        return self._get("quote", {
            "inTokenAddress": tin.address, "outTokenAddress": tout.address,
            "amount": amount_in_human, "gasPrice": self._gas_gwei()})

    def price_impact(self, token_in: str, token_out: str, amount_in_human: float) -> float | None:
        """Slippage estimate (fraction, e.g. 0.004 = 0.4%) from the aggregator, or
        None if unavailable. Used by MarketFeed as the liquidity gate."""
        try:
            pi = self.quote(token_in, token_out, amount_in_human).get("price_impact")
            return float(str(pi).replace("%", "")) / 100.0 if pi is not None else None
        except Exception as e:  # noqa: BLE001
            log.debug("openocean_quote_failed", token_out=token_out, error=type(e).__name__)
            return None

    # --- execution ---
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
        amount_in_wei = to_wei_amount(amount_in_human, tin.decimals)

        if self.dry_run:
            data = self.quote(token_in, token_out, amount_in_human)
            out_wei = int(data.get("outAmount", 0) or 0)
            log.info("dry_run_swap", backend="openocean", token_in=token_in,
                     token_out=token_out, amount_in_wei=amount_in_wei, expected_out_wei=out_wei)
            return SwapResult(token_in, token_out, amount_in_wei, out_wei, 0, simulated=True)

        if self.account is None:
            raise RuntimeError("no signing account configured for a live swap")

        sq = self._get("swap_quote", {
            "inTokenAddress": tin.address, "outTokenAddress": get_token(token_out).address,
            "amount": amount_in_human, "gasPrice": self._gas_gwei(),
            "slippage": self.slippage_bps / 100.0,            # OpenOcean wants PERCENT
            "account": self.account.address})
        spender = Web3.to_checksum_address(sq["to"])
        out_wei = int(sq.get("outAmount", 0) or 0)
        self._approve(token_in, amount_in_wei, spender)
        tx = {
            "from": self.account.address,
            "to": spender,
            "data": sq["data"],
            "value": int(sq.get("value", 0) or 0),
            "gas": int(int(sq.get("estimatedGas", 0) or 0) * 1.25) or 500_000,
            "gasPrice": int(self.w3.eth.gas_price * 1.2),
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": settings.bsc_chain_id,
        }
        tx_hash = self._sign_and_send(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        hash_str = _to_hex(tx_hash)
        if getattr(receipt, "status", 1) != 1:   # mined but reverted → treat as failure
            log.warning("swap_reverted", backend="openocean", token_in=token_in,
                        token_out=token_out, tx_hash=hash_str)
            raise RuntimeError(f"OpenOcean swap reverted on-chain (status 0): {hash_str}")
        log.info("swap_sent", backend="openocean", token_in=token_in,
                 token_out=token_out, tx_hash=hash_str)
        return SwapResult(token_in, token_out, amount_in_wei, out_wei, 0,
                          simulated=False, tx_hash=hash_str)

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
        receipt = self.w3.eth.wait_for_transaction_receipt(self._sign_and_send(tx), timeout=180)
        if getattr(receipt, "status", 1) != 1:
            raise RuntimeError(f"token approval reverted for {token_in}")

    def _sign_and_send(self, tx: dict):
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return self.w3.eth.send_raw_transaction(raw)
