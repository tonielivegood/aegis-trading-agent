import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.positions import CopyPosition, PositionStore
from src.agent.copy_trade.trade_engine import TradeEngine

T = "0x" + "a" * 40
W1, W2, W3, OUTSIDER = ("0x" + c * 40 for c in "1234")
CLUSTER = {"wallets": [W1, W2, W3], "first_ts": 0.0, "first_price_usd": 1.0}


def _engine(tmp_path, shadow=True, executors=None):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=executors,
                      shadow_mode=shadow, journal_path=tmp_path / "closed.jsonl")
    return eng, budget, store


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_shadow_open_never_touches_executors(_g, _p, _t, tmp_path):
    executors = MagicMock()
    eng, budget, store = _engine(tmp_path, shadow=True, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    pos = store.find_by_token(T)
    assert pos.simulated is True
    assert pos.entry_price_usd == 2.0 * (1 + 0.04 + 0.01)
    assert pos.cluster_wallets == [W1, W2, W3]
    assert pos.first_price_usd == 1.0                     # from CLUSTER fixture
    assert budget.available_usd == 16.14 - 3.0
    executors.assert_not_called()
    assert not executors.method_calls        # zero interaction, ever


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_first_price_usd_none_in_cluster_defaults_to_zero(_g, _p, _t, tmp_path):
    """cluster_signal.record() can report first_price_usd=None (no price at
    observation time) — that must not crash the fill or leak a None into the
    journal used by the shadow-mode entry-lateness report."""
    eng, _budget, store = _engine(tmp_path)
    cluster = {"wallets": [W1, W2, W3], "first_ts": 0.0, "first_price_usd": None}
    assert eng.open_cluster_position(T, "GEM", 18, cluster) is True
    assert store.find_by_token(T).first_price_usd == 0.0


@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(False, None))
def test_safety_gate_blocks_and_releases_budget(_s, tmp_path):
    eng, budget, store = _engine(tmp_path)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14
    assert store.all() == []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_exit_needs_two_cluster_wallets_outsiders_ignored(_g, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.on_exit_signal(OUTSIDER, T)
    eng.on_exit_signal(W1, T)
    assert store.find_by_token(T) is not None            # 1 of 3 — still holding
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W1, T)                            # duplicate — still 1
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W2, T)                            # 2 of 3 — close
    assert store.find_by_token(T) is None
    assert budget.available_usd == 16.14                 # slice released


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_closes_at_70pct_drawdown(_s, price_mock, _t, tmp_path):
    price_mock.return_value = 10.0
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18,
                              {"wallets": [W1, W2, W3], "first_ts": 0.0,
                               "first_price_usd": 10.0})
    entry = store.find_by_token(T).entry_price_usd
    price_mock.return_value = entry * 0.31               # -69% — hold
    eng.check_valve()
    assert store.find_by_token(T) is not None
    price_mock.return_value = entry * 0.29               # -71% — dump
    eng.check_valve()
    assert store.find_by_token(T) is None
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "valve" and row["simulated"] is True
    assert row["pnl_pct"] < -0.5
    assert row["first_price_usd"] == 10.0                 # ties entry to ví-1 buy price


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=None)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_holds_when_price_unavailable(_s, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    # open with a known price first
    with patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=5.0):
        eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.check_valve()                                    # price None — do nothing
    assert store.find_by_token(T) is not None


# ---------- live-money path (Finding 1) ----------

