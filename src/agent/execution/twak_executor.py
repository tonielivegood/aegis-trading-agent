"""Alternative execution backend via the Trust Wallet Agent Kit (TWAK) CLI.

This wraps `twak swap ... --chain bsc --json` as a drop-in alternative to the
PancakeSwap router path: same `.swap(token_in, token_out, amount)` signature and
the same safety rails (whitelist, distinct tokens, positive amount, slippage,
DRY_RUN never broadcasts). It exists so the agent can execute on-chain through
the contest's intended Trust Wallet stack.

Design notes / safety:
  - subprocess is invoked with an ARGUMENT LIST (never shell=True) so token
    values cannot inject shell commands.
  - the wallet password is passed via the TWAK_WALLET_PASSWORD env var, never on
    the command line (argv is visible to other processes).
  - tokens are passed as contract ADDRESSES (from the curated set) to avoid
    symbol ambiguity across chains.
  - JSON output is parsed defensively: the exact field names are not contractual
    across CLI versions, so the tx-hash extractor tries several. The exact schema
    must be confirmed against a live `twak swap --json` run before go-live.

Opt-in: the default execution backend remains PancakeSwap on the registered
wallet. See config `execution_backend`.
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
        # Password is read from the environment if not supplied; never logged.
        self._password = password if password is not None else os.getenv("TWAK_WALLET_PASSWORD", "")

    # --- command construction ---
    def _build_args(self, token_in: str, token_out: str, amount_human: float,
                    quote_only: bool) -> list[str]:
        addr_in = get_token(token_in).address   # KeyError if not whitelisted
        addr_out = get_token(token_out).address
        args = [
            _CLI, "swap", _fmt_amount(amount_human), addr_in, addr_out,
            "--chain", "bsc", "--json",
            "--slippage", _fmt_amount(self.slippage_bps / 100),  # bps -> percent
        ]
        if quote_only:
            args.append("--quote-only")
        return args

    # --- subprocess boundary ---
    def _run(self, args: list[str]) -> dict:
        env = dict(os.environ)
        if self._password:
            env["TWAK_WALLET_PASSWORD"] = self._password
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
