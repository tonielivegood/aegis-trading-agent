"""Launch collector — records newly-created BSC pools, never trades.

WHY THIS EXISTS: we tried to derive entry/safety rules by researching past
winners and it could not be done. Of 16 confirmed 4x+ winners, 14 are now at $0
liquidity; the LP-lock hypothesis was refuted by the data (the dead ones averaged
81.8% locked, the survivors 0%); and the socials hypothesis is untestable in
hindsight because DexScreener erases a token's entire `info` block once its pairs
die. The data needed to answer "what do the survivors have in common" is destroyed
by the time you go looking for it.

So this records everything at launch instead — socials, the raw GoPlus record,
liquidity, and a dense price/volume film — and a separate labelling pass
(scripts/label_launch_outcomes.py) attaches the outcome days later. Entry rules
come from that dataset, not from a hypothesis.

This module MUST NOT import anything from the trading stack (trade_engine,
positions, budget). tests/test_launch_collector.py enforces that statically.

Live API shapes verified 2026-07-30 (see the probe notes on each constant).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

_GT = "https://api.geckoterminal.com/api/v2/networks/bsc/new_pools"
_GT_HEADERS = {"Accept": "application/json;version=20230302"}

# Probed 2026-07-30: BSC new_pools has exactly 4 non-empty pages reaching back
# ~58 min (p1 covers 1.6-18min, p4 covers 43-58min); page 5 is empty. That does
# NOT span the full 15min-2h arm window — but it does not need to. Polling every
# DISCOVERY_INTERVAL_S means every pool is seen repeatedly while it sits in the
# 15-33min band (pages 1-2), so each one is caught the moment it ages in. The 2h
# upper bound is a safety net for pools missed during downtime, not the main path.
DISCOVERY_PAGES = 4
DISCOVERY_INTERVAL_S = 120

ARM_MIN_AGE_S = 15 * 60
ARM_MAX_AGE_S = 2 * 3600
MIN_RESERVE_USD = 20_000.0     # anh TONiE's call. Probed: only ~6% of new pools
                               # clear this, so it — not MAX_FILMS — is the
                               # binding filter on what enters the dataset.

_MAX_429_RETRIES = 1           # yield-always: a gap in a research film costs
                               # nothing, so never grind against a rate limit
_RETRY_SLEEP_S = 2.1           # GeckoTerminal free tier is ~30 req/min
_REQ_TIMEOUT_S = 25


def parse_pools(payload: dict) -> list[dict]:
    """GeckoTerminal new_pools JSON -> the flat dicts arm_candidates() consumes.

    Skips any row we cannot fully trust rather than guessing: a pool with no
    base-token relationship or an unparseable creation time is not a candidate.
    """
    out = []
    for row in (payload or {}).get("data") or []:
        attrs = row.get("attributes") or {}
        token_id = (((row.get("relationships") or {}).get("base_token") or {})
                    .get("data") or {}).get("id") or ""
        if "_" not in token_id:
            continue
        created_ts = _iso_to_ts(attrs.get("pool_created_at"))
        if created_ts is None:
            continue
        out.append({
            "pool_address": attrs.get("address"),
            "token_address": token_id.split("_", 1)[1],
            "name": attrs.get("name"),
            "created_ts": created_ts,
            "reserve_usd": _as_float(attrs.get("reserve_in_usd")),
        })
    return out


def _iso_to_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def arm_candidates(pools: list[dict], now: float, seen: set[str],
                   min_age_s: float = ARM_MIN_AGE_S,
                   max_age_s: float = ARM_MAX_AGE_S,
                   min_reserve_usd: float = MIN_RESERVE_USD) -> list[dict]:
    """Pools worth filming: inside the age window, liquid enough, not seen yet.

    `seen` is compared case-insensitively — GeckoTerminal and DexScreener hand
    back checksummed addresses while GoPlus returns lowercase, and letting the
    same token in twice under two casings would double-film it.
    """
    seen_lower = {s.lower() for s in seen}
    picked: list[dict] = []
    for p in pools:
        token = p.get("token_address")
        created = p.get("created_ts")
        reserve = p.get("reserve_usd")
        if not token or not isinstance(created, (int, float)):
            continue
        if not isinstance(reserve, (int, float)) or reserve < min_reserve_usd:
            continue
        age = now - created
        if age < min_age_s or age > max_age_s:
            continue
        key = token.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)          # a token with two pools is still one film
        picked.append(p)
    return picked


def new_pools(pages: int = DISCOVERY_PAGES) -> list[dict]:
    """Newest BSC pools, page 1..N merged. Never raises — a failed discovery
    tick just means no arms this tick."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        payload = _gt_get(f"{_GT}?page={page}")
        parsed = parse_pools(payload) if payload else []
        if not parsed:
            break        # page 5+ is empty; walking past the end wastes quota
        out.extend(parsed)
    return out


def _gt_get(url: str) -> dict | None:
    """GET with one 429 retry honouring Retry-After, then yield.

    Deliberately gentler than the live bot's fail-closed retries: this is a
    research collector, and a missing sample costs nothing.
    """
    for attempt in range(_MAX_429_RETRIES + 1):
        try:
            r = requests.get(url, headers=_GT_HEADERS, timeout=_REQ_TIMEOUT_S)
            if r.status_code == 429:
                if attempt >= _MAX_429_RETRIES:
                    log.warning("gt_rate_limited_giving_up", url=url)
                    return None
                time.sleep(_as_float(r.headers.get("Retry-After")) or _RETRY_SLEEP_S)
                continue
            return r.json()
        except Exception as e:  # noqa: BLE001 — never let discovery kill the tick
            log.warning("gt_fetch_failed", url=url, error=type(e).__name__)
            if attempt >= _MAX_429_RETRIES:
                return None
            time.sleep(_RETRY_SLEEP_S)
    return None
