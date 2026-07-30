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

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .prices import holder_stats_from_record, pair_stats_from_pairs
from .watchlist import Watchlist
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

_DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="

# Batch sizes. Probed 2026-07-30 with 5 real addresses: both endpoints accept a
# comma-separated list and return one merged payload. Batching is what keeps the
# whole collector inside the free tiers — 60 concurrent films at a 30s interval
# is 4 DexScreener calls/min batched, versus 120 unbatched.
DEX_BATCH = 30
GOPLUS_BATCH = 15
_GOPLUS_MIN_INTERVAL_S = 2.0   # floor between GoPlus calls; it is the tightest
                               # free tier of the three (~30 req/min)


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


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_pairs_batch(tokens: list[str]) -> dict[str, list[dict]]:
    """token (lowercase) -> its BSC pairs, for many tokens in few calls.

    Two demux traps, both confirmed against the live API on 2026-07-30:
      1. DexScreener echoes CHECKSUMMED addresses even when asked in lowercase,
         so every comparison here is lowercased. A case-sensitive lookup returns
         nothing for every token, silently.
      2. A returned pair may carry a requested token as its QUOTE side. Counting
         those as the token's own pair would read the other side's price and
         liquidity into its film — a wrong number that looks entirely plausible.
    Every requested token is present in the result, mapped to [] if it had no
    pairs, so callers never need to guard for KeyError.
    """
    out: dict[str, list[dict]] = {t.lower(): [] for t in tokens}
    if not tokens:
        return out
    for chunk in _chunks([t.lower() for t in tokens], DEX_BATCH):
        payload = _json_get(_DEXSCREENER + ",".join(chunk))
        for pair in (payload or {}).get("pairs") or []:
            if pair.get("chainId") != "bsc":
                continue
            base = ((pair.get("baseToken") or {}).get("address") or "").lower()
            if base in out:                     # base side only — see trap 2
                out[base].append(pair)
    return out


def goplus_batch(tokens: list[str]) -> dict[str, dict]:
    """token (lowercase) -> its raw GoPlus token_security record.

    Tokens GoPlus has not analysed yet are ABSENT from the result rather than
    mapped to {}. That distinction matters: GoPlus analyses asynchronously and
    returned a record for only 1 of 5 brand-new tokens in the 2026-07-30 probe,
    so "no record" is the normal early state, not an error, and must not be
    mistaken for "analysed and clean".
    """
    out: dict[str, dict] = {}
    if not tokens:
        return out
    for i, chunk in enumerate(_chunks([t.lower() for t in tokens], GOPLUS_BATCH)):
        if i:
            time.sleep(_GOPLUS_MIN_INTERVAL_S)
        payload = _json_get(_GOPLUS + ",".join(chunk))
        for key, record in ((payload or {}).get("result") or {}).items():
            if isinstance(record, dict):
                out[key.lower()] = record
    return out


def _json_get(url: str) -> dict | None:
    """GET returning parsed JSON, or None. Yields rather than grinding on 429 —
    the collector must never be the reason an API starts refusing us."""
    for attempt in range(_MAX_429_RETRIES + 1):
        try:
            r = requests.get(url, timeout=_REQ_TIMEOUT_S)
            if r.status_code == 429:
                if attempt >= _MAX_429_RETRIES:
                    log.warning("batch_rate_limited_giving_up", url=url[:80])
                    return None
                time.sleep(_as_float(r.headers.get("Retry-After")) or _RETRY_SLEEP_S)
                continue
            return r.json()
        except Exception as e:  # noqa: BLE001 — a lost sample costs nothing
            log.warning("batch_fetch_failed", url=url[:80], error=type(e).__name__)
            if attempt >= _MAX_429_RETRIES:
                return None
            time.sleep(_RETRY_SLEEP_S)
    return None


