"""The live-dashboard status snapshot must be valid JSON, public-safe (no secrets),
and fail-safe (never raise into a tick)."""
from __future__ import annotations

import json

from src.agent import agent_loop as al
from src.agent.config import settings
from src.agent.risk.drawdown import DrawdownTracker


def _run(tmp_path, mocker):
    mocker.patch.object(al, "WEB_DIR", tmp_path)
    mocker.patch.object(al, "STATUS_FILE", tmp_path / "status.json")
    mocker.patch.object(al, "REGIME_FILE", tmp_path / "regime.json")
    mocker.patch.object(al, "POSITIONS_FILE", tmp_path / "pos.json")
    mocker.patch.object(al.cmc_agent_hub, "get_fear_greed",
                        return_value={"value": 22, "classification": "Fear"})
    mocker.patch.object(al, "_load_trending", return_value=frozenset({"BTC", "SOL"}))
    dd = DrawdownTracker(0.20, 0.30)
    dd.update(34.0)
    al._write_status_snapshot(34.0, dd, -0.012, "sniper:risk_on", {"ETH": 2000.0},
                              [], dry_run=True, now=al.utcnow())
    return json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))


def test_snapshot_is_valid_and_public_safe(tmp_path, mocker):
    data = _run(tmp_path, mocker)
    assert data["mode"] == "DRY"
    assert data["equity"] == 34.0
    assert data["strategy"] == "sniper:risk_on"
    assert data["agent_hub"]["fear_greed"]["value"] == 22
    assert "BTC" in data["agent_hub"]["trending"]
    assert "breaker" in data and data["breaker"]["cap_pct"] == 30
    # NEVER leak a secret into the public dashboard file.
    blob = json.dumps(data)
    assert settings.agent_private_key not in blob
    assert settings.cmc_api_key not in blob


def test_snapshot_never_raises_on_bad_state(tmp_path, mocker):
    mocker.patch.object(al, "STATUS_FILE", tmp_path / "status.json")
    mocker.patch.object(al.cmc_agent_hub, "get_fear_greed", side_effect=RuntimeError("boom"))
    dd = DrawdownTracker(0.20, 0.30)
    dd.update(10.0)
    # Must swallow the error — a dashboard export can never break a trading tick.
    al._write_status_snapshot(10.0, dd, 0.0, "sniper", {}, [], dry_run=False, now=al.utcnow())
