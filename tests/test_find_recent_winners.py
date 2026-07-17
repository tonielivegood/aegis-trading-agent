"""Pure-function tests for winner mining. The whole point of follower_stats is to
measure the multiple available to a 60s-lag follower (close of first hourly candle),
NOT the sniper-only launch price — a wrong baseline here silently mines the wrong
wallets, so these tests pin the math."""
from scripts.find_recent_winners import follower_stats, is_catchable


def _candle(ts_h, o, h, l, c):
    return [ts_h * 3600, o, h, l, c, 0]


def test_follower_stats_uses_first_candle_close_not_open():
    # open 0.001 (sniper price), first-candle close 0.01, later peak 0.05
    ohlcv = [_candle(0, 0.001, 0.012, 0.001, 0.01),
             _candle(1, 0.01, 0.02, 0.009, 0.018),
             _candle(9, 0.018, 0.05, 0.017, 0.04)] + [
             _candle(i, 0.04, 0.041, 0.039, 0.04) for i in range(10, 18)]
    s = follower_stats(ohlcv)
    assert s["entry_price"] == 0.01                  # close of candle 1, NOT 0.001
    assert s["multiple"] == 5.0                      # 0.05 / 0.01
    assert s["time_to_peak_h"] == 9.0                # candle at ts 9h holds the peak


def test_follower_stats_sorts_newest_first_input():
    rows = [_candle(9, 1, 5.0, 1, 4), _candle(0, 1, 1.2, 1, 1.0)] + [
        _candle(i, 4, 4, 4, 4) for i in range(1, 9)]
    s = follower_stats(rows)                         # deliberately unsorted input
    assert s["entry_price"] == 1.0 and s["multiple"] == 5.0


def test_follower_stats_insufficient_candles_returns_none():
    assert follower_stats([_candle(0, 1, 1, 1, 1)] * 7) is None
    assert follower_stats([]) is None


def test_follower_stats_zero_entry_returns_none():
    rows = [_candle(i, 0, 0, 0, 0) for i in range(10)]
    assert follower_stats(rows) is None


def test_is_catchable_thresholds():
    good = {"entry_price": 1, "multiple": 4.2, "time_to_peak_h": 7.0}
    assert is_catchable(good, min_multiple=4, min_peak_hours=6) is True
    assert is_catchable({**good, "multiple": 3.9}, 4, 6) is False
    assert is_catchable({**good, "time_to_peak_h": 2.0}, 4, 6) is False   # sniper pump