# The 7 contract flags GoPlus can report. Kept here only to answer "has GoPlus
# finished analysing this token yet" — the collector does NOT gate on them (that
# is rug_check.py's job on the trading path). Storing the raw record is what
# matters; these are for the completeness flag.
_GATED_FLAGS = ("is_mintable", "can_take_back_ownership", "hidden_owner",
                "owner_change_balance", "transfer_pausable",
                "slippage_modifiable", "is_proxy")


def _best(pairs: list[dict]) -> dict:
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def socials_of(pair: dict) -> dict:
    """The `info` block — the thing that is ERASED when a token dies, which is
    exactly why it must be captured live. Probed 2026-07-30: 4 of 5 brand-new
    tokens have no info block at all, so absence is normal and is recorded as
    such rather than treated as an error."""
    info = pair.get("info") or {}
    return {"socials": list(info.get("socials") or []),
            "websites": list(info.get("websites") or []),
            "info_present": bool(info)}


def goplus_complete(record: dict | None) -> bool:
    """True once GoPlus has actually analysed the contract. It analyses
    asynchronously — a brand-new token commonly returns a record with these
    fields blank, and reading blank as "0" would mean reading "not analysed" as
    "clean". That exact confusion was a Critical bug in rug_check.py."""
    if not record:
        return False
    return all(str(record.get(f, "")).strip() != "" for f in _GATED_FLAGS)


def launch_snapshot(pool: dict, pairs: list[dict], goplus: dict | None,
                    now: float) -> dict:
    """Everything about a token that is unrecoverable once it dies."""
    best = _best(pairs) if pairs else {}
    base = best.get("baseToken") or {}
    stats = pair_stats_from_pairs(pairs) if pairs else {}
    return {
        "event": "launch", "ts": now,
        "token_address": (pool.get("token_address") or "").lower(),
        "pool_address": pool.get("pool_address"),
        "symbol": base.get("symbol"), "name": pool.get("name"),
        "pool_created_ts": pool.get("created_ts"),
        "pool_age_s_at_arm": (now - pool["created_ts"]
                              if isinstance(pool.get("created_ts"), (int, float))
                              else None),
        "arm_price_usd": stats.get("price_usd"),
        "arm_liquidity_usd": stats.get("liquidity_usd", pool.get("reserve_usd")),
        # GeckoTerminal's reserve is what the arm filter actually gated on, so it
        # has to be on the record next to DexScreener's number. They agreed to
        # 1.0x on 25 of 29 live arms (2026-07-30) and disagreed totally on 4,
        # where GT claimed >$20k against DexScreener's $0.01. Storing only one of
        # them leaves the dataset unable to say which source was wrong.
        "arm_reserve_usd_gt": pool.get("reserve_usd"),
        "arm_market_cap_usd": stats.get("market_cap_usd"),
        "pair_created_at_ms": stats.get("pair_created_at_ms"),
        "pairs_count": len(pairs),
        **socials_of(best),
        "goplus": goplus,                     # RAW and whole, not a subset
        "goplus_complete": goplus_complete(goplus),
    }


def sample_row(token: str, pairs: list[dict], holders: dict | None,
               now: float, armed_at: float) -> dict | None:
    """One point in a token's film. None when the token has no pairs this tick
    (a gap in the film, visible via ts, rather than a fabricated row)."""
    if not pairs:
        return None
    best = _best(pairs)
    stats = pair_stats_from_pairs(pairs)
    vol = best.get("volume") or {}
    return {
        "ts": now, "age_s": now - armed_at,
        "price": stats["price_usd"], "liq": stats["liquidity_usd"],
        "mcap": stats["market_cap_usd"],
        "buys_m5": stats["txns_m5_buys"], "sells_m5": stats["txns_m5_sells"],
        "buys_h1": stats["txns_h1_buys"], "sells_h1": stats["txns_h1_sells"],
        # volume is NOT in pair_stats_from_pairs — the live bot never needed it.
        # It is what makes "buy pressure" analysable: with the txn counts it
        # gives buy_share and average trade size, the closest free proxy for the
        # buy-vs-sell USD split (no free API exposes the real split).
        "vol_m5": _as_float(vol.get("m5")), "vol_h1": _as_float(vol.get("h1")),
        "vol_h6": _as_float(vol.get("h6")), "vol_h24": _as_float(vol.get("h24")),
        "chg_m5": stats["price_change_m5"], "chg_h1": stats["price_change_h1"],
        "holders": (holders or {}).get("holder_count"),
        "top_pct": (holders or {}).get("top_pct"),
        "top5_pct": (holders or {}).get("top5_pct"),
    }


