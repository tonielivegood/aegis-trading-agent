"""label_outcome: turns a launch snapshot + today's reality into a labelled row.

The label is the whole point of the collector — a film without an outcome
teaches nothing. Measured base rate when this was written: of 16 confirmed 4x+
winners, only 2 were still alive days later.
"""
from scripts.label_launch_outcomes import label_outcome, pending_tokens

ARMED = 1_785_000_000.0
HOUR = 3600.0
LAUNCH = {"token_address": "0x" + "1" * 40, "symbol": "CZ", "ts": ARMED,
          "pool_address": "0xpool", "arm_price_usd": 1.0}


def _candles(prices, start=ARMED, vol=100.0):
    """[ts, open, high, low, close, volume] — GeckoTerminal's hourly shape."""
    return [[start + i * HOUR, p, p, p, p, vol] for i, p in enumerate(prices)]


def test_alive_when_liquidity_is_above_the_dead_floor():
    out = label_outcome(LAUNCH, {"liquidity_usd": 38_000.0, "price_usd": 7.5},
                        _candles([1.0, 2.0]), now=ARMED + 72 * HOUR)
    assert out["alive"] is True
    assert out["liquidity_usd_now"] == 38_000.0
    assert out["multiple_now"] == 7.5


def test_dead_when_dexscreener_has_no_pairs_left():
    # The common case: 14 of 16 mined winners reached $0 within days, and once
    # the pairs die DexScreener returns nothing at all for the token.
    out = label_outcome(LAUNCH, None, _candles([1.0, 5.0, 0.01]),
                        now=ARMED + 72 * HOUR)
    assert out["alive"] is False
    assert out["liquidity_usd_now"] is None
    # The peak still has to be recovered from OHLCV — a token can 5x and then
    # rug, and "it rugged" alone would hide that the entry rule was right.
    assert out["max_multiple"] == 5.0


def test_dead_when_liquidity_is_below_the_dead_floor():
    out = label_outcome(LAUNCH, {"liquidity_usd": 12.0, "price_usd": 0.0001},
                        _candles([1.0]), now=ARMED + 72 * HOUR)
    assert out["alive"] is False


def test_time_to_peak_and_the_hour_marks_anh_tonie_cares_about():
    # CZ peaked 41h after entry, so the shape of the run matters as much as
    # its size: 1h/4h/24h marks say whether a 4h film could have caught it.
    prices = [1.0] + [2.0] * 3 + [6.0] + [10.0] * 19 + [42.5] + [3.0] * 10
    out = label_outcome(LAUNCH, {"liquidity_usd": 50_000.0, "price_usd": 3.0},
                        _candles(prices), now=ARMED + 40 * HOUR)
    assert out["max_multiple"] == 42.5
    assert out["time_to_peak_h"] == 24.0
    assert out["mult_1h"] == 2.0
    assert out["mult_4h"] == 6.0
    assert out["mult_24h"] == 42.5


def test_zero_or_missing_arm_price_never_divides_by_zero():
    for bad in (0.0, None):
        out = label_outcome({**LAUNCH, "arm_price_usd": bad},
                            {"liquidity_usd": 50_000.0, "price_usd": 2.0},
                            _candles([1.0, 2.0]), now=ARMED + 72 * HOUR)
        assert out["max_multiple"] is None and out["multiple_now"] is None
        assert out["alive"] is True          # liquidity is still knowable


def test_last_active_h_comes_from_the_last_candle_with_real_volume():
    # Hourly OHLCV carries no liquidity series, so the last hour that actually
    # traded is the cheapest available proxy for time-of-death.
    rows = _candles([1.0, 2.0, 3.0, 4.0])
    rows[2][5] = 0.0
    rows[3][5] = 0.0
    out = label_outcome(LAUNCH, None, rows, now=ARMED + 72 * HOUR)
    assert out["last_active_h"] == 1.0


def test_empty_ohlcv_is_labelled_not_crashed():
    out = label_outcome(LAUNCH, None, [], now=ARMED + 72 * HOUR)
    assert out["alive"] is False
    assert out["max_multiple"] is None and out["time_to_peak_h"] is None
    assert out["ohlcv_candles"] == 0


def test_pending_tokens_skips_films_that_are_too_young():
    snaps = [{"event": "launch", "token_address": "0xa", "ts": ARMED},
             {"event": "launch", "token_address": "0xb", "ts": ARMED + 70 * HOUR}]
    out = pending_tokens(snaps, [], now=ARMED + 72 * HOUR, min_age_h=72,
                         refresh_until_h=336)
    assert [s["token_address"] for s in out] == ["0xa"]


def test_pending_tokens_relabels_until_the_refresh_horizon():
    # Tokens die on day 5, not day 3 — one label is a point, repeated labels
    # are a survival curve.
    snaps = [{"event": "launch", "token_address": "0xa", "ts": ARMED}]
    done = [{"token_address": "0xa", "labelled_at": ARMED + 73 * HOUR}]
    still = pending_tokens(snaps, done, now=ARMED + 100 * HOUR, min_age_h=72,
                           refresh_until_h=336)
    assert [s["token_address"] for s in still] == ["0xa"]
    final = pending_tokens(snaps, done, now=ARMED + 400 * HOUR, min_age_h=72,
                           refresh_until_h=336)
    assert final == []


def test_pending_tokens_ignores_non_launch_rows_and_dedupes():
    snaps = [{"event": "launch", "token_address": "0xa", "ts": ARMED},
             {"event": "info", "token_address": "0xa", "ts": ARMED + 60},
             {"event": "launch", "token_address": "0xa", "ts": ARMED + 120}]
    out = pending_tokens(snaps, [], now=ARMED + 100 * HOUR, min_age_h=72,
                         refresh_until_h=336)
    assert len(out) == 1
    assert out[0]["ts"] == ARMED          # the FIRST launch row wins
