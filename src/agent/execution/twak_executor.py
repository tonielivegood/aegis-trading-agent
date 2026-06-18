"""Alternative execution backend via the Trust Wallet Agent Kit (TWAK) CLI v0.19.1+.

Wraps `twak swap <amount> <fromSymbol> <toSymbol> --chain bsc --json` as a
drop-in alternative to the PancakeSwap router path: same `.swap()` signature
and the same safety rails (whitelist, distinct tokens, positive amount,
slippage, DRY_RUN never broadcasts). Exists so the agent can demonstrate the
full Trust Wallet stack for the BNB Hack special-prize "technical execution"
criterion.

CLI schema (confirmed v0.19.1):
  twak swap <amount> <fromSymbol> <toSymbol> --chain bsc --json
            --slippage <pct_float>
            [--quote-only]                   # dry-run: quote only, no broadcast
            [--password <pw>]                # required for live execution
  Auth: TWAK_ACCESS_ID + TWAK_HMAC_SECRET env vars (set in .env)

Safety:
  - subprocess called with an ARGUMENT LIST (never shell=True).
  - wallet password is passed via --password flag only for live swaps;
    dry-run (--quote-only) never touches the password.
  - TWAK auth credentials passed via environment, never logged.
  - tokens are passed as SYMBOLS (TWAK has its own BSC registry); internal
    symbols are mapped via _TO_TWAK before being passed to the CLI.
  - JSON output parsed defensively: tx-hash extractor tries several field
    names to tolerate minor schema changes across CLI versions.

Opt-in: default backend remains PancakeSwap on the registered contest wallet.
See config `execution_backend`.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from ..config import settings
from ..data.token_list import get_token
from ..monitor.logger import get_logger

log = get_logger(__name__)

# Candidate JSON keys for the broadcast transaction hash (schema-tolerant).
_TX_HASH_KEYS = ("txHash", "tx_hash", "hash", "transactionHash")
_CLI = "twak"
_TIMEOUT_S = 180

# TWAK BSC registry uses "BNB" for wrapped BNB (WBNB in our internal naming).
_TO_TWAK: dict[str, str] = {"WBNB": "BNB", "BTCB": "BTC"}


class TwakError(RuntimeError):
    """Raised when the TWAK CLI fails or returns unparseable output."""


@dataclass
class TwakSwapResult:
    token_in: str
    token_out: str
    simulated: bool
    tx_hash: str | None = None
    raw: dict | None = None


class TwakExecutor:
    def __init__(self, dry_run: bool | None = None, password: str | None = None) -> None:
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.slippage_bps = settings.slippage_bps
        self._password = password if password is not None else os.getenv("TWAK_WALLET_PASSWORD", "")

    # --- command construction ---
    def _build_args(self, token_in: str, token_out: str, amount_human: float,
                    quote_only: bool) -> list[str]:
        get_token(token_in)   # KeyError if not whitelisted
        get_token(token_out)
        sym_in = _TO_TWAK.get(token_in.upper(), token_in.upper())
        sym_out = _TO_TWAK.get(token_out.upper(), token_out.upper())
        args = [
            _CLI, "swap", _fmt_amount(amount_human), sym_in, sym_out,
            "--chain", "bsc", "--json",
            "--slippage", _fmt_amount(self.slippage_bps / 100),  # bps -> percent
        ]
        if quote_only:
            args.append("--quote-only")
        elif self._password:
            # Password is required for live execution; omitted for quote-only.
            args.extend(["--password", self._password])
        return args

    # --- subprocess boundary ---
    def _run(self, args: list[str]) -> dict:
        env = dict(os.environ)
        # TWAK auth credentials injected via environment (Access ID + HMAC Secret).
        access_id = getattr(settings, "twak_access_id", "") or os.getenv("TWAK_ACCESS_ID", "")
        hmac_secret = getattr(settings, "twak_hmac_secret", "") or os.getenv("TWAK_HMAC_SECRET", "")
        if access_id:
            env["TWAK_ACCESS_ID"] = access_id
        if hmac_secret:
            env["TWAK_HMAC_SECRET"] = hmac_secret
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=_TIMEOUT_S,
                env=env, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise TwakError(f"twak invocation failed: {type(e).__name__}") from e
        if proc.returncode != 0:
            raise TwakError(f"twak exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            return json.loads(proc.stdout)
        except (ValueError, TypeError) as e:
            raise TwakError("twak returned non-JSON output") from e

    @staticmethod
    def _extract_tx_hash(payload: dict) -> str | None:
        for k in _TX_HASH_KEYS:
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        return None

    # --- execution ---
    def swap(self, token_in: str, token_out: str, amount_in_human: float) -> TwakSwapResult:
        get_token(token_in)   # KeyError if not whitelisted
        get_token(token_out)
        if token_in.upper() == token_out.upper():
            raise ValueError("token_in and token_out must differ")
        if not isinstance(amount_in_human, (int, float)) or amount_in_human <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in_human!r}")

        args = self._build_args(token_in, token_out, amount_in_human, quote_only=self.dry_run)
        payload = self._run(args)

        if self.dry_run:
            log.info("twak_dry_run_quote", token_in=token_in, token_out=token_out)
            return TwakSwapResult(token_in, token_out, simulated=True, raw=payload)

        tx_hash = self._extract_tx_hash(payload)
        log.info("twak_swap_sent", token_in=token_in, token_out=token_out, tx_hash=tx_hash)
        return TwakSwapResult(token_in, token_out, simulated=False, tx_hash=tx_hash, raw=payload)


def _fmt_amount(x: float) -> str:
    """Compact decimal string (no scientific notation, no trailing zeros)."""
    return format(float(x), "f").rstrip("0").rstrip(".") or "0"