# ---------------------------------------------------------------- collector loop

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data" / "launch_collector"
FILMS_PATH = DATA_DIR / "films.jsonl"
SNAPSHOTS_PATH = DATA_DIR / "snapshots.jsonl"
STATE_PATH = DATA_DIR / "state.json"

SAMPLE_INTERVAL_S = 30         # DexScreener's m5 window / 10, so an imbalance is
                               # visible while it is forming rather than after
MAX_FILMS = 60
FILM_MAX_AGE_S = 4 * 3600      # anh TONiE's call: dense film long enough for
                               # ENTRY rules. The long tail (exit rules) is
                               # reconstructed hourly by the labelling pass.
HOLDER_SAMPLE_EVERY_N = 10     # GoPlus every 10th tick = every 5 min

# Sample bias is the single biggest threat to this dataset: if launches outpace
# the cap, MAX_FILMS and MIN_RESERVE_USD — not the market — decide what gets
# filmed, and every rule derived later inherits that. There is no way to design
# it away, only to measure it, so the misses are counted and persisted.
STATS = {"arms_skipped_cap": 0, "arms_skipped_empty_pool": 0}


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def discover_and_arm(wl, seen: set[str], snapshots_path: Path,
                     now: float | None = None) -> int:
    """Find new pools in the age window and start filming them.

    Writes exactly one `launch` snapshot per armed token, capturing the socials
    and the raw GoPlus record — the things that cannot be recovered once the
    token dies.
    """
    now = time.time() if now is None else now
    pools = new_pools()
    picked = arm_candidates(pools, now=now, seen=seen)
    if not picked:
        return 0

    tokens = [p["token_address"].lower() for p in picked]
    pairs_by_token = fetch_pairs_batch(tokens)
    goplus_by_token = goplus_batch(tokens)

    armed = 0
    for pool in picked:
        token = pool["token_address"].lower()
        pairs = pairs_by_token.get(token) or []
        stats = pair_stats_from_pairs(pairs) if pairs else {}
        price = stats.get("price_usd") or 0.0
        liq = stats.get("liquidity_usd") or pool.get("reserve_usd") or 0.0
        # GeckoTerminal's reserve got these through the age/liquidity filter, but
        # on 4 of 29 live arms (2026-07-30) DexScreener said the pool held $0.01
        # at that same moment — and every one of those 4 read zero for its whole
        # film and never recovered, so DexScreener was the truthful source. An
        # empty pool is not a sample; filming it burns a slot and four hours of
        # API quota to record zeros. Only skip when DexScreener actually answered
        # for the token: no pairs at all means "not indexed yet", not "empty".
        if pairs and (stats.get("liquidity_usd") or 0.0) < MIN_RESERVE_USD:
            STATS["arms_skipped_empty_pool"] += 1
            log.info("launch_skipped_empty_pool", token=token,
                     dex_liq=round(stats.get("liquidity_usd") or 0.0, 2),
                     gt_reserve=round(pool.get("reserve_usd") or 0.0, 2))
            continue
        if not wl.arm(token, wallet="pool", price=price, liquidity=liq, now=now):
            STATS["arms_skipped_cap"] += 1
            continue
        seen.add(token)
        _append(snapshots_path,
                launch_snapshot(pool, pairs, goplus_by_token.get(token), now))
        armed += 1
        log.info("launch_armed", token=token, liq=round(liq, 2),
                 age_s=round(now - pool["created_ts"]))
    return armed


