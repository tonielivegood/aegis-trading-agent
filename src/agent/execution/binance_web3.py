"""Binance Wallet Web3 API — SAFE connectivity / quote layer only.

This module deliberately does the bare minimum:
  - reads the API key/secret from the ENVIRONMENT only (via settings), never a
    file in the repo, never a hard-coded value;
  - NEVER signs a blockchain TRANSACTION and NEVER broadcasts one — there are
    no such functions here by design (HMAC-signing a read-only HTTP request for
    API auth, below, is a different thing and does not touch the chain);
  - masks the key/secret in every log/return value (e.g. ``abc123...xyz789``);
  - is best-effort: a failed check never raises into the trading loop.

Live execution stays on PancakeSwap (registered wallet) / TWAK. The Binance Web3
API is an OPTIONAL additional QUOTE source we may later use if it improves
routing/pricing/MEV. For now this is connectivity proof + a region/compliance
probe (`check_region`) — see docs/superpowers/specs/2026-06-27-binance-w3w-venue-design.md
for the full design and why: Binance blocks some IPs/regions (compliance code
40304) independent of whether the key/secret are valid.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

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

# --- W3W (Wallet Web3) dex API — confirmed 27/6 against the live host. Fixed
# host/prefix, NOT settings.binance_web3_base_url (that field defaults to the
# unrelated Binance spot host and is used by the legacy connectivity_check above).
_W3W_HOST = "https://web3.binance.com"
_W3W_PATH_PREFIX = "/build"
_W3W_MARKET_PRICE_PATH = f"{_W3W_PATH_PREFIX}/api/v1/dex/market/price"
_W3W_PRICE_INFO_PATH = f"{_W3W_PATH_PREFIX}/api/v1/dex/market/price-info"
_W3W_HOT_TOKEN_PATH = f"{_W3W_PATH_PREFIX}/api/v1/dex/market/token/hot-token"
_W3W_QUOTE_PATH = f"{_W3W_PATH_PREFIX}/api/v1/dex/aggregator/quote"
_BSC_CHAIN_ID = "56"
_PRICE_INFO_BATCH_MAX = 100      # server-enforced batch cap for price/price-info

# hot-token rankBy / rankingTimeFrame enums (confirmed from the OpenAPI schema).
RANK_BY_PRICE_CHANGE = 2
RANK_BY_VOLUME = 4
TIMEFRAME_5MIN = 2
# USDT on BSC — deep, always-listed, safe default probe token (no meme-specific
# coverage assumptions needed just to check IP/region access).
_USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"

# Binance OCResult codes worth explaining to a human running the region check.
_OC_CODE_DETAIL = {
    0: "OK — this IP can reach Binance W3W",
    40101: "auth error — API key invalid",
    40102: "auth error — signature mismatch (check the signing implementation)",
    40103: "auth error — timestamp outside the allowed window (check server clock)",
    40104: "auth error — key lacks permission for this endpoint",
    40301: "BLOCKED — sanctioned/region restriction",
    40302: "BLOCKED — VPN/proxy detected",
    40303: "BLOCKED — anomalous IP",
    40304: "BLOCKED — compliance restriction (this is the block seen from the old VPS)",
    42900: "rate limited — back off and retry",
}


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


# --------------------------- W3W region/compliance check ---------------------------

@dataclass
class RegionCheckResult:
    has_credentials: bool
    endpoint: str
    ok: bool                # True only on Binance's own code == 0 (real success)
    api_code: int | None    # Binance's OCResult code, e.g. 0, 40304, 42900
    detail: str


def _api_secret() -> str:
    return getattr(settings, "binance_web3_api_secret", "") or os.getenv("BINANCE_WEB3_API_SECRET", "")


def _iso_timestamp_ms(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _request_signature(secret: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    """HMAC-SHA256(timestamp + METHOD + requestPath + body), base64-encoded.

    This authenticates a read-only HTTP request to Binance's API — it does not
    sign a blockchain transaction and never touches the chain."""
    payload = f"{timestamp}{method}{request_path}{body}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def check_region(contract_address: str = _USDT_BSC, *, chain_id: str = _BSC_CHAIN_ID) -> RegionCheckResult:
    """Probe whether THIS machine's IP can reach Binance W3W at all.

    Signs one read-only POST to the market/price endpoint for a single deep,
    always-listed token (USDT/BSC by default) — no swap, no signing of any
    transaction, no broadcast. Run this from a candidate VPS/region BEFORE
    migrating anything else: Binance's compliance block (code 40304, seen from
    the old Hostinger-US VPS) is about the calling IP, not the key/secret."""
    endpoint = _W3W_HOST + _W3W_MARKET_PRICE_PATH
    key, secret = _api_key(), _api_secret()
    if not key or not secret:
        return RegionCheckResult(False, endpoint, False, None,
                                 "BINANCE_WEB3_API_KEY / _SECRET not set")

    body = json.dumps([{"binanceChainId": chain_id, "tokenContractAddress": contract_address}],
                      separators=(",", ":"))
    timestamp = _iso_timestamp_ms()
    signature = _request_signature(secret, timestamp, "POST", _W3W_MARKET_PRICE_PATH, body)
    headers = {
        "X-OC-APIKEY": key,
        "X-OC-TIMESTAMP": timestamp,
        "X-OC-SIGN": signature,
        "Content-Type": "application/json",
    }
    log.info("binance_w3w_region_check", key=mask_secret(key), endpoint=endpoint)
    try:
        resp = requests.post(endpoint, headers=headers, data=body, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        return RegionCheckResult(True, endpoint, False, None, f"unreachable: {type(e).__name__}")

    try:
        payload = resp.json()
    except ValueError:
        return RegionCheckResult(True, endpoint, False, None, f"HTTP {resp.status_code}, non-JSON body")

    code = payload.get("code")
    detail = _OC_CODE_DETAIL.get(code, payload.get("msg") or f"unrecognized code {code}")
    return RegionCheckResult(True, endpoint, code == 0, code, detail)


# --------------------------- Market API: batch price/volume/liquidity ---------------------------

def price_info(contracts: list[str], *, chain_id: str = _BSC_CHAIN_ID) -> dict[str, dict]:
    """Batch price + 5m/1h/4h/24h volume + liquidity + holders for up to N contracts
    (auto-chunked at the server's 100-per-call limit). Returns {contract_lower: entry};
    a chunk that fails or errors is simply absent from the result — never raises into
    the trading loop. This is the live per-tick universe pricing source (replaces
    per-symbol CMC calls and the separate kline-derived 5m volume calc)."""
    key, secret = _api_key(), _api_secret()
    if not key or not secret or not contracts:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(contracts), _PRICE_INFO_BATCH_MAX):
        chunk = contracts[i:i + _PRICE_INFO_BATCH_MAX]
        body = json.dumps([{"binanceChainId": chain_id, "tokenContractAddress": c} for c in chunk],
                          separators=(",", ":"))
        timestamp = _iso_timestamp_ms()
        signature = _request_signature(secret, timestamp, "POST", _W3W_PRICE_INFO_PATH, body)
        headers = {
            "X-OC-APIKEY": key, "X-OC-TIMESTAMP": timestamp, "X-OC-SIGN": signature,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(_W3W_HOST + _W3W_PRICE_INFO_PATH, headers=headers, data=body,
                                 timeout=_TIMEOUT_S)
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("binance_w3w_price_info_failed", error=type(e).__name__)
            continue
        if payload.get("code") != 0:
            log.warning("binance_w3w_price_info_error", code=payload.get("code"), msg=payload.get("msg"))
            continue
        for entry in payload.get("data") or []:
            addr = (entry.get("tokenContractAddress") or "").lower()
            if addr:
                out[addr] = entry
    return out


def _signed_get(path: str, params: dict[str, str]) -> dict:
    """Sign + send a GET with query params. The signed pre-hash string and the actual
    request MUST use the identical query string — built once here, never re-encoded
    by `requests`, so a param can't silently reorder between sign time and send time."""
    key, secret = _api_key(), _api_secret()
    if not key or not secret:
        return {}
    query = urlencode(sorted(params.items()))
    full_path = f"{path}?{query}" if query else path
    timestamp = _iso_timestamp_ms()
    signature = _request_signature(secret, timestamp, "GET", full_path, "")
    headers = {"X-OC-APIKEY": key, "X-OC-TIMESTAMP": timestamp, "X-OC-SIGN": signature}
    try:
        resp = requests.get(_W3W_HOST + full_path, headers=headers, timeout=_TIMEOUT_S)
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("binance_w3w_get_failed", path=path, error=type(e).__name__)
        return {}


# --------------------------- Market API: server-side filtered discovery ---------------------------

def hot_token(*, chain_id: str = _BSC_CHAIN_ID, rank_by: int = RANK_BY_PRICE_CHANGE,
             ranking_time_frame: int = TIMEFRAME_5MIN,
             price_change_percent_min: float | None = None,
             volume_min: float | None = None, liquidity_min: float | None = None,
             exclude_mint: bool = True, exclude_freeze: bool = True,
             top10_holding_percent_max: float | None = None,
             sniper_holding_percent_max: float | None = None,
             bundler_holding_percent_max: float | None = None,
             dev_holding_percent_max: float | None = None,
             size: int = 100) -> list[dict]:
    """Server-side-filtered breakout discovery (Option B): Binance ranks + filters
    tokens for us — wash-trading/dev-wash-trading/insider-wash-trading always hidden,
    mint/freeze-capable tokens excluded by default, holder-concentration caps optional.
    Replaces client-side scanning of a self-maintained universe file. [] on any failure."""
    params: dict[str, str] = {
        "binanceChainId": chain_id,
        "rankBy": str(rank_by),
        "rankingTimeFrame": str(ranking_time_frame),
        "isMint": str(exclude_mint).lower(),
        "isFreeze": str(exclude_freeze).lower(),
        "isHideWashTradingTokens": "true",
        "isHideDevWashTradingTokens": "true",
        "isHideInternalWashTradingTokens": "true",
        "size": str(size),
    }
    optional = {
        "priceChangePercentMin": price_change_percent_min,
        "volumeMin": volume_min,
        "liquidityMin": liquidity_min,
        "top10HoldingPercentMax": top10_holding_percent_max,
        "sniperHoldingPercentMax": sniper_holding_percent_max,
        "bundlerHoldingPercentMax": bundler_holding_percent_max,
        "devHoldingPercentMax": dev_holding_percent_max,
    }
    for k, v in optional.items():
        if v is not None:
            params[k] = str(v)
    payload = _signed_get(_W3W_HOT_TOKEN_PATH, params)
    if payload.get("code") != 0:
        return []
    return (payload.get("data") or {}).get("items") or []


# --------------------------- Trading API: just-in-time safety quote ---------------------------

def quote(from_token: str, to_token: str, amount_wei: str, *, chain_id: str = _BSC_CHAIN_ID,
         wallet_address: str = "") -> list[dict]:
    """Aggregated swap quote — the just-in-time `isHoneyPot` / `taxRate` /
    `priceImpactPercent` safety check right before an entry decision.

    The returned `quoteId` has a 30-SECOND TTL: call this fresh at decision time,
    never cache or reuse a quote across ticks. Routes are sorted by `toTokenAmount`
    descending (the first entry, or the one with `isBest=True`, is the best fill)."""
    params = {
        "binanceChainId": chain_id, "fromTokenAddress": from_token,
        "toTokenAddress": to_token, "amount": amount_wei,
    }
    if wallet_address:
        params["userWalletAddress"] = wallet_address
    payload = _signed_get(_W3W_QUOTE_PATH, params)
    if payload.get("code") != 0:
        return []
    return payload.get("data") or []
