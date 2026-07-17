"""The promotion bar is the filter whose absence caused the July scalper incident —
these tests pin its three rules and the insufficient-data refusal."""
from scripts.wallet_audition import (MIN_GEM_BUYS, audit_wallets,
                                     convergence_windows, is_gem_band)

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
    young_ms = (time.time() - 2 * 86400) * 1000
    gem_stats = {**GEM_STATS, "pair_created_at_ms": young_ms}
    stats = {GEM: gem_stats, OLD: {"pair_created_at_ms": young_ms - 300 * 86400_000,
                                   "market_cap_usd": 5e7, "liquidity_usd": 1e6}}
    # W1: 3 distinct gem buys + 1 old buy (75% gem), holds 1h -> PROMOTE
    gems = [GEM, "0x" + "c" * 40, "0x" + "d" * 40]
    for g in gems[1:]:
        stats[g] = gem_stats
    rows = [_row(f"2026-07-18T0{i}:00:00+00:00", W1, t)
            for i, t in enumerate(gems)] + [_row("2026-07-18T03:00:00+00:00", W1, OLD)]
    # W2: only old buys -> REJECT ; W3: 1 gem buy -> INSUFFICIENT
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


def test_convergence_windows_counts_distinct_wallets_in_window():
    rows = [_row("2026-07-18T00:00:00+00:00", W1, GEM),
            _row("2026-07-18T00:10:00+00:00", W2, GEM),
            _row("2026-07-18T00:40:00+00:00", W3, GEM),      # outside 15m of first two
            _row("2026-07-18T00:00:00+00:00", W1, OLD)]      # non-gem: ignored
    assert convergence_windows(rows, {GEM}, window_minutes=15)[GEM] == 2
    assert convergence_windows(rows, {GEM}, window_minutes=60)[GEM] == 3
