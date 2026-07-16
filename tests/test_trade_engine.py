import json
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.positions import PositionStore
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
    assert budget.available_usd == 16.14 - 3.0
    executors.assert_not_called()
    assert not executors.method_calls        # zero interaction, ever


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