@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_buy_success_opens_real_position(_s, rank_mock, tmp_path):
    buy_result = MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
    pancake = MagicMock()
    pancake.swap.return_value = buy_result
    executors = {"pancake": pancake}
    rank_mock.return_value = ["pancake"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    pos = store.find_by_token(T)
    assert pos.simulated is False
    assert pos.token_amount == 5.0                        # 5e18 wei / 10**18
    assert pos.entry_price_usd == 3.0 / 5.0                # usd_size / token_amount
    pancake.swap.assert_called_once_with("USDT", "GEM", 3.0)
    assert budget.available_usd == 16.14 - 3.0


@patch("src.agent.copy_trade.trade_engine.rank_backends", return_value=[])
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_buy_no_route_releases_budget(_s, _r, tmp_path):
    executors = {"pancake": MagicMock()}
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14                  # slice released, not leaked
    assert store.all() == []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_succeeds_on_first_ranked_backend(_s, rank_mock, _p, _t, tmp_path):
    pancake = MagicMock()
    pancake.swap.return_value = MagicMock(received_out_wei=5 * 10 ** 18,
                                          expected_out_wei=0)
    executors = {"pancake": pancake}
    rank_mock.return_value = ["pancake"]                  # used for buy AND sell
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    pancake.swap.reset_mock()

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # 2 of 3 — triggers close
    assert store.find_by_token(T) is None
    pancake.swap.assert_called_once_with("GEM", "USDT", 5.0)
    assert budget.available_usd == 16.14                  # slice released on close


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_fails_over_to_second_backend(_s, rank_mock, _p, _t, tmp_path):
    def _swap_a(token_in, token_out, amount):
        if token_in == "USDT":                            # the buy leg
            return MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
        raise RuntimeError("route dried up")                # the sell leg fails

    backend_a = MagicMock()
    backend_a.swap.side_effect = _swap_a
    backend_b = MagicMock()
    backend_b.swap.return_value = MagicMock()
    executors = {"a": backend_a, "b": backend_b}
    rank_mock.return_value = ["a", "b"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # triggers close: a fails, b wins
    assert store.find_by_token(T) is None                  # closed despite first failure
    backend_b.swap.assert_called_once_with("GEM", "USDT", 5.0)
    assert budget.available_usd == 16.14


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_all_backends_fail_keeps_position_open(_s, rank_mock, _p, _t,
                                                         tmp_path):
    def _swap(token_in, token_out, amount):
        if token_in == "USDT":                            # the buy leg
            return MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
        raise RuntimeError("no route")                      # every sell leg fails

    backend_a = MagicMock()
    backend_a.swap.side_effect = _swap
    backend_b = MagicMock()
    backend_b.swap.side_effect = _swap
    executors = {"a": backend_a, "b": backend_b}
    rank_mock.return_value = ["a", "b"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    assert budget.available_usd == 16.14 - 3.0

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # all backends fail
    assert store.find_by_token(T) is not None              # position stays open
    assert store.find_by_token(T).exited_by == [W1, W2]    # votes recorded, not lost
    assert budget.available_usd == 16.14 - 3.0              # NOT released — not leaked


# ---------- Finding 2: shadow/real wiring-bug guard ----------

@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=0.1)
def test_shadow_engine_refuses_to_close_a_real_position(_p, tmp_path):
    """Defense-in-depth: if a shadow-mode engine ever ends up holding a
    simulated=False position (a hypothetical future wiring bug), _close() must
    refuse to call _sell_live — never touching self._executors — rather than
    crash (executors may be None) or risk a real swap during shadow mode."""
    executors = MagicMock()
    eng, budget, store = _engine(tmp_path, shadow=True, executors=executors)
    bad_pos = CopyPosition(
        token_symbol="GEM", token_address=T, token_decimals=18,
        source_wallet="", usd_size=3.0, token_amount=5.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        cluster_wallets=[W1, W2, W3], entry_price_usd=1.0,
        simulated=False, first_price_usd=1.0)
    store.open_position(bad_pos)                          # bypass _paper_fill

    eng.check_valve()                                     # price 0.1 vs entry 1.0 → -90%

    assert store.find_by_token(T) is not None              # NOT removed — fail safe
    executors.assert_not_called()
    assert not executors.method_calls                      # zero interaction, ever


# ---------- v3 entry gates ----------
import time as _time


def _engine_v3(tmp_path, **kw):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=None,
                      shadow_mode=True, journal_path=tmp_path / "closed.jsonl",
                      signals_path=tmp_path / "signals.jsonl", **kw)
    return eng, budget, store


def _signals(tmp_path):
    p = tmp_path / "signals.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_staleness_gate_skips_when_cluster_already_sold_in_batch(_s, _p, _t, tmp_path):
    eng, budget, store = _engine_v3(tmp_path)
    ok = eng.open_cluster_position(T, "GEM", 18, CLUSTER,
                                   batch_sellers={W1.lower(), W2.lower()})
    assert ok is False and store.all() == []
    assert budget.available_usd == 16.14                  # nothing allocated
    assert _signals(tmp_path)[-1]["decision"] == "skipped_stale"
    # only 1 cluster wallet selling is NOT stale (need >= exit_wallets=2)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER,
                                     batch_sellers={W1.lower()}) is True


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_cooldown_blocks_reopen_and_seeds_from_journal(_s, _p, _t, tmp_path):
    eng, _b, store = _engine_v3(tmp_path, cooldown_minutes=60)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # full close (partial off)
    assert store.find_by_token(T) is None
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False   # cooling down
    assert _signals(tmp_path)[-1]["decision"] == "skipped_cooldown"
    # a NEW engine on the same journal (simulates restart) still refuses
    eng2, _b2, _s2 = _engine_v3(tmp_path, cooldown_minutes=60)
    assert eng2.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    # zero-cooldown engine (v2 behavior) is unaffected
    eng3, _b3, _s3 = _engine_v3(tmp_path)
    assert eng3.open_cluster_position(T, "GEM", 18, CLUSTER) is True


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
@patch("src.agent.copy_trade.trade_engine.get_pair_stats")
def test_gem_filter_age_mcap_liquidity(stats_mock, _s, _p, _t, tmp_path):
    young = _time.time() * 1000 - 2 * 86400_000           # 2 days old
    old = _time.time() * 1000 - 40 * 86400_000            # 40 days old
    gates = dict(max_token_age_days=14, max_market_cap_usd=5_000_000,
                 min_liquidity_usd=20_000)
    good = {"price_usd": 1.0, "liquidity_usd": 50_000.0,
            "market_cap_usd": 800_000.0, "pair_created_at_ms": young,
            "pair_address": "0x" + "c" * 40}

    for bad, expect in [
        (dict(good, market_cap_usd=95_000_000.0), "mcap"),       # the O trade
        (dict(good, pair_created_at_ms=old), "age"),
        (dict(good, liquidity_usd=3_000.0), "liquidity"),
        (dict(good, pair_created_at_ms=None), "age"),            # unknown age = skip
        (None, "no_pair_stats"),                                 # API down = skip
    ]:
        stats_mock.return_value = bad
        eng, budget, store = _engine_v3(tmp_path, **gates)
        assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
        assert store.all() == [] and budget.available_usd == 16.14
        assert expect in (_signals(tmp_path)[-1]["decision"]
                          + _signals(tmp_path)[-1]["detail"])

    stats_mock.return_value = good
    eng, _b, store = _engine_v3(tmp_path, **gates)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    assert _signals(tmp_path)[-1]["decision"] == "opened"
    # engine with all gates None never calls get_pair_stats (v2 behavior)
    stats_mock.reset_mock()
    eng2, _b2, _s2 = _engine_v3(tmp_path)
    eng2.open_cluster_position("0x" + "d" * 40, "GEM2", 18, CLUSTER)
    stats_mock.assert_not_called()
