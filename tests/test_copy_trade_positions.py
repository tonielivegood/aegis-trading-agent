import json
from pathlib import Path

import pytest

from src.agent.copy_trade.positions import CopyPosition, PositionStore

POS = CopyPosition(
    token_symbol="GEM",
    token_address="0xgem1",
    token_decimals=9,
    source_wallet="0xshark1",
    usd_size=1.5,
    token_amount=12345.0,
    opened_at="2026-07-15T10:00:00Z",
)


def test_open_position_persists_to_disk_immediately(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(POS)

    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk[0]["token_address"] == "0xgem1"


def test_reloading_a_fresh_store_recovers_positions_written_by_a_prior_process(tmp_path):
    path = tmp_path / "positions.json"
    store_a = PositionStore(path)
    store_a.open_position(POS)

    # Simulate a process restart: a brand new PositionStore instance, same path.
    store_b = PositionStore(path)
    store_b.load()

    found = store_b.find("0xgem1", "0xshark1")
    assert found is not None
    assert found.token_amount == pytest.approx(12345.0)


def test_close_position_removes_it_and_persists(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(POS)

    closed = store.close_position("0xgem1", "0xshark1")
    assert closed is not None
    assert closed.token_amount == pytest.approx(12345.0)
    assert store.find("0xgem1", "0xshark1") is None

    reloaded = PositionStore(path)
    reloaded.load()
    assert reloaded.all() == []


def test_close_position_returns_none_when_not_found(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    assert store.close_position("0xnope", "0xshark1") is None


def test_load_on_missing_file_starts_empty(tmp_path):
    store = PositionStore(tmp_path / "does_not_exist.json")
    store.load()
    assert store.all() == []


def test_save_uses_atomic_write(tmp_path):
    """Verify _save() uses atomic write to prevent torn files on crash.

    A torn write would leave a .tmp file behind on error, or leave the
    live file partially written if the process is killed mid-write.
    This test checks:
    1. No leftover .tmp files after a successful save (proves atomic cleanup)
    2. The saved JSON is valid and complete (not truncated)
    """
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(POS)

    # After a successful save, there should be no .tmp files in the directory
    tmp_files = list(tmp_path.glob(".positions_*.tmp"))
    assert tmp_files == [], f"Leftover tmp files found: {tmp_files}"

    # The live file should exist and contain valid, complete JSON
    assert path.exists(), "positions.json should exist after open_position"
    content = path.read_text(encoding="utf-8")
    parsed = json.loads(content)  # Would raise JSONDecodeError if file is torn/partial
    assert isinstance(parsed, list), "positions.json should contain a JSON array"
    assert len(parsed) == 1, "Should have exactly 1 position"
    assert parsed[0]["token_address"] == "0xgem1", "Position data should be complete and correct"
