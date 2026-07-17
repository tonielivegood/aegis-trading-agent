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


# ---------- v3 wiring ----------
import json as _json


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_same_batch_sell_kills_stale_cluster(_s, _mp, _ep, _t, tmp_path):
    """The 9-second-round-trip fix: 3 buys AND 2 sells of the same cluster arrive
    in ONE poll batch → the signal is already dead, no position may open."""
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    batch = [_ev(W1), _ev(W2), _ev(W3),
             _ev(W1, direction="out"), _ev(W2, direction="out")]
    process_events(batch, tracker, engine, store, None, meta)
    assert store.all() == []                       # signal dead-on-arrival — skipped


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_observe_only_wallet_never_votes(_s, _mp, _ep, _t, tmp_path):
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    voting = {W1.lower(), W2.lower()}              # W3 is observe-only
    process_events([_ev(W1), _ev(W2), _ev(W3)], tracker, engine, store, None,
                   meta, voting=voting)
    assert store.all() == []                       # 2 votes only — no cluster
    process_events([_ev(W4)], tracker, engine, store, None, meta,
                   voting={W1.lower(), W2.lower(), W4.lower()})
    assert len(store.all()) == 1                   # 3rd real vote fires it


def test_load_wallets_splits_watch_and_voting(tmp_path, monkeypatch):
    import src.agent.copy_trade.monitor as mon
    wf = tmp_path / "wallets.json"
    wf.write_text(_json.dumps([
        {"address": "0x" + "1" * 40},
        {"address": "0x" + "2" * 40, "observe_only": True},
        {"address": "0x" + "3" * 40, "observe_only": False},
    ]))
    monkeypatch.setattr(mon, "WALLETS_PATH", wf)
    watch, voting = mon._load_wallets()
    assert len(watch) == 3                         # all watched (data collection)
    assert voting == {"0x" + "1" * 40, "0x" + "3" * 40}


def test_append_wallet_events_writes_jsonl(tmp_path, monkeypatch):
    import src.agent.copy_trade.monitor as mon
    out = tmp_path / "wallet_events.jsonl"
    monkeypatch.setattr(mon, "WALLET_EVENTS_PATH", out)
    mon._append_wallet_events([_ev(W1), _ev(W2, direction="out")])
    rows = [_json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["wallet"] == W1 and rows[0]["direction"] == "in"
    assert rows[1]["direction"] == "out" and "ts" in rows[1] and "block" in rows[1]
    mon._append_wallet_events([])                  # empty batch: no crash, no write
    assert len(out.read_text().splitlines()) == 2


# ---------- notification reason must reflect the real close cause (Task 3 review
# finding: check_exits() can now close for reason="trail", not just "valve") ----------

def test_trail_close_notification_says_trail_not_valve(tmp_path, monkeypatch):
    import json as jsonlib
    from unittest.mock import MagicMock
    import src.agent.copy_trade.monitor as mon
    from src.agent.copy_trade.positions import CopyPosition, PositionStore

    W = "0x" + "1" * 40
    config_path = tmp_path / "config.json"
    config_path.write_text(jsonlib.dumps({"copy_settings": {
        "shadow_mode": True, "slice_usd": 3.0, "total_budget_usd": 16.14,
        "min_wallets": 3, "exit_wallets": 2, "window_minutes": 15,
        "valve_drop_pct": 0.70, "trail_pct": 0.2, "poll_interval_seconds": 0,
        "rpc_endpoints": ["https://example-rpc.invalid"],
    }}), encoding="utf-8")
    wallets_path = tmp_path / "wallets.json"
    wallets_path.write_text(jsonlib.dumps([{"address": W}]), encoding="utf-8")
    shadow_path = tmp_path / "shadow_positions.json"
    monkeypatch.setattr(mon, "CONFIG_PATH", config_path)
    monkeypatch.setattr(mon, "WALLETS_PATH", wallets_path)
    monkeypatch.setattr(mon, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mon, "SHADOW_PATH", shadow_path)
    monkeypatch.setattr(mon, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(mon, "JOURNAL_PATH", tmp_path / "closed.jsonl")

    # HWM=2.0, entry=1.0: a price of 1.5 stays well above the valve floor (0.3)
    # but trips the 20% trail off the high-water mark (1.6).
    pos = CopyPosition(
        token_symbol="GEM", token_address="0x" + "b" * 40, token_decimals=18,
        source_wallet="", usd_size=3.0, token_amount=3.0,
        opened_at="2026-07-16T00:00:00Z",
        cluster_wallets=[W, "0x" + "2" * 40, "0x" + "3" * 40],
        entry_price_usd=1.0, simulated=True, first_price_usd=1.0,
        high_water_usd=2.0)
    PositionStore(shadow_path).open_position(pos)

    pool_instance = MagicMock()
    pool_instance.latest_block.return_value = 999
    source_instance = MagicMock()
    source_instance.poll.return_value = []
    source_instance.last_processed = 999
    mock_notifier = MagicMock()

    with patch("src.agent.copy_trade.monitor.RpcPool", return_value=pool_instance), \
         patch("src.agent.copy_trade.monitor.ChainEventSource",
               return_value=source_instance), \
         patch("src.agent.copy_trade.monitor.EmailNotifier",
               return_value=mock_notifier), \
         patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.5), \
         patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0)):
        mon.run_scan(once=True)   # trail fires, valve does not

    reloaded = PositionStore(shadow_path)
    reloaded.load()
    assert reloaded.all() == []                     # trail actually closed it
    assert mock_notifier.send_alert.called
    subject = mock_notifier.send_alert.call_args.args[0]
    body = mock_notifier.send_alert.call_args.args[1]
    assert "TRAIL" in subject.upper() and "VALVE" not in subject.upper()
    assert "trail" in body.lower() and "valve" not in body.lower()
