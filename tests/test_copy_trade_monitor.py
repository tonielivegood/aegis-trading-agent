# tests/test_copy_trade_monitor.py
"""Integration-ish tests for the v2 scan pipeline: events → cluster → engine.
The safety-critical assertions: 3 distinct-wallet buys open exactly ONE position;
a 4th buy on the same token opens nothing; shadow mode performs zero real calls."""
import time
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.chain_events import WalletEvent
from src.agent.copy_trade.cluster_signal import ClusterBuySignalTracker
from src.agent.copy_trade.monitor import process_events
from src.agent.copy_trade.positions import PositionStore
from src.agent.copy_trade.trade_engine import TradeEngine

T = "0x" + "a" * 40
W1, W2, W3, W4 = ("0x" + c * 40 for c in "1234")


def _ev(wallet, direction="in", token=T, block=1):
    return WalletEvent(wallet=wallet, token_address=token, direction=direction,
                       amount_raw=10 ** 18, tx_hash="0x" + "f" * 64, block=block)


def _pipeline(tmp_path):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    engine = TradeEngine(budget=budget, store=store, executors=None,
                         shadow_mode=True,
                         journal_path=tmp_path / "closed.jsonl")
    tracker = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    return tracker, engine, store


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.02, 0.02))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_three_buys_open_exactly_one_shadow_position(_s, _mp, _ep, _t, tmp_path):
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1), _ev(W2)], tracker, engine, store, None, meta)
    assert store.all() == []                       # 2 of 3 — no trade
    process_events([_ev(W3)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1 and store.all()[0].simulated is True
    process_events([_ev(W4)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1                   # dup-token guard


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_out_events_route_to_exit_logic(_s, _mp, _ep, _t, tmp_path):
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1), _ev(W2), _ev(W3)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1
    process_events([_ev(W1, "out"), _ev(W2, "out")],
                   tracker, engine, store, None, meta)
    assert store.all() == []                       # 2-of-cluster exit fired


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_close_clears_tracker_so_stale_wallet_cannot_immediately_reopen(
        _s, _mp, _ep, _t, tmp_path):
    """Finding I1 regression: once a cluster-exit close happens, the wallets that
    formed the original cluster must NOT still be sitting in the tracker's buffer
    — otherwise a single stale "in" event from one of them re-satisfies the old
    >=3-wallet buffer and immediately re-opens a new position, inflating the
    shadow-mode cluster-event count with re-entry churn."""
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1), _ev(W2), _ev(W3)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1
    process_events([_ev(W1, "out"), _ev(W2, "out")],
                   tracker, engine, store, None, meta)
    assert store.all() == []                       # 2-of-cluster exit fired
    # W3 was part of the original cluster and is still within the 15-min window.
    # Without clearing the tracker's buffer on close, this single "in" event
    # would immediately re-fire (the buffer already held >=3 wallets) and
    # re-open a position on stale/crashed-price data.
    process_events([_ev(W3)], tracker, engine, store, None, meta)
    assert store.all() == []                       # must NOT reopen from stale buffer


def test_wallets_json_required(tmp_path, monkeypatch):
    import src.agent.copy_trade.monitor as mon
    monkeypatch.setattr(mon, "WALLETS_PATH", tmp_path / "missing.json")
    try:
        mon._load_wallets()
        assert False, "should raise"
    except SystemExit:
        pass
