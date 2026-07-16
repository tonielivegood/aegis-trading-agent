"""Part 1 of the v2 spec: build our OWN BSC smart-money list instead of blindly
trusting GMGN's public smart_degen tag (public info = no edge). Pure functions here;
network orchestration lives in scripts/build_bsc_smart_wallets.py so everything in
this module is unit-testable offline."""
from __future__ import annotations


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
