"""Binance Web3 API connectivity-layer tests.

Verifies the SAFE contract: key read from env only, masked in output, harmless
GET probe, no signing/broadcasting, never raises. No network is touched.
"""
from __future__ import annotations

import json

import requests

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


# ----------------------------- W3W region check -----------------------------

def test_request_signature_matches_reference_hmac():
    # Independent computation (not calling the module's own helper for the
    # payload assembly) — catches a payload-order or encoding regression.
    import base64
    import hashlib
    import hmac as hmac_mod
    expected = base64.b64encode(
        hmac_mod.new(b"mysecret", b"2026-06-27T10:00:00.000ZPOST/build/pathBODY",
                     hashlib.sha256).digest()
    ).decode()
    got = bw._request_signature("mysecret", "2026-06-27T10:00:00.000Z", "POST", "/build/path", "BODY")
    assert got == expected


def test_check_region_no_credentials_reports_missing(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    mocker.patch.object(bw, "_api_secret", return_value="")
    r = bw.check_region()
    assert r.has_credentials is False and r.ok is False
    assert "not set" in r.detail


def test_check_region_success_code_zero(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    mocker.patch.object(bw, "_api_secret", return_value="shhh")
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 0, "data": [{"price": "1.0001"}], "success": True}
    post = mocker.patch("src.agent.execution.binance_web3.requests.post", return_value=resp)
    r = bw.check_region()
    assert r.ok is True and r.api_code == 0
    # signed body sent must be the SAME string that was signed (no drift bug)
    assert post.call_args.kwargs["data"] == json.dumps(
        [{"binanceChainId": bw._BSC_CHAIN_ID, "tokenContractAddress": bw._USDT_BSC}],
        separators=(",", ":"))
    headers = post.call_args.kwargs["headers"]
    assert headers["X-OC-APIKEY"] == "abcdef0123456789xyz789"
    assert "X-OC-SIGN" in headers and "X-OC-TIMESTAMP" in headers


def test_check_region_compliance_block_reported(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    mocker.patch.object(bw, "_api_secret", return_value="shhh")
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 40304, "msg": "compliance restriction", "success": False}
    mocker.patch("src.agent.execution.binance_web3.requests.post", return_value=resp)
    r = bw.check_region()
    assert r.ok is False and r.api_code == 40304
    assert "BLOCKED" in r.detail


def test_check_region_network_error_is_caught_not_raised(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    mocker.patch.object(bw, "_api_secret", return_value="shhh")
    mocker.patch("src.agent.execution.binance_web3.requests.post",
                 side_effect=requests.ConnectionError("boom"))
    r = bw.check_region()  # must not raise
    assert r.ok is False and "unreachable" in r.detail


# ----------------------------- price_info (batch price/volume/liquidity) -----------------------------

def _creds(mocker):
    mocker.patch.object(bw, "_api_key", return_value="abcdef0123456789xyz789")
    mocker.patch.object(bw, "_api_secret", return_value="shhh")


def test_price_info_no_credentials_returns_empty(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    mocker.patch.object(bw, "_api_secret", return_value="")
    assert bw.price_info(["0xabc"]) == {}


def test_price_info_no_contracts_returns_empty(mocker):
    _creds(mocker)
    post = mocker.patch("src.agent.execution.binance_web3.requests.post")
    assert bw.price_info([]) == {}
    post.assert_not_called()


def test_price_info_keys_by_lowercased_contract(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 0, "msg": "success", "data": [
        {"tokenContractAddress": "0xABC123", "price": "1.5", "priceChange5M": "6.0",
         "volume5M": "1000.0", "liquidity": "50000.0"},
    ]}
    mocker.patch("src.agent.execution.binance_web3.requests.post", return_value=resp)
    out = bw.price_info(["0xABC123"])
    assert out["0xabc123"]["priceChange5M"] == "6.0"
    assert out["0xabc123"]["volume5M"] == "1000.0"


def test_price_info_chunks_batches_over_100(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 0, "msg": "success", "data": []}
    post = mocker.patch("src.agent.execution.binance_web3.requests.post", return_value=resp)
    contracts = [f"0x{i:040x}" for i in range(150)]
    bw.price_info(contracts)
    assert post.call_count == 2   # 100 + 50
    first_body = post.call_args_list[0].kwargs["data"]
    assert len(json.loads(first_body)) == 100
    second_body = post.call_args_list[1].kwargs["data"]
    assert len(json.loads(second_body)) == 50


def test_price_info_api_error_code_returns_empty_for_that_chunk(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 40411, "msg": "chain not supported"}
    mocker.patch("src.agent.execution.binance_web3.requests.post", return_value=resp)
    assert bw.price_info(["0xabc"]) == {}


def test_price_info_network_error_never_raises(mocker):
    _creds(mocker)
    mocker.patch("src.agent.execution.binance_web3.requests.post",
                 side_effect=requests.ConnectionError("boom"))
    assert bw.price_info(["0xabc"]) == {}   # must not raise


# ----------------------------- hot_token (server-side filtered discovery) -----------------------------

def test_hot_token_no_credentials_returns_empty(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    mocker.patch.object(bw, "_api_secret", return_value="")
    assert bw.hot_token() == []


def test_hot_token_returns_items(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 0, "msg": "success",
                              "data": {"page": None, "items": [{"tokenSymbol": "MYX"}]}}
    get = mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    out = bw.hot_token(price_change_percent_min=6.0, volume_min=1000.0)
    assert out == [{"tokenSymbol": "MYX"}]
    # safety filters are always sent, and the caller's numeric filters land as strings.
    sent_url = get.call_args.args[0]
    assert "isHideWashTradingTokens=true" in sent_url
    assert "isMint=true" in sent_url            # exclude_mint=True by default
    assert "priceChangePercentMin=6.0" in sent_url


def test_hot_token_api_error_returns_empty(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 50000, "msg": "internal error"}
    mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    assert bw.hot_token() == []


def test_hot_token_network_error_never_raises(mocker):
    _creds(mocker)
    mocker.patch("src.agent.execution.binance_web3.requests.get",
                 side_effect=requests.ConnectionError("boom"))
    assert bw.hot_token() == []


# ----------------------------- quote (just-in-time honeypot/tax/impact check) -----------------------------

def test_quote_no_credentials_returns_empty(mocker):
    mocker.patch.object(bw, "_api_key", return_value="")
    mocker.patch.object(bw, "_api_secret", return_value="")
    assert bw.quote("0xfrom", "0xto", "1000000000000000000") == []


def test_quote_returns_routes_with_honeypot_and_tax_fields(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 0, "msg": "success", "data": [
        {"quoteId": "abc", "vendorName": "LiquidMesh", "isBest": True,
         "toToken": {"tokenSymbol": "MYX", "isHoneyPot": False, "taxRate": "0"}},
    ]}
    get = mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    routes = bw.quote("0xfrom", "0xto", "5000000000000000000")
    assert routes[0]["toToken"]["isHoneyPot"] is False
    sent_url = get.call_args.args[0]
    assert "fromTokenAddress=0xfrom" in sent_url
    assert "amount=5000000000000000000" in sent_url


def test_quote_api_error_returns_empty(mocker):
    _creds(mocker)
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"code": 40401, "msg": "quote expired"}
    mocker.patch("src.agent.execution.binance_web3.requests.get", return_value=resp)
    assert bw.quote("0xfrom", "0xto", "1") == []


def test_quote_network_error_never_raises(mocker):
    _creds(mocker)
    mocker.patch("src.agent.execution.binance_web3.requests.get",
                 side_effect=requests.ConnectionError("boom"))
    assert bw.quote("0xfrom", "0xto", "1") == []
