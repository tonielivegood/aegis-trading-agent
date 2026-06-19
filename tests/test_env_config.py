"""Env-config + Binance Web3 safety tests.

Verifies the template carries no real secrets, real .env stays ignored, secrets
are masked in diagnostics, the Web3 layer fails safe without a key, execution/
broadcast flags default off, and Alpha market data is independent of Web3 exec.
No network.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.agent.config import Settings, mask_secret
from src.agent.execution import binance_web3 as bw

REPO = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / ".env.example"
GITIGNORE = REPO / ".gitignore"


# ----------------------------- .env.example hygiene -----------------------------

def test_env_example_exists_with_required_vars():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in ("BINANCE_WEB3_ENABLED", "BINANCE_WEB3_API_KEY", "BINANCE_WEB3_BASE_URL",
                "BINANCE_WEB3_QUOTE_ENABLED", "BINANCE_WEB3_EXECUTION_ENABLED",
                "BINANCE_WEB3_BROADCAST_ENABLED", "BINANCE_ALPHA_MARKET_DATA_ENABLED",
                "DRY_RUN", "STRATEGY_MODE"):
        assert f"{var}=" in text, f"{var} missing from .env.example"


def test_env_example_has_no_real_secrets():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # secret keys must be blank or obvious placeholders, never a real-looking value
    for line in text.splitlines():
        if line.startswith(("BINANCE_WEB3_API_KEY=", "BINANCE_WEB3_API_SECRET=",
                            "CMC_API_KEY=", "X_BEARER_TOKEN=", "TELEGRAM_BOT_TOKEN=")):
            val = line.split("=", 1)[1].strip()
            assert val == "" or val.startswith("your_"), f"placeholder leak: {line}"
    # no 0x-prefixed 64-hex private key committed in the template
    assert not re.search(r"0x[0-9a-fA-F]{64}", text)
    assert "DRY_RUN=true" in text


def test_real_env_is_gitignored():
    assert ".env" in GITIGNORE.read_text(encoding="utf-8").splitlines()


def test_runtime_dir_is_gitignored():
    assert "data/runtime/" in GITIGNORE.read_text(encoding="utf-8")


# ----------------------------- masking / diagnostics -----------------------------

def test_mask_secret_never_reveals_full_key():
    full = "abcdef0123456789secretZZZ"
    masked = mask_secret(full)
    assert full not in masked and masked == "abcdef...retZZZ"
    assert mask_secret("") == "<absent>"


def test_diagnostics_masks_keys_and_excludes_full_values():
    s = _settings(BINANCE_WEB3_API_KEY="abcdef0123456789secretZZZ", CMC_API_KEY="cmckeyXXXXXXXXXX")
    diag = s.diagnostics()
    blob = " ".join(diag.values())
    assert "abcdef0123456789secretZZZ" not in blob
    assert "cmckeyXXXXXXXXXX" not in blob
    assert diag["binance_web3_api_key"] == "abcdef...retZZZ"


# ----------------------------- safe defaults / fail-safe -----------------------------

def test_web3_flags_default_off():
    s = _settings()
    assert s.binance_web3_enabled is False
    assert s.binance_web3_quote_enabled is False
    assert s.binance_web3_execution_enabled is False
    assert s.binance_web3_broadcast_enabled is False


def test_alpha_market_data_independent_of_web3_execution():
    s = _settings(BINANCE_ALPHA_MARKET_DATA_ENABLED="true", BINANCE_WEB3_EXECUTION_ENABLED="false")
    assert s.binance_alpha_market_data_enabled is True
    assert s.binance_web3_execution_enabled is False


def test_missing_web3_key_fails_safe(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    r = bw.connectivity_check()
    assert r.has_key is False and r.reachable is False and "not set" in r.detail


def test_binance_web3_module_has_no_sign_or_broadcast():
    for forbidden in ("sign", "broadcast", "send_transaction", "swap", "execute"):
        assert not hasattr(bw, forbidden)


# ----------------------------- helpers -----------------------------

def _settings(**env) -> Settings:
    """Build a Settings with required fields filled + optional env overrides."""
    base = dict(
        agent_private_key="0x" + "1" * 64, agent_wallet_address="0xabc",
        bsc_rpc_url="x", hackathon_contract="0xabc", pancake_router="0xabc",
        wbnb_address="0xabc", usdt_address="0xabc", bscscan_api_key="",
        cmc_api_key=env.pop("CMC_API_KEY", ""),
        binance_web3_api_key=env.pop("BINANCE_WEB3_API_KEY", ""),
        binance_web3_enabled=env.pop("BINANCE_WEB3_ENABLED", "false") in ("true", "1", "yes"),
        binance_web3_quote_enabled=env.pop("BINANCE_WEB3_QUOTE_ENABLED", "false") in ("true", "1"),
        binance_web3_execution_enabled=env.pop("BINANCE_WEB3_EXECUTION_ENABLED", "false") in ("true", "1"),
        binance_web3_broadcast_enabled=env.pop("BINANCE_WEB3_BROADCAST_ENABLED", "false") in ("true", "1"),
        binance_alpha_market_data_enabled=env.pop("BINANCE_ALPHA_MARKET_DATA_ENABLED", "true") in ("true", "1"),
    )
    return Settings(**base)
