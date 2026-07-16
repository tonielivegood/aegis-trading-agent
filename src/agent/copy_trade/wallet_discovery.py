"""Part 1 of the v2 spec: build our OWN BSC smart-money list instead of blindly
trusting GMGN's public smart_degen tag (public info = no edge). Pure functions here;
network orchestration lives in scripts/build_bsc_smart_wallets.py so everything in
this module is unit-testable offline."""
from __future__ import annotations

import requests


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def early_buyers(logs: list[dict], exclude: set[str], max_buyers: int = 200) -> list[str]:
    """First-seen unique Transfer recipients of a token's earliest logs — the wallets
    that were in BEFORE the run. Caller passes logs from pair creation onward and
    excludes the pair/zero/router addresses."""
    excl = {a.lower() for a in exclude}
    seen: dict[str, None] = {}
    for lg in logs:
        if len(lg.get("topics", [])) < 3:
            continue
        to_addr = _topic_to_addr(lg["topics"][2])
        if to_addr in excl or to_addr in seen:
            continue
        seen[to_addr] = None
        if len(seen) >= max_buyers:
            break
    return list(seen)


def cross_winner_candidates(buyers_by_token: dict[str, list[str]],
                            min_tokens: int = 2) -> dict[str, int]:
    """Address → how many distinct winner tokens it bought early. Only wallets early
    in >= min_tokens different winners qualify — one lucky entry is noise, repeated
    early entries across independent winners is the signal we're mining."""
    counts: dict[str, int] = {}
    for buyers in buyers_by_token.values():
        for addr in set(buyers):
            counts[addr] = counts.get(addr, 0) + 1
    return {a: c for a, c in counts.items() if c >= min_tokens}


BSCSCAN_URL = "https://api.etherscan.io/v2/api"   # etherscan v2 multichain, chainid=56

MAX_TX_PER_DAY = 100.0     # above this it's a sniper/MEV bot, not a human trader
MAX_LAST_TX_AGE_DAYS = 7.0
MIN_TX_7D = 3


def wallet_activity(bscscan_key: str, address: str, now_ts: int) -> dict | None:
    """Recent-activity profile from BscScan txlist (key already in .env). None on
    any failure — the caller drops the candidate rather than guessing."""
    try:
        r = requests.get(BSCSCAN_URL, params={
            "chainid": 56, "module": "account", "action": "txlist",
            "address": address, "page": 1, "offset": 200, "sort": "desc",
            "apikey": bscscan_key}, timeout=20)
        r.raise_for_status()
        result = r.json().get("result")
        if not isinstance(result, list):
            return None
        stamps = [int(t["timeStamp"]) for t in result if t.get("timeStamp")]
        if not stamps:
            return {"tx_7d": 0, "tx_per_day": 0.0, "last_tx_age_days": 9e9}
        week_ago = now_ts - 7 * 86400
        tx_7d = sum(1 for s in stamps if s >= week_ago)
        span_days = max((now_ts - min(stamps)) / 86400, 1.0)
        return {"tx_7d": tx_7d,
                "tx_per_day": len(stamps) / span_days,
                "last_tx_age_days": (now_ts - max(stamps)) / 86400}
    except Exception:  # noqa: BLE001
        return None


def passes_filters(activity: dict, code: str) -> tuple[bool, str]:
    if code != "0x":
        return False, "contract"
    if activity["tx_per_day"] > MAX_TX_PER_DAY:
        return False, "bot_tx_rate"
    if activity["last_tx_age_days"] > MAX_LAST_TX_AGE_DAYS:
        return False, "cold_wallet"
    if activity["tx_7d"] < MIN_TX_7D:
        return False, "inactive"
    return True, "ok"


def score_candidate(wins_early: int, gmgn_hits: int, in_both: bool) -> float:
    """Early-winner evidence dominates (it's OUR mined signal); GMGN trade frequency
    is a tie-breaker; showing up in both independent sources is strong confirmation."""
    return (min(wins_early, 5) * 2.0
            + min(gmgn_hits, 10) * 0.3
            + (3.0 if in_both else 0.0))


def build_ranked_list(candidates: list[dict], top_n: int = 50) -> list[dict]:
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:top_n]
