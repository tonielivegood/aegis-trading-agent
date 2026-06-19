"""Binance Wallet Web3 API — SAFE connectivity / quote layer only.

This module deliberately does the bare minimum:
  - reads the API key from the ENVIRONMENT only (via settings), never a file in
    the repo, never a hard-coded value;
  - NEVER signs a transaction and NEVER broadcasts one — there are no such
    functions here by design;
  - masks the key in every log/return value (e.g. ``abc123...xyz789``);
  - is best-effort: a failed check never raises into the trading loop.

Live execution stays on PancakeSwap (registered wallet) / TWAK. The Binance Web3
API is an OPTIONAL additional QUOTE source we may later use if it improves
routing/pricing/MEV — but only behind the same DRY_RUN and safety gates, and only
after the contest organizer confirms scoring rules. For now this is connectivity
proof + a masked-secret-handling reference, nothing more.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 15
# Binance convention; overridable if the Web3 API uses a different header.
_AUTH_HEADER = "X-MBX-APIKEY"
# Harmless, unauthenticated market endpoint for a reachability probe. The exact
# Web3-API path can be set via env once confirmed; this default just pings the
# public server time so the check is safe even before the path is known.
_DEFAULT_PING_PATH = "/api/v3/time"


def mask_secret(s: str | None) -> str:
    """Mask a secret for safe display: first6...last6, or *** / <absent>."""
    if not s:
        return "<absent>"
    if len(s) <= 12:
        return "***"
    return f"{s[:6]}...{s[-6:]}"


@dataclass
class ConnectivityResult:
    has_key: bool
    endpoint: str
    reachable: bool
    status: int | None
    detail: str


def _api_key() -> str:
    # Environment only — settings already loads from env; fall back to raw env.
    return getattr(settings, "binance_web3_api_key", "") or os.getenv("BINANCE_WEB3_API_KEY", "")


def _api_base() -> str:
    return (getattr(settings, "binance_web3_base_url", "") or
            os.getenv("BINANCE_WEB3_BASE_URL", "") or "https://api.binance.com")


def connectivity_check(ping_path: str | None = None) -> ConnectivityResult:
    """Validate the key is present and the API is reachable via a harmless GET.

    Never signs, never broadcasts, never logs the full key. Returns a structured
    result instead of raising, so callers can report status without risk.
    """
    path = ping_path or os.getenv("BINANCE_WEB3_PING_PATH", "") or _DEFAULT_PING_PATH
    endpoint = _api_base().rstrip("/") + path
    key = _api_key()

    if not key:
        log.warning("binance_web3_no_key", detail="BINANCE_WEB3_API_KEY not set")
        return ConnectivityResult(False, endpoint, False, None, "BINANCE_WEB3_API_KEY not set")

    # Only the MASKED key is ever logged.
    log.info("binance_web3_connectivity_check", key=mask_secret(key), endpoint=endpoint)
    try:
        resp = requests.get(endpoint, headers={_AUTH_HEADER: key}, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        return ConnectivityResult(True, endpoint, False, None, f"unreachable: {type(e).__name__}")

    ok = resp.status_code == 200
    return ConnectivityResult(True, endpoint, ok, resp.status_code,
                              "ok" if ok else f"HTTP {resp.status_code}")