# Last-seen values per token, so `security`/`info` rows are appended only when
# something actually changed rather than once per tick.
_last_info: dict[str, dict] = {}
_last_goplus: dict[str, dict] = {}


def sample_tick(wl, tick: int, snapshots_path: Path,
                now: float | None = None) -> int:
    """One sampling pass over every film in progress."""
    now = time.time() if now is None else now
    wl.expire(now)
    films = wl.active()
    tokens = [d.token_address for d in films]
    # Drop change-detection state for films that have ended, otherwise these
    # dicts grow by one entry per token for the life of the process.
    for stale in set(_last_info) - set(tokens):
        _last_info.pop(stale, None)
    for stale in set(_last_goplus) - set(tokens):
        _last_goplus.pop(stale, None)
    if not films:
        return 0

    pairs_by_token = fetch_pairs_batch(tokens)
    want_goplus = tick % HOLDER_SAMPLE_EVERY_N == 0
    goplus_by_token = goplus_batch(tokens) if want_goplus else {}

    written = 0
    for d in films:
        token = d.token_address
        pairs = pairs_by_token.get(token) or []
        record = goplus_by_token.get(token)
        holders = holder_stats_from_record(record) if record else None

        row = sample_row(token, pairs, holders, now=now, armed_at=d.armed_at)
        if row is None:
            log.info("sample_skipped", token=token, reason="no_pairs")
            continue
        wl.add_sample(token, row)
        written += 1

        if pairs:
            info = socials_of(_best(pairs))
            if info != _last_info.get(token):
                if _last_info.get(token) is not None or info["info_present"]:
                    # Capturing WHEN socials appear is the point — retrospective
                    # research cannot see it, because a dead token's info block
                    # is erased entirely.
                    _append(snapshots_path,
                            {"event": "info", "ts": now, "token_address": token,
                             **info})
                _last_info[token] = info

        if record is not None and record != _last_goplus.get(token):
            _append(snapshots_path,
                    {"event": "security", "ts": now, "token_address": token,
                     "goplus": record, "goplus_complete": goplus_complete(record)})
            _last_goplus[token] = record
    return written


def run_collector(once: bool = False) -> None:
    """Discover, film, repeat. Never trades — see the module docstring."""
    wl = Watchlist(FILMS_PATH, max_dossiers=MAX_FILMS, max_age_s=FILM_MAX_AGE_S)
    seen: set[str] = set()
    tick = 0
    last_discovery = 0.0
    log.info("launch_collector_start", max_films=MAX_FILMS,
             sample_interval_s=SAMPLE_INTERVAL_S, film_max_age_s=FILM_MAX_AGE_S)
    while True:
        tick += 1
        now = time.time()
        if once or now - last_discovery >= DISCOVERY_INTERVAL_S:
            try:
                discover_and_arm(wl, seen, SNAPSHOTS_PATH, now)
            except Exception as e:  # noqa: BLE001 — a bad tick must not end the run
                log.warning("discovery_tick_failed", error=type(e).__name__)
            last_discovery = now
        try:
            sample_tick(wl, tick, SNAPSHOTS_PATH, now)
        except Exception as e:  # noqa: BLE001
            log.warning("sample_tick_failed", error=type(e).__name__)

        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "last_tick_at": datetime.now(timezone.utc).isoformat(),
            "tick": tick, "active_films": len(wl.active()),
            "tokens_seen": len(seen), **STATS}), encoding="utf-8")
        if once:
            break
        time.sleep(SAMPLE_INTERVAL_S)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Record new BSC token launches")
    ap.add_argument("--once", action="store_true", help="run a single tick")
    run_collector(once=ap.parse_args().once)
