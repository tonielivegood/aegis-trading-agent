"""Pure-function tests for the gem report analyzer. Network calls stay untested
(thin wrappers, None on failure); the matching/scoring math is what must not lie."""
from scripts.gem_report import classify, match_hold_times, max_multiple

W1, W2 = "0x" + "1" * 40, "0x" + "2" * 40
TOK_A, TOK_B = "0x" + "a" * 40, "0x" + "b" * 40


def _row(ts, wallet, token, direction):
    return {"ts": ts, "wallet": wallet, "token_address": token,
            "direction": direction, "block": 1, "tx_hash": "0x" + "f" * 64}


def test_match_hold_times_pairs_in_then_out_per_wallet_token():
    rows = [
        _row("2026-07-17T00:00:00+00:00", W1, TOK_A, "in"),
        _row("2026-07-17T00:00:30+00:00", W2, TOK_A, "in"),    # other wallet
        _row("2026-07-17T00:01:00+00:00", W1, TOK_A, "out"),   # W1 held 60s
        _row("2026-07-17T02:00:00+00:00", W2, TOK_A, "out"),   # W2 held ~2h
        _row("2026-07-17T03:00:00+00:00", W1, TOK_B, "out"),   # out w/o in — ignored
    ]
    holds = match_hold_times(rows)
    assert holds[W1] == [60.0]
    assert holds[W2] == [7170.0]


def test_match_hold_times_ignores_rebuy_while_holding():
    rows = [
        _row("2026-07-17T00:00:00+00:00", W1, TOK_A, "in"),
        _row("2026-07-17T00:05:00+00:00", W1, TOK_A, "in"),    # top-up, not new cycle
        _row("2026-07-17T00:10:00+00:00", W1, TOK_A, "out"),
    ]
    assert match_hold_times(rows)[W1] == [600.0]               # first-in → first-out


def test_classify_bands():
    assert classify(30) == "SCALPER"           # 30s
    assert classify(899) == "SCALPER"
    assert classify(3600) == "SWING"           # 1h
    assert classify(200000) == "HOLDER"        # 2.3d


def test_max_multiple_only_counts_candles_after_signal():
    ohlcv = [[100, 1.0, 9.9, 0.5, 1.0, 0],     # BEFORE signal — excluded
             [200, 1.0, 2.0, 0.9, 1.5, 0],
             [300, 1.5, 5.0, 1.2, 4.0, 0]]     # high 5.0 after signal
    assert max_multiple(ohlcv, since_ts=150, base_price=1.0) == 5.0
    assert max_multiple(ohlcv, since_ts=150, base_price=2.0) == 2.5
    assert max_multiple([], since_ts=0, base_price=1.0) is None
    assert max_multiple(ohlcv, since_ts=150, base_price=0) is None
