"""TDD for the OpenOcean aggregator executor — mocked HTTP + local signing, no network."""
from __future__ import annotations

import pytest

from src.agent.execution.openocean import OpenOcean


def _resp(mocker, data):
    r = mocker.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"code": 200, "data": data}
    return r


def _oo(mocker, dry_run=True, account=None):
    w3 = mocker.Mock()
    w3.eth.gas_price = 1_000_000_000  # 1 gwei
    return OpenOcean(w3=w3, account=account, dry_run=dry_run)


def test_price_impact_parses_percent(mocker):
    oo = _oo(mocker)
    mocker.patch("src.agent.execution.openocean.requests.get",
                 return_value=_resp(mocker, {"price_impact": "0.40%", "outAmount": "1"}))
    assert oo.price_impact("USDT", "ETH", 12.0) == pytest.approx(0.004)


def test_dry_run_swap_quotes_only_never_broadcasts(mocker):
    oo = _oo(mocker, dry_run=True)
    get = mocker.patch("src.agent.execution.openocean.requests.get",
                       return_value=_resp(mocker, {"outAmount": "5000", "price_impact": "0.1%"}))
    r = oo.swap("USDT", "ETH", 12.0)
    assert r.simulated is True and r.tx_hash is None
    assert r.expected_out_wei == 5000
    assert all("swap_quote" not in str(c) for c in get.call_args_list)   # quote only


def test_rejects_same_token_and_nonpositive(mocker):
    oo = _oo(mocker)
    with pytest.raises(ValueError):
        oo.swap("ETH", "ETH", 12.0)
    with pytest.raises(ValueError):
        oo.swap("USDT", "ETH", 0)


def test_live_swap_signs_and_sends_aggregator_calldata(mocker):
    acct = mocker.Mock()
    acct.address = "0x0000000000000000000000000000000000000001"
    signed = mocker.Mock()
    signed.raw_transaction = b"raw"
    acct.sign_transaction.return_value = signed
    oo = _oo(mocker, dry_run=False, account=acct)
    # huge allowance + balance → skip approve, no clamp
    oo.w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = 10**30
    oo.w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10**30
    oo.w3.eth.get_transaction_count.return_value = 7
    oo.w3.eth.send_raw_transaction.return_value = b"\xab\xcd"
    oo.w3.eth.wait_for_transaction_receipt.return_value.status = 1   # mined OK
    mocker.patch("src.agent.execution.openocean.requests.get", return_value=_resp(mocker, {
        "to": "0x6352a56caadC4F1E25CD6c75970Fa768A3304e64", "data": "0xdead",
        "value": "0", "estimatedGas": "250000", "outAmount": "9999"}))

    r = oo.swap("USDT", "ETH", 12.0)
    assert r.simulated is False and r.tx_hash is not None
    assert oo.w3.eth.send_raw_transaction.called
    sent = acct.sign_transaction.call_args[0][0]               # the tx we signed
    assert sent["data"] == "0xdead"                            # aggregator's own calldata
    assert sent["to"].lower().endswith("3304e64")              # routed to OpenOcean, not Pancake
    assert sent["value"] == 0                                  # ERC-20 input
