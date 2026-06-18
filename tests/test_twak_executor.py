"""TWAK executor tests — written test-first (TDD), updated for CLI v0.19.1.

The TWAK executor is an alternative execution backend that signs/broadcasts via
the Trust Wallet Agent Kit CLI (`twak swap ... --chain bsc --json`). It must keep
the SAME safety rails as the PancakeSwap path:
  - only curated/whitelisted tokens (KeyError otherwise)
  - positive amount, distinct tokens
  - dry-run NEVER broadcasts (uses --quote-only, no password in args)
  - no shell=True; args passed as a list (no command injection)
  - defensive JSON parsing (tolerate schema variation across CLI versions)

CLI schema confirmed v0.19.1:
  - tokens are SYMBOLS (not contract addresses)
  - WBNB maps to "BNB" in TWAK registry
  - --password <pw> flag required for live execution (not env var)
  - auth via TWAK_ACCESS_ID + TWAK_HMAC_SECRET env vars
No test touches the network, the CLI, or real credentials.
"""
from __future__ import annotations

import json

import pytest

from src.agent.execution.twak_executor import TwakExecutor, TwakError


def _exec(dry_run=True):
    return TwakExecutor(dry_run=dry_run, password="x")


# ----------------------------- arg building -----------------------------

def test_build_args_uses_symbols_chain_and_json():
    ex = _exec()
    args = ex._build_args("USDT", "WBNB", 3.5, quote_only=True)
    assert args[0] == "twak" and args[1] == "swap"
    assert "--chain" in args and "bsc" in args
    assert "--json" in args
    assert "--quote-only" in args
    # tokens passed as SYMBOLS (TWAK v0.19.1 uses BSC symbol registry)
    assert "USDT" in args
    assert "BNB" in args   # WBNB mapped to BNB for TWAK registry
    assert "3.5" in args


def test_build_args_maps_wbnb_to_bnb():
    ex = _exec()
    args = ex._build_args("WBNB", "USDT", 1.0, quote_only=True)
    assert "BNB" in args
    assert "WBNB" not in args


def test_build_args_passes_slippage_as_percent():
    ex = _exec()
    ex.slippage_bps = 50  # 0.5%
    args = ex._build_args("USDT", "WBNB", 1.0, quote_only=False)
    assert "--slippage" in args
    i = args.index("--slippage")
    assert float(args[i + 1]) == pytest.approx(0.5)
    assert "--quote-only" not in args  # live execution


# ----------------------------- validation -----------------------------

def test_rejects_unknown_token():
    ex = _exec()
    with pytest.raises(KeyError):
        ex.swap("NOTAREAL", "USDT", 1.0)


def test_rejects_same_token():
    ex = _exec()
    with pytest.raises(ValueError):
        ex.swap("USDT", "USDT", 1.0)


def test_rejects_nonpositive_amount():
    ex = _exec()
    with pytest.raises(ValueError):
        ex.swap("USDT", "WBNB", 0.0)
    with pytest.raises(ValueError):
        ex.swap("USDT", "WBNB", -1.0)


# ----------------------------- dry-run safety -----------------------------

def test_dry_run_uses_quote_only_and_does_not_broadcast(mocker):
    ex = _exec(dry_run=True)
    run = mocker.patch.object(ex, "_run", return_value={"amountOut": "1.0"})
    result = ex.swap("USDT", "WBNB", 2.0)
    assert result.simulated is True
    assert result.tx_hash is None
    # the call it made must be a quote, never a broadcast
    args = run.call_args.args[0]
    assert "--quote-only" in args


# ----------------------------- live execution -----------------------------

def test_live_swap_broadcasts_and_parses_tx_hash(mocker):
    ex = _exec(dry_run=False)
    mocker.patch.object(ex, "_run",
                        return_value={"txHash": "0xabc", "status": "success"})
    result = ex.swap("USDT", "WBNB", 2.0)
    assert result.simulated is False
    assert result.tx_hash == "0xabc"
    args = ex._run.call_args.args[0]
    assert "--quote-only" not in args


@pytest.mark.parametrize("field", ["txHash", "tx_hash", "hash", "transactionHash"])
def test_tx_hash_parsed_from_alternate_field_names(field):
    ex = _exec(dry_run=False)
    assert ex._extract_tx_hash({field: "0xdead"}) == "0xdead"


def test_extract_tx_hash_none_when_absent():
    ex = _exec(dry_run=False)
    assert ex._extract_tx_hash({"status": "success"}) is None


# ----------------------------- subprocess hardening -----------------------------

def test_run_rejects_nonzero_exit(mocker):
    ex = _exec(dry_run=False)
    proc = mocker.Mock(returncode=1, stdout="", stderr="boom")
    mocker.patch("src.agent.execution.twak_executor.subprocess.run", return_value=proc)
    with pytest.raises(TwakError):
        ex._run(["twak", "swap"])


def test_run_rejects_non_json_output(mocker):
    ex = _exec(dry_run=False)
    proc = mocker.Mock(returncode=0, stdout="not json", stderr="")
    mocker.patch("src.agent.execution.twak_executor.subprocess.run", return_value=proc)
    with pytest.raises(TwakError):
        ex._run(["twak", "swap"])


def test_run_parses_json_and_never_uses_shell(mocker):
    ex = _exec(dry_run=False)
    proc = mocker.Mock(returncode=0, stdout=json.dumps({"txHash": "0x1"}), stderr="")
    run = mocker.patch("src.agent.execution.twak_executor.subprocess.run", return_value=proc)
    out = ex._run(["twak", "swap", "--json"])
    assert out == {"txHash": "0x1"}
    assert run.call_args.kwargs.get("shell", False) is False


def test_dry_run_has_no_password_in_args():
    ex = TwakExecutor(dry_run=True, password="secret-pw")
    args = ex._build_args("USDT", "WBNB", 1.0, quote_only=True)
    # dry-run (quote-only) never needs or passes the wallet password
    assert "--password" not in args
    assert "secret-pw" not in args


def test_live_swap_includes_password_flag():
    ex = TwakExecutor(dry_run=False, password="secret-pw")
    args = ex._build_args("USDT", "WBNB", 1.0, quote_only=False)
    # live execution: TWAK CLI requires --password flag (not env var)
    assert "--password" in args
    i = args.index("--password")
    assert args[i + 1] == "secret-pw"
    assert "--quote-only" not in args


def test_twak_auth_passed_via_env(mocker):
    mocker.patch("src.agent.execution.twak_executor.settings")
    import src.agent.execution.twak_executor as twak_mod
    twak_mod.settings.twak_access_id = "acc-123"
    twak_mod.settings.twak_hmac_secret = "hmac-abc"
    twak_mod.settings.dry_run = False
    ex = TwakExecutor(dry_run=False, password="pw")
    proc = mocker.Mock(returncode=0, stdout=json.dumps({"txHash": "0x1"}), stderr="")
    run = mocker.patch("src.agent.execution.twak_executor.subprocess.run", return_value=proc)
    ex._run(["twak", "swap", "--json"])
    call_env = run.call_args.kwargs["env"]
    assert call_env["TWAK_ACCESS_ID"] == "acc-123"
    assert call_env["TWAK_HMAC_SECRET"] == "hmac-abc"
