"""label_outcome: turns a launch snapshot + today's reality into a labelled row.

The label is the whole point of the collector — a film without an outcome
teaches nothing. Measured base rate when this was written: of 16 confirmed 4x+
winners, only 2 were still alive days later.
"""
import scripts.label_launch_outcomes as lbl
from scripts.label_launch_outcomes import fetch_ohlcv, label_outcome, pending_tokens

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


def test_peak_and_its_timestamp_come_from_the_same_post_arm_window():
    # Live 2026-07-30: the hour candle a token is armed in OPENS before the arm,
    # so taking the peak from post-arm candles but its time from all candles
    # reported time_to_peak_h=-0.94 next to max_multiple=None.
    rows = _candles([9.0, 1.0, 3.0], start=ARMED - HOUR)
    out = label_outcome(LAUNCH, None, rows, now=ARMED + 72 * HOUR)
    assert out["max_multiple"] == 3.0        # the 9.0 pre-arm candle is excluded
    assert out["time_to_peak_h"] == 1.0      # ...from the timestamp too


def test_fetch_ohlcv_retries_a_429_then_returns_the_candles(monkeypatch):
    calls = []

    class _R:
        # GeckoTerminal really answers a 429 with `Retry-After: 0`; obeying it
        # literally burns all three attempts in milliseconds.
        def __init__(self, code):
            self.status_code, self.headers = code, {"Retry-After": "0"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return {"data": {"attributes": {"ohlcv_list": [[1, 2, 3, 4, 5, 6]]}}}

    def _get(*a, **k):
        calls.append(1)
        return _R(429 if len(calls) == 1 else 200)

    slept = []
    monkeypatch.setattr(lbl.requests, "get", _get)
    monkeypatch.setattr(lbl.time, "sleep", slept.append)
    assert fetch_ohlcv("0xpool") == [[1, 2, 3, 4, 5, 6]]
    assert len(calls) == 2
    assert slept == [lbl._GT_MIN_GAP_S]     # the useless header did not win


def test_fetch_ohlcv_returns_none_not_empty_when_every_attempt_fails(monkeypatch):
    # None ("unknown") must stay distinguishable from [] ("never traded"):
    # outcomes.jsonl is append-only, so a row written off a failed read bakes a
    # wrong null peak in permanently.
    monkeypatch.setattr(lbl.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(lbl.time, "sleep", lambda s: None)
    assert fetch_ohlcv("0xpool") is None


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
