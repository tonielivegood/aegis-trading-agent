"""TDD for the best-execution router: quote every backend live, rank by output."""
from __future__ import annotations

from dataclasses import dataclass

from src.agent.execution import best_execution as be


@dataclass
class _PancakeQuote:
    expected_out_wei: int


def test_ranks_by_highest_output(mocker):
    oneinch = mocker.Mock()
    oneinch._quote_out_wei.return_value = int(9.5 * 10**18)
    openocean = mocker.Mock()
    openocean.quote.return_value = {"outAmount": str(int(10.2 * 10**18))}
    pancake = mocker.Mock()
    pancake.quote.return_value = _PancakeQuote(expected_out_wei=int(9.0 * 10**18))

    ranked = be.rank_backends(
        {"oneinch": oneinch, "openocean": openocean, "pancake": pancake},
        "USDT", "ETH", 10.0)
    assert ranked == ["openocean", "oneinch", "pancake"]   # best output first


def test_failing_backend_dropped_not_crashed(mocker):
    oneinch = mocker.Mock()
    oneinch._quote_out_wei.side_effect = RuntimeError("no api key")
    openocean = mocker.Mock()
    openocean.quote.return_value = {"outAmount": str(int(5.0 * 10**18))}

    ranked = be.rank_backends({"oneinch": oneinch, "openocean": openocean}, "USDT", "ETH", 10.0)
    assert ranked == ["openocean"]


def test_all_backends_fail_returns_empty(mocker):
    oneinch = mocker.Mock()
    oneinch._quote_out_wei.side_effect = RuntimeError("boom")
    ranked = be.rank_backends({"oneinch": oneinch}, "USDT", "ETH", 10.0)
    assert ranked == []


def test_zero_output_treated_as_no_quote(mocker):
    oneinch = mocker.Mock()
    oneinch._quote_out_wei.return_value = 0
    ranked = be.rank_backends({"oneinch": oneinch}, "USDT", "ETH", 10.0)
    assert ranked == []


def test_unknown_backend_name_ignored(mocker):
    weird = mocker.Mock()
    ranked = be.rank_backends({"mystery": weird}, "USDT", "ETH", 10.0)
    assert ranked == []
