"""Binance Web3 API connectivity-layer tests.

Verifies the SAFE contract: key read from env only, masked in output, harmless
GET probe, no signing/broadcasting, never raises. No network is touched.
"""
from __future__ import annotations

from src.agent.execution import binance_web3 as bw


# ----------------------------- secret masking -----------------------------

def test_mask_absent():
    assert bw.mask_secret("") == "<absent>"
    assert bw.mask_secret(None) == "<absent>"


def test_mask_short_is_fully_hidden():
    assert bw.mask_secret("shortkey123") == "***"  # <= 12 chars


def test_mask_long_shows_only_edges():
    masked = bw.mask_secret("abcdef0123456789xyz789")
    assert masked == "abcdef...xyz789"
    assert "0123456789" not in masked  # the middle is never shown


# ----------------------------- connectivity -----------------------------

def test_no_key_reports_missing(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    r = bw.connectivity_check()
    assert r.has_key is False and r.reachable is False
    assert "not set" in r.detail


def test_reachable_on_200(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    resp = mocker.Mock(status_code=200)
    get = mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    r = bw.connectivity_check()
    assert r.has_key and r.reachable and r.status == 200
    # the FULL key must never be passed in a way that would log it — only header
    sent_headers = get.call_args.kwargs["headers"]
    assert sent_headers[bw._AUTH_HEADER] == "abcdef0123456789xyz789"


def test_network_error_is_caught_not_raised(mocker):
    import requests
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    mocker.patch("src.agent.execution.binance_web3.requests.get",
                 side_effect=requests.ConnectionError("boom"))
    r = bw.connectivity_check()  # must not raise
    assert r.has_key and r.reachable is False
    assert "unreachable" in r.detail


def test_non_200_reported(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    resp = mocker.Mock(status_code=403)
    mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    r = bw.connectivity_check()
    assert r.has_key and r.reachable is False and r.status == 403


def test_module_has_no_sign_or_broadcast():
    # Safety-by-construction: this layer must expose no signing/broadcast surface.
    for forbidden in ("sign", "broadcast", "send_transaction", "swap", "execute"):
        assert not hasattr(bw, forbidden), f"binance_web3 must not expose {forbidden}()"
