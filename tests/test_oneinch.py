"""TDD for the 1inch aggregator executor — mocked HTTP + local signing, no network."""
from __future__ import annotations

import pytest

from src.agent.execution.oneinch import OneInch


def _resp(mocker, body):
    r = mocker.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = body
    return r


def _oi(mocker, dry_run=True, account=None):
    w3 = mocker.Mock()
    w3.eth.gas_price = 1_000_000_000
    return OneInch(w3=w3, account=account, dry_run=dry_run, api_key="test-key")


def test_requires_api_key(mocker):
    w3 = mocker.Mock()
    oi = OneInch(w3=w3, dry_run=True, api_key="")
    with pytest.raises(RuntimeError):
        oi.swap("USDT", "ETH", 12.0)


def test_price_impact_from_fair_value(mocker):
    oi = _oi(mocker)
    # routed out 0.00597 ETH vs fair 0.006 (12 USDT @ $1 / ETH @ $2000) ≈ 0.5% impact
    mocker.patch("src.agent.execution.oneinch.requests.get",
                 return_value=_resp(mocker, {"dstAmount": str(int(0.00597 * 10**18))}))
    mocker.patch("src.agent.execution.oneinch.price_feed.onchain_price_usd",
                 side_effect=lambda s: {"USDT": 1.0, "ETH": 2000.0}[s])
    assert oi.price_impact("USDT", "ETH", 12.0) == pytest.approx(0.005, abs=2e-4)


def test_dry_run_swap_quotes_only(mocker):
    oi = _oi(mocker, dry_run=True)
    get = mocker.patch("src.agent.execution.oneinch.requests.get",
                       return_value=_resp(mocker, {"dstAmount": "5000"}))
    r = oi.swap("USDT", "ETH", 12.0)
    assert r.simulated is True and r.tx_hash is None and r.expected_out_wei == 5000
    assert all(c.args[0].endswith("/quote") for c in get.call_args_list)   # quote endpoint only


def test_live_swap_signs_1inch_calldata(mocker):
    acct = mocker.Mock()
    acct.address = "0x0000000000000000000000000000000000000001"
    signed = mocker.Mock()
    signed.raw_transaction = b"raw"
    acct.sign_transaction.return_value = signed
    oi = _oi(mocker, dry_run=False, account=acct)
    oi.w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = 10**30
    oi.w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10**30
    oi.w3.eth.get_transaction_count.return_value = 3
    oi.w3.eth.send_raw_transaction.return_value = b"\xab\xcd"
    mocker.patch("src.agent.execution.oneinch.requests.get", return_value=_resp(mocker, {
        "dstAmount": "9999",
        "tx": {"to": "0x111111125421cA6dc452d289314280a0f8842A65", "data": "0xbeef", "value": "0"}}))

    r = oi.swap("USDT", "ETH", 12.0)
    assert r.simulated is False and r.tx_hash is not None
    sent = acct.sign_transaction.call_args[0][0]
    assert sent["data"] == "0xbeef"
    assert sent["to"].lower().endswith("8842a65")          # 1inch AggregationRouterV6
