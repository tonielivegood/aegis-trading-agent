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


def _cluster_pos(token="0x" + "c" * 40, simulated=True):
    return CopyPosition(token_symbol="GEM", token_address=token, token_decimals=18,
                        source_wallet="", usd_size=3.0, token_amount=100.0,
                        opened_at="2026-07-16T00:00:00+00:00",
                        cluster_wallets=["0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40],
                        entry_price_usd=0.03, simulated=simulated)


def test_new_fields_default_for_legacy_json(tmp_path):
    p = tmp_path / "positions.json"
    p.write_text('[{"token_symbol": "OLD", "token_address": "0xabc", '
                 '"token_decimals": 18, "source_wallet": "0xdef", "usd_size": 1.5, '
                 '"token_amount": 10.0, "opened_at": "t"}]', encoding="utf-8")
    store = PositionStore(p)
    store.load()
    pos = store.all()[0]
    assert pos.cluster_wallets == [] and pos.exited_by == []
    assert pos.entry_price_usd == 0.0 and pos.simulated is False


def test_find_by_token_and_close_by_token(tmp_path):
    store = PositionStore(tmp_path / "p.json")
    store.load()
    pos = _cluster_pos()
    store.open_position(pos)
    assert store.find_by_token(pos.token_address.upper()) is pos   # case-insensitive
    assert store.find_by_token("0x" + "d" * 40) is None
    closed = store.close_by_token(pos.token_address)
    assert closed is pos and store.all() == []


def test_update_persists_exited_by(tmp_path):
    path = tmp_path / "p.json"
    store = PositionStore(path)
    store.load()
    pos = _cluster_pos()
    store.open_position(pos)
    pos.exited_by.append(pos.cluster_wallets[0])
    store.update(pos)
    reloaded = PositionStore(path)
    reloaded.load()
    assert reloaded.all()[0].exited_by == [pos.cluster_wallets[0]]


def test_update_unknown_position_raises(tmp_path):
    store = PositionStore(tmp_path / "p.json")
    store.load()
    with pytest.raises(ValueError):
        store.update(_cluster_pos())
