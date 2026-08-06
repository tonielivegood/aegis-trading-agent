from scripts.search_entry_filters import LOOKBACK, features, score


def _samples(n=10, liq=50_000.0, liq_before=None, buys=8, sells=2,
             vol=5_000.0, top5=None, step=30.0):
    out = []
    for i in range(n):
        out.append({"ts": 1_000.0 + i * step,
                    "price": 1.0 + i,
                    "liq": (liq_before if liq_before is not None and
                            i < n - 1 - LOOKBACK + 1 else liq),
                    "buys_m5": buys, "sells_m5": sells,
                    "vol_m5": vol, "top5_pct": top5})
    return out


def test_features_only_look_backwards_from_the_trigger():
    """Anything read from after the trigger is knowledge the live bot cannot
    have, and would make every result here fiction."""
    s = _samples(n=8)
    s[7]["liq"] = 999_999.0            # the future must not leak in
    f = features(s, 3)
    assert f["liq_growth"] == 1.0      # judged against sample 0, not sample 7


def test_liquidity_growth_is_measured_against_the_lookback_window():
    s = _samples(n=8, liq=60_000.0)
    for i in range(8 - LOOKBACK):
        s[i]["liq"] = 50_000.0
    f = features(s, 7)
    assert f["liq_growth"] == 1.2       # 60k against the 50k LOOKBACK samples back


def test_buy_share_and_churn_come_from_the_trigger_sample():
    s = _samples(buys=9, sells=1, vol=5_000.0, liq=50_000.0)
    f = features(s, 5)
    assert f["buy_share"] == 0.9
    assert f["vol_to_liq"] == 0.1


def test_minutes_to_2x_counts_from_the_arm_not_the_trigger():
    f = features(_samples(n=10, step=60.0), 5)
    assert f["minutes_to_2x"] == 5.0


def test_missing_fields_become_none_rather_than_a_default_that_passes():
    """A filter must never let a token through because a field was absent —
    that is the fail-open shape that already bought a $1.41 pool live."""
    s = [{"ts": 1.0, "price": 1.0}, {"ts": 31.0, "price": 2.0}]
    f = features(s, 1)
    assert f["liq_growth"] is None
    assert f["buy_share"] is None
    assert f["vol_to_liq"] is None
    assert f["top5_pct"] is None


def test_a_selection_too_small_to_mean_anything_scores_nothing():
    rows = [{"ts": 1.0, "f": {"liq_growth": 2.0},
             "o": {6.0: {"outcome": "win", "net_multiple": 6.0}}}
            for _ in range(29)]
    assert score(rows, "x", "liq_growth", lambda v: True, 6.0) is None
    assert score(rows + rows, "x", "liq_growth", lambda v: True, 6.0) is not None


def test_the_filter_actually_selects():
    good = {"ts": 1.0, "f": {"liq_growth": 2.0},
            "o": {6.0: {"outcome": "win", "net_multiple": 6.0}}}
    bad = {"ts": 1.0, "f": {"liq_growth": 0.5},
           "o": {6.0: {"outcome": "rugged", "net_multiple": 0.0}}}
    rows = [good] * 40 + [bad] * 40
    st = score(rows, "grow", "liq_growth", lambda v: v is not None and v >= 1.0,
               6.0)
    assert st["n"] == 40 and st["expectancy"] == 5.0
