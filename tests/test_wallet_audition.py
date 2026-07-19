"""The promotion bar is the filter whose absence caused the July scalper incident —
these tests pin its three rules and the insufficient-data refusal."""
from scripts.wallet_audition import (EARLY_MAX_MEDIAN_AGE_MIN, MIN_GEM_BUYS,
                                     audit_wallets, convergence_windows,
                                     is_gem_band)

W1, W2, W3 = ("0x" + c * 40 for c in "123")
GEM, OLD = "0x" + "a" * 40, "0x" + "b" * 40
CFG = {"max_token_age_days": 14, "max_market_cap_usd": 5_000_000,
       "min_liquidity_usd": 20_000}
GEM_STATS = {"pair_created_at_ms": None, "market_cap_usd": 400_000.0,
             "liquidity_usd": 50_000.0}


def _row(ts, wallet, token, direction="in"):
    return {"ts": ts, "wallet": wallet, "token_address": token,
            "direction": direction, "block": 1, "tx_hash": "0x" + "f" * 64}


def test_is_gem_band_rules():
    import time
    young_ms = (time.time() - 2 * 86400) * 1000
    good = {"pair_created_at_ms": young_ms, "market_cap_usd": 1e6,
            "liquidity_usd": 30_000.0}
    assert is_gem_band(good, 14, 5e6, 2e4) is True
    assert is_gem_band({**good, "market_cap_usd": 9e6}, 14, 5e6, 2e4) is False
    assert is_gem_band({**good, "liquidity_usd": 5_000.0}, 14, 5e6, 2e4) is False
    assert is_gem_band({**good, "pair_created_at_ms": young_ms - 30 * 86400_000},
                       14, 5e6, 2e4) is False
    assert is_gem_band({**good, "pair_created_at_ms": None}, 14, 5e6, 2e4) is False
    assert is_gem_band(None, 14, 5e6, 2e4) is False


def test_audit_promote_reject_insufficient():
    import time
    from datetime import datetime, timezone
    now = time.time()
    young_ms = (now - 2 * 86400) * 1000
    gem_stats = {**GEM_STATS, "pair_created_at_ms": young_ms}
    stats = {GEM: gem_stats, OLD: {"pair_created_at_ms": young_ms - 300 * 86400_000,
                                   "market_cap_usd": 5e7, "liquidity_usd": 1e6}}
    # W1: 3 distinct gem buys, 10/20/30 min after each token's pair_created_at_ms
    # (well inside EARLY_MAX_MEDIAN_AGE_MIN=60) + 1 old buy (75% gem), holds 1h
    # -> PROMOTE. Row timestamps are derived from time.time() (like young_ms
    # itself), not hardcoded dates, so the gap stays exact regardless of run date.
    gems = [GEM, "0x" + "c" * 40, "0x" + "d" * 40]
    for g in gems[1:]:
        stats[g] = gem_stats

    def _ts(minutes_after_creation):
        return datetime.fromtimestamp(
            now - 2 * 86400 + minutes_after_creation * 60, tz=timezone.utc
        ).isoformat()

    rows = [_row(_ts(m), W1, t) for m, t in zip((10, 20, 30), gems)]
    rows += [_row(_ts(180), W1, OLD)]
    # W2: only old buys -> REJECT ; W3: holds=[] (no completed hold-time
    # observations yet, can't judge hold duration) -> INSUFFICIENT
    rows += [_row("2026-07-18T04:00:00+00:00", W2, OLD)] * 4
    rows += [_row("2026-07-18T05:00:00+00:00", W3, GEM)]
    holds = {W1: [3600.0, 4000.0], W2: [30.0], W3: []}
    out = {r["wallet"]: r for r in audit_wallets(rows, stats, holds, CFG)}
    assert out[W1]["verdict"] == "PROMOTE"
    assert out[W2]["verdict"] == "REJECT"
    assert out[W3]["verdict"] == "INSUFFICIENT" and MIN_GEM_BUYS == 3


def test_audit_scalper_hold_time_rejects_even_with_gem_buys():
    import time
    young_ms = (time.time() - 2 * 86400) * 1000
    gems = ["0x" + c * 40 for c in "cde"]
    stats = {g: {**GEM_STATS, "pair_created_at_ms": young_ms} for g in gems}
    rows = [_row(f"2026-07-18T0{i}:00:00+00:00", W1, g) for i, g in enumerate(gems)]
    holds = {W1: [30.0, 45.0, 20.0]}                 # gem buyer but 30s median hold
    out = audit_wallets(rows, stats, holds, CFG)
    assert out[0]["verdict"] == "REJECT"


def test_audit_earliness_bar_promotes_hunters_rejects_followers():
    """A wallet that buys gem-band tokens within the first hour of pair creation
    is a hunter -> PROMOTE. One that clears every other bar but habitually buys
    1.5-4h in is a follower riding someone else's find -> REJECT, not
    INSUFFICIENT (it has real hold-time data; it's just too late)."""
    import time
    from datetime import datetime, timezone
    created_s = time.time() - 5 * 86400   # pair created 5 days ago (< 14d gem window)
    stats = {t: {"pair_created_at_ms": created_s * 1000, "market_cap_usd": 400_000.0,
                "liquidity_usd": 50_000.0}
             for t in ("0x" + c * 40 for c in "678" + "9de")}

    def _ts(minutes_after_creation):
        return datetime.fromtimestamp(
            created_s + minutes_after_creation * 60, tz=timezone.utc
        ).isoformat()

    hunter_gems = ["0x" + c * 40 for c in "678"]
    follower_gems = ["0x" + c * 40 for c in "9de"]
    HUNTER, FOLLOWER = "0x" + "5" * 40, "0x" + "4" * 40
    rows = ([_row(_ts(m), HUNTER, t) for m, t in zip((10, 20, 40), hunter_gems)]
            + [_row(_ts(m), FOLLOWER, t) for m, t in zip((90, 180, 240), follower_gems)])
    holds = {HUNTER: [3600.0, 4000.0, 3800.0], FOLLOWER: [3600.0, 4000.0, 3800.0]}
    out = {r["wallet"]: r for r in audit_wallets(rows, stats, holds, CFG)}
    assert out[HUNTER]["verdict"] == "PROMOTE"
    assert abs(out[HUNTER]["median_entry_age_min"] - 20) < 0.01
    assert out[FOLLOWER]["verdict"] == "REJECT"
    assert abs(out[FOLLOWER]["median_entry_age_min"] - 180) < 0.01
    assert out[FOLLOWER]["median_entry_age_min"] > EARLY_MAX_MEDIAN_AGE_MIN


def test_convergence_windows_counts_distinct_wallets_in_window():
    rows = [_row("2026-07-18T00:00:00+00:00", W1, GEM),
            _row("2026-07-18T00:10:00+00:00", W2, GEM),
            _row("2026-07-18T00:40:00+00:00", W3, GEM),      # outside 15m of first two
            _row("2026-07-18T00:00:00+00:00", W1, OLD)]      # non-gem: ignored
    assert convergence_windows(rows, {GEM}, window_minutes=15)[GEM] == 2
    assert convergence_windows(rows, {GEM}, window_minutes=60)[GEM] == 3
