# tests/test_copy_trade_monitor_runtime.py
"""Runtime-safety regression tests for monitor.py, split out from
test_copy_trade_monitor.py (which stays focused on the events->cluster->engine
pipeline). These cover properties that were previously only verified by manual
code reading:

  1. `_build_runtime`'s shadow_mode -> executors gating: `executors` must be
     `None` in shadow mode and a real backend dict only when `shadow_mode` is
     `False` (a shadow engine must never be able to construct a signing
     executor).
  2. `run_scan`'s backlog-replay guard: `ChainEventSource` must always be built
     with `start_block=pool.latest_block()` — the CURRENT chain tip at process
     start — never a stale/cached block. (Real incident, 16/7: a fresh
     DRY_RUN instance replayed 25 historical txs into 9 phantom positions
     because this guard was missing.)
  3. The -70% valve stop-loss must email on close, exactly like a
     cluster-exit-vote close already does.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import src.agent.copy_trade.monitor as mon
from src.agent.copy_trade.positions import CopyPosition, PositionStore

SENTINEL_BLOCK = 12345
W = "0x" + "1" * 40


def _write_config(path, **overrides):
    cfg = {
        "shadow_mode": True,
        "slice_usd": 3.0,
        "total_budget_usd": 16.14,
        "min_wallets": 3,
        "exit_wallets": 2,
        "window_minutes": 15,
        "valve_drop_pct": 0.70,
        "poll_interval_seconds": 0,
        "rpc_endpoints": ["https://example-rpc.invalid"],
    }
    cfg.update(overrides)
    path.write_text(json.dumps({"copy_settings": cfg}), encoding="utf-8")


def _patch_paths(monkeypatch, tmp_path, config_path=None, wallets_path=None):
    monkeypatch.setattr(mon, "CONFIG_PATH", config_path or tmp_path / "config.json")
    monkeypatch.setattr(mon, "WALLETS_PATH", wallets_path or tmp_path / "wallets.json")
    monkeypatch.setattr(mon, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mon, "SHADOW_PATH", tmp_path / "shadow_positions.json")
    monkeypatch.setattr(mon, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(mon, "JOURNAL_PATH", tmp_path / "closed.jsonl")


def _mock_pool_and_source():
    """A patched RpcPool/ChainEventSource pair whose latest_block()/poll() never
    hit real infra. Returns (pool_instance, source_instance)."""
    pool_instance = MagicMock()
    pool_instance.latest_block.return_value = SENTINEL_BLOCK
    source_instance = MagicMock()
    source_instance.poll.return_value = []
    source_instance.last_processed = SENTINEL_BLOCK
    return pool_instance, source_instance


# ---------- Finding 1a: _build_runtime shadow_mode -> executors gating ----------

def test_build_runtime_shadow_mode_has_no_executors(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "SHADOW_PATH", tmp_path / "shadow_positions.json")
    monkeypatch.setattr(mon, "POSITIONS_PATH", tmp_path / "positions.json")
    _, _, engine = mon._build_runtime({"shadow_mode": True})
    assert engine._executors is None


def test_build_runtime_live_mode_builds_all_backends(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "SHADOW_PATH", tmp_path / "shadow_positions.json")
    monkeypatch.setattr(mon, "POSITIONS_PATH", tmp_path / "positions.json")
    # dry_run=True so no real private key is required (OneInch/OpenOcean/PancakeSwap
    # constructors are mocked below anyway since their __init__ calls get_web3(),
    # which makes a real network connection attempt).
    monkeypatch.setattr(mon.settings, "dry_run", True)
    with patch("src.agent.copy_trade.monitor.OneInch") as m_1inch, \
         patch("src.agent.copy_trade.monitor.OpenOcean") as m_oo, \
         patch("src.agent.copy_trade.monitor.PancakeSwap") as m_pcs:
        _, _, engine = mon._build_runtime({"shadow_mode": False})
    assert engine._executors is not None
    assert set(engine._executors) == {"1inch", "openocean", "pancake"}
    m_1inch.assert_called_once()
    m_oo.assert_called_once()
    m_pcs.assert_called_once()


# ---------- Finding I2: budget reconciliation must use actual usd_size, not a
# ---------- replayed slice-count ----------

def test_build_runtime_reconciles_budget_from_actual_position_sizes(tmp_path, monkeypatch):
    """Positions were opened back when slice_usd was 1.5 (2 positions => $3.0 spent).
    The CURRENT config (loaded here) has since been edited to slice_usd=3.0 — a
    documented live-config-edit practice on the VPS. Reconciliation must use the
    positions' own stored usd_size ($1.5 each, $3.0 total), NOT replay
    can_open_new()/allocate() at the new slice_usd (which would allocate 1 slice
    of $3.0 for 2 loaded positions and leave available_usd wrong)."""
    shadow_path = tmp_path / "shadow_positions.json"
    monkeypatch.setattr(mon, "SHADOW_PATH", shadow_path)
    monkeypatch.setattr(mon, "POSITIONS_PATH", tmp_path / "positions.json")

    store = PositionStore(shadow_path)
    for i in range(2):
        store.open_position(CopyPosition(
            token_symbol=f"GEM{i}", token_address="0x" + str(i) * 40, token_decimals=18,
            source_wallet="", usd_size=1.5, token_amount=1.5,
            opened_at="2026-07-16T00:00:00Z",
            cluster_wallets=[W], entry_price_usd=1.0, simulated=True,
            first_price_usd=1.0))

    budget, _, _ = mon._build_runtime({
        "shadow_mode": True, "total_budget_usd": 16.14, "slice_usd": 3.0})

    assert budget.available_usd == pytest.approx(16.14 - 3.0)   # 16.14 - sum(usd_size)


# ---------- Finding 1b: run_scan backlog-replay guard ----------

def test_run_scan_uses_current_block_never_stale(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(config_path)
    wallets_path = tmp_path / "wallets.json"
    wallets_path.write_text(json.dumps([{"address": W}]), encoding="utf-8")
    _patch_paths(monkeypatch, tmp_path, config_path, wallets_path)

    pool_instance, source_instance = _mock_pool_and_source()

    with patch("src.agent.copy_trade.monitor.RpcPool", return_value=pool_instance), \
         patch("src.agent.copy_trade.monitor.ChainEventSource",
               return_value=source_instance) as mock_ces, \
         patch("src.agent.copy_trade.monitor.EmailNotifier", side_effect=ValueError):
        mon.run_scan(once=True)

    assert mock_ces.call_args.kwargs["start_block"] == SENTINEL_BLOCK
    pool_instance.latest_block.assert_called()


# ---------- Finding 2: valve-triggered close must notify ----------

def test_valve_close_sends_notification(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    _write_config(config_path)
    wallets_path = tmp_path / "wallets.json"
    wallets_path.write_text(json.dumps([{"address": W}]), encoding="utf-8")
    shadow_path = tmp_path / "shadow_positions.json"
    _patch_paths(monkeypatch, tmp_path, config_path, wallets_path)
    monkeypatch.setattr(mon, "SHADOW_PATH", shadow_path)

    pos = CopyPosition(
        token_symbol="GEM", token_address="0x" + "b" * 40, token_decimals=18,
        source_wallet="", usd_size=3.0, token_amount=3.0,
        opened_at="2026-07-16T00:00:00Z",
        cluster_wallets=["0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40],
        entry_price_usd=1.0, simulated=True, first_price_usd=1.0)
    PositionStore(shadow_path).open_position(pos)   # pre-seed an open position

    pool_instance, source_instance = _mock_pool_and_source()
    mock_notifier = MagicMock()

    with patch("src.agent.copy_trade.monitor.RpcPool", return_value=pool_instance), \
         patch("src.agent.copy_trade.monitor.ChainEventSource",
               return_value=source_instance), \
         patch("src.agent.copy_trade.monitor.EmailNotifier",
               return_value=mock_notifier), \
         patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=0.2), \
         patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0)):
        mon.run_scan(once=True)   # entry=1.0, price=0.2 <= entry*(1-0.70) -> valve fires

    reloaded = PositionStore(shadow_path)
    reloaded.load()
    assert reloaded.all() == []                     # valve actually closed it
    assert mock_notifier.send_alert.called
    subject = mock_notifier.send_alert.call_args.args[0]
    assert "VALVE" in subject
    assert pos.token_address[:10] in subject
