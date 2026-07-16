from src.agent.copy_trade.cluster_signal import ClusterBuySignalTracker

T = "0x" + "a" * 40
W1, W2, W3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


def test_below_threshold_returns_none():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    assert tr.record(T, W1, ts=0, price_usd=1.0) is None
    assert tr.record(T, W2, ts=60, price_usd=1.1) is None


def test_three_distinct_wallets_in_window_fires_with_first_price():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W2, ts=300, price_usd=1.5)
    got = tr.record(T, W3, ts=600, price_usd=2.0)
    assert got == {"wallets": [W1, W2, W3], "first_ts": 0, "first_price_usd": 1.0}


def test_same_wallet_repeat_buys_do_not_count_twice():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W1, ts=10, price_usd=1.0)
    assert tr.record(T, W2, ts=20, price_usd=1.0) is None


def test_observations_outside_window_are_pruned():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W2, ts=60, price_usd=1.0)
    assert tr.record(T, W3, ts=16 * 60, price_usd=1.0) is None  # W1 aged out


def test_tokens_are_independent():
    tr = ClusterBuySignalTracker(min_wallets=2, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    assert tr.record("0x" + "b" * 40, W2, ts=1, price_usd=1.0) is None


def test_clear_empties_buffer_so_one_wallet_is_sub_threshold_again():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W2, ts=1, price_usd=1.0)
    got = tr.record(T, W3, ts=2, price_usd=1.0)
    assert got is not None                        # cluster fired
    tr.clear(T)
    assert tr.record(T, W1, ts=3, price_usd=1.0) is None  # buffer wiped, back to 1/3


def test_clear_on_token_with_no_buffer_is_a_noop():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.clear(T)   # must not raise
    assert tr.record(T, W1, ts=0, price_usd=1.0) is None
