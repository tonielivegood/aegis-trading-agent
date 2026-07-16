"""Restart-reconciliation tests for the copy-trade monitor (findings C2 + C3).

`_reconcile_after_restart` is the small extracted helper that `_build_runtime` runs
after loading positions from disk. It must, for every open position: re-register its
token in the RAM-only registry (so a mirror-sell can still resolve it after a restart)
and re-consume its budget slice (so we never over-allocate past the $15.39 hard cap).
"""
import pytest

from src.agent.copy_trade import monitor
from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.positions import CopyPosition, PositionStore
from src.agent.data.token_list import get_token, is_discovered

# A discovered (non-static) token with a valid 40-hex contract so Token.address resolves.
RECOVER_ADDR = "0x" + "de" * 20


def _open_position_on_disk(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(CopyPosition(
        token_symbol="RECOVERGEM", token_address=RECOVER_ADDR, token_decimals=9,
        source_wallet="0xshark1", usd_size=1.5, token_amount=42.0,
        opened_at="2026-07-15T10:00:00Z",
    ))
    # Simulate a fresh process: brand-new store, empty RAM registry + fresh budget.
    reloaded = PositionStore(path)
    reloaded.load()
    return reloaded


def test_reconcile_reregisters_token_after_restart(tmp_path):
    store = _open_position_on_disk(tmp_path)
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)

    assert not is_discovered("RECOVERGEM")  # RAM registry empty before reconcile
    monitor._reconcile_after_restart(budget, store)

    # (a) token re-registered → get_token() resolves without raising (mirror-sell can exit)
    assert is_discovered("RECOVERGEM")
    assert get_token("RECOVERGEM").address is not None


def test_reconcile_replays_budget_allocation_after_restart(tmp_path):
    store = _open_position_on_disk(tmp_path)
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)

    monitor._reconcile_after_restart(budget, store)

    # (b) budget reduced by exactly the one open position's slice — no double-spend past cap
    assert budget.available_usd == pytest.approx(15.39 - 1.5)


def test_load_json_state_tolerates_missing_file(tmp_path):
    """Finding 2: state.json is gitignored, so a fresh VPS checkout won't have one.
    Loading it with a default must return an empty state instead of crashing run_scan
    with FileNotFoundError before the bot ever ticks."""
    missing = tmp_path / "state.json"  # never created
    assert not missing.exists()

    state = monitor._load_json(missing, default=monitor._default_state())

    # Shape the rest of monitor.py relies on: check_wallet writes state["last_checked"][addr]
    assert state["last_checked"] == {}
    assert state.get("processed_txs") == []
    assert state.get("alerts") == []


def test_load_json_config_still_raises_on_missing_file(tmp_path):
    """Finding 2 guard-rail: a MISSING config is a real error (no default passed), not a
    fresh-deploy condition — it must still surface, not be swallowed."""
    with pytest.raises(FileNotFoundError):
        monitor._load_json(tmp_path / "config.json")
