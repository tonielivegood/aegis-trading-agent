"""Edge-case tests for RPC failover candidate selection and config validation
(added in the polish pass)."""
from __future__ import annotations

import pytest

from src.agent.data import rpc


def test_candidates_put_configured_url_first_and_dedup(mocker):
    mocker.patch.object(rpc.settings, "bsc_rpc_url", "https://bsc-dataseed.binance.org/")
    c = rpc._candidates()
    assert c[0] == "https://bsc-dataseed.binance.org/"
    assert len(c) == len(set(c))  # configured url is also a fallback -> must not duplicate


def test_candidates_custom_paid_url_first(mocker):
    mocker.patch.object(rpc.settings, "bsc_rpc_url", "https://paid-rpc.example/")
    c = rpc._candidates()
    assert c[0] == "https://paid-rpc.example/"
    assert "https://bsc-dataseed.binance.org/" in c  # public fallbacks still present


def test_candidates_skip_empty_configured_url(mocker):
    mocker.patch.object(rpc.settings, "bsc_rpc_url", "")
    c = rpc._candidates()
    assert "" not in c
    assert len(c) >= 5


# --- config ---

def test_get_raises_on_missing_required(monkeypatch):
    from src.agent.config import _get
    monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
    with pytest.raises(RuntimeError):
        _get("DEFINITELY_NOT_SET_XYZ")
    assert _get("DEFINITELY_NOT_SET_XYZ", "fallback") == "fallback"


_VALID_KW = dict(
    agent_wallet_address="0x0", bsc_rpc_url="x", hackathon_contract="x",
    pancake_router="x", wbnb_address="x", usdt_address="x",
    bscscan_api_key="x", cmc_api_key="x",
)


def test_settings_rejects_placeholder_private_key():
    from src.agent.config import Settings
    with pytest.raises(Exception):
        Settings(agent_private_key="PASTE_YOUR_PRIVATE_KEY_HERE", **_VALID_KW)


def test_settings_adds_0x_prefix_to_key():
    from src.agent.config import Settings
    s = Settings(agent_private_key="abc123", **_VALID_KW)
    assert s.agent_private_key == "0xabc123"
