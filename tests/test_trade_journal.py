"""TDD for the append-only trade journal (win-rate / PnL evaluation data source)."""
from __future__ import annotations

import json

import pytest

from src.agent.aegis import trade_journal as tj


def test_record_entry_writes_one_json_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_entry(path, symbol="SPCX", token_class="meme", entry_price=4.88e-06,
                    usd_size=5.10, reason="breakout vol 0.0x +14.3%", backend="1inch",
                    tx="0xabc", time_iso="2026-07-02T00:16:19Z")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "entry" and row["symbol"] == "SPCX" and row["usd_size"] == 5.10


def test_record_exit_computes_pnl(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_exit(path, symbol="FOO", token_class="meme", entry_price=1.0, exit_price=1.4,
                   usd_size=10.0, hold_minutes=42.0, reason="aegis exit: hard TP 1.4x",
                   backend="pancake", tx="0xdef", time_iso="2026-07-02T01:00:00Z")
    rows = tj.read_all(path)
    assert len(rows) == 1
    assert rows[0]["pnl_pct"] == pytest.approx(0.4)  # exact: (1.4/1.0 - 1)
    assert rows[0]["pnl_usd"] == pytest.approx(4.0)
    assert rows[0]["hold_minutes"] == 42.0


def test_record_exit_handles_zero_entry_price(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_exit(path, symbol="ZERO", token_class="meme", entry_price=0.0, exit_price=1.0,
                   usd_size=5.0, hold_minutes=1.0, reason="x", backend="pancake", tx=None,
                   time_iso="2026-07-02T00:00:00Z")
    rows = tj.read_all(path)
    assert rows[0]["pnl_pct"] == 0.0 and rows[0]["pnl_usd"] == 0.0


def test_read_all_returns_empty_list_for_missing_file(tmp_path):
    assert tj.read_all(tmp_path / "does_not_exist.jsonl") == []


def test_read_all_skips_malformed_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"event": "entry", "symbol": "OK"}\nnot json\n\n', encoding="utf-8")
    rows = tj.read_all(path)
    assert rows == [{"event": "entry", "symbol": "OK"}]


def test_report_computes_win_rate_and_pnl():
    path_rows = [
        {"event": "exit", "pnl_usd": 2.0, "pnl_pct": 0.20},
        {"event": "exit", "pnl_usd": -1.0, "pnl_pct": -0.05},
        {"event": "entry", "symbol": "IGNORED"},   # entries are not counted in win-rate
        {"event": "exit", "pnl_usd": 3.0, "pnl_pct": 0.30},
    ]
    import src.agent.aegis.trade_journal as tj_mod
    orig = tj_mod.read_all
    tj_mod.read_all = lambda path: path_rows
    try:
        rep = tj.report("unused-path")
    finally:
        tj_mod.read_all = orig
    assert rep["n_trades"] == 3
    assert rep["win_rate"] == 2 / 3
    assert rep["total_pnl_usd"] == 4.0
    assert rep["worst_pnl_pct"] == -0.05


def test_report_empty_journal():
    import src.agent.aegis.trade_journal as tj_mod
    orig = tj_mod.read_all
    tj_mod.read_all = lambda path: []
    try:
        rep = tj.report("unused-path")
    finally:
        tj_mod.read_all = orig
    assert rep == {"n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
                   "worst_pnl_pct": None, "total_pnl_usd": 0.0}
