from unittest.mock import patch

import pytest

from src.agent.copy_trade.rug_check import passes_rug_check

TOKEN = "0x" + "a" * 40

_CLEAN = {
    "is_mintable": "0", "can_take_back_ownership": "0", "hidden_owner": "0",
    "owner_change_balance": "0", "transfer_pausable": "0",
    "slippage_modifiable": "0", "is_proxy": "0",
}


class FakeResp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self._status_ok:
            raise Exception("bad status")


def _goplus_payload(info: dict) -> dict:
    return {"result": {TOKEN.lower(): info}}


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_all_flags_clean_passes(mock_get):
    mock_get.return_value = FakeResp(_goplus_payload(_CLEAN))
    assert passes_rug_check(TOKEN) == (True, "")


@pytest.mark.parametrize("field,reason", [
    ("is_mintable", "mintable"),
    ("can_take_back_ownership", "fake_renounce"),
    ("hidden_owner", "hidden_owner"),
    ("owner_change_balance", "owner_can_edit_balance"),
    ("transfer_pausable", "transfer_pausable"),
    ("slippage_modifiable", "slippage_modifiable"),
    ("is_proxy", "upgradeable_proxy"),
])
@patch("src.agent.copy_trade.rug_check.requests.get")
def test_each_flag_blocks_with_its_own_reason(mock_get, field, reason):
    info = dict(_CLEAN)
    info[field] = "1"
    mock_get.return_value = FakeResp(_goplus_payload(info))
    assert passes_rug_check(TOKEN) == (False, reason)


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_no_record_for_token_blocks(mock_get):
    mock_get.return_value = FakeResp({"result": {}})
    assert passes_rug_check(TOKEN) == (False, "no_goplus_record")


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_network_error_blocks(mock_get):
    mock_get.side_effect = Exception("boom")
    assert passes_rug_check(TOKEN) == (False, "goplus_error")


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_bad_http_status_blocks(mock_get):
    mock_get.return_value = FakeResp({}, status_ok=False)
    assert passes_rug_check(TOKEN) == (False, "goplus_error")
