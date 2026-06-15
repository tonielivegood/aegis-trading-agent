"""Execution layer tests — written test-first (TDD).

Encodes the execution threat model as tests:
  - slippage protection (min_out) is always derived from a live quote, never 0
  - only curated/whitelisted tokens can be swapped
  - dry-run NEVER broadcasts a transaction
  - token approval is for the exact amount, not unlimited
  - decimals are handled correctly (DOGE = 8, others = 18)
No test touches the network or a real key.
"""
from __future__ import annotations

import pytest

from src.agent.data import token_list
from src.agent.execution import tx_builder
from src.agent.execution.pancakeswap import PancakeSwap


# ----------------------------- pure helpers -----------------------------

def test_to_wei_handles_decimals():
    assert tx_builder.to_wei_amount(1.0, 18) == 10**18
    assert tx_builder.to_wei_amount(1.0, 8) == 10**8        # DOGE
    assert tx_builder.to_wei_amount(2.5, 18) == 25 * 10**17


def test_to_wei_rejects_bad_amount():
    with pytest.raises(ValueError):
        tx_builder.to_wei_amount(-1.0, 18)
    with pytest.raises(ValueError):
        tx_builder.to_wei_amount(float("nan"), 18)


def test_apply_slippage_reduces_min_out():
    # 0.5% slippage (50 bps) on 1000 → 995
    assert tx_builder.apply_slippage(1000, 50) == 995
    # min_out must always be strictly less than expected for a positive amount
    assert tx_builder.apply_slippage(10**18, 50) < 10**18


def test_apply_slippage_never_zero_for_positive():
    assert tx_builder.apply_slippage(10000, 50) > 0


def test_swap_deadline_is_short():
    now = 1_780_000_000
    dl = tx_builder.swap_deadline(now, seconds=120)
    assert dl == now + 120
    assert dl - now <= 300  # never a far-future deadline


# ----------------------------- routing -----------------------------

def _ps(mocker, dry_run=True):
    w3 = mocker.Mock()
    return PancakeSwap(w3=w3, dry_run=dry_run)


def test_build_path_direct_to_wbnb(mocker):
    ps = _ps(mocker)
    path = ps.build_path("CAKE", "WBNB")
    assert path == [token_list.get_token("CAKE").address, token_list.get_token("WBNB").address]


def test_build_path_routes_through_wbnb(mocker):
    ps = _ps(mocker)
    path = ps.build_path("CAKE", "USDT")
    assert path == [
        token_list.get_token("CAKE").address,
        token_list.get_token("WBNB").address,
        token_list.get_token("USDT").address,
    ]


def test_build_path_from_wbnb(mocker):
    ps = _ps(mocker)
    path = ps.build_path("WBNB", "CAKE")
    assert path == [token_list.get_token("WBNB").address, token_list.get_token("CAKE").address]


# ----------------------------- quoting / validation -----------------------------

def test_quote_rejects_unknown_token(mocker):
    ps = _ps(mocker)
    with pytest.raises(KeyError):
        ps.quote("NOTAREALTOKEN", "USDT", 10.0)


def test_quote_rejects_nonpositive_amount(mocker):
    ps = _ps(mocker)
    with pytest.raises(ValueError):
        ps.quote("CAKE", "USDT", 0.0)
    with pytest.raises(ValueError):
        ps.quote("CAKE", "USDT", -5.0)


def test_quote_rejects_same_token(mocker):
    # review fix #1: swapping a token for itself is nonsensical and wastes gas.
    ps = _ps(mocker)
    with pytest.raises(ValueError):
        ps.quote("CAKE", "CAKE", 10.0)


def test_quote_computes_min_out_with_slippage(mocker):
    ps = _ps(mocker)
    # 1 CAKE in → router says 2.0 USDT out (2e18). min_out = 2e18 * 0.995.
    mocker.patch.object(ps, "get_amounts_out", return_value=[10**18, 2 * 10**18])
    q = ps.quote("CAKE", "USDT", 1.0)
    assert q.expected_out_wei == 2 * 10**18
    assert q.min_out_wei == tx_builder.apply_slippage(2 * 10**18, ps.slippage_bps)
    assert q.min_out_wei > 0
    assert q.min_out_wei < q.expected_out_wei


# ----------------------------- swap safety -----------------------------

def test_dry_run_swap_does_not_broadcast(mocker):
    ps = _ps(mocker, dry_run=True)
    mocker.patch.object(ps, "get_amounts_out", return_value=[10**18, 2 * 10**18])
    send = mocker.patch.object(ps, "_sign_and_send")
    approve = mocker.patch.object(ps, "_approve")

    result = ps.swap("CAKE", "USDT", 1.0)

    assert result.simulated is True
    send.assert_not_called()
    approve.assert_not_called()
    assert result.min_out_wei > 0


def test_quote_refuses_when_min_out_rounds_to_zero(mocker):
    # Safety: if the quote is so tiny that slippage-adjusted min_out is 0, the
    # swap must be REFUSED (an unprotected swap could be sandwiched to ~nothing).
    ps = _ps(mocker)
    mocker.patch.object(ps, "get_amounts_out", return_value=[10**18, 1])  # 1 wei out
    with pytest.raises(ValueError):
        ps.quote("CAKE", "USDT", 1.0)


def test_live_swap_without_account_raises(mocker):
    ps = _ps(mocker, dry_run=False)
    ps.account = None
    mocker.patch.object(ps, "get_amounts_out", return_value=[10**18, 2 * 10**18])
    with pytest.raises(RuntimeError):
        ps.swap("CAKE", "USDT", 1.0)


def test_apply_slippage_edges():
    assert tx_builder.apply_slippage(10**18, 0) == 10**18      # 0 bps -> unchanged
    assert tx_builder.apply_slippage(10**18, 10_000) == 0      # 100% -> 0
    assert tx_builder.apply_slippage(3, 50) == 2               # floors (3*9950//10000)


def test_to_hex_handles_hexbytes_and_str():
    from src.agent.execution.pancakeswap import _to_hex

    class FakeHash:
        def hex(self):
            return "deadbeef"  # hexbytes may omit 0x

    assert _to_hex(FakeHash()) == "0xdeadbeef"
    assert _to_hex("0xdeadbeef") == "0xdeadbeef"  # already prefixed string preserved


def test_clamp_to_balance_at_exact_balance_clamps_down(mocker):
    ps = _ps(mocker)
    ps.account = mocker.Mock()
    ps.account.address = "0x0000000000000000000000000000000000000001"
    fake_erc20 = mocker.Mock()
    fake_erc20.functions.balanceOf.return_value.call.return_value = 5 * 10**18  # 5.0
    ps.w3.eth.contract.return_value = fake_erc20
    # requesting EXACTLY the balance must clamp just below it (avoid wei overshoot)
    assert ps._clamp_to_balance("CAKE", 5.0) == pytest.approx(5.0 * 0.9999)


def test_build_path_between_two_non_wbnb_tokens_routes_via_wbnb(mocker):
    ps = _ps(mocker)
    path = ps.build_path("BTCB", "ETH")
    assert path[0] == token_list.get_token("BTCB").address
    assert path[1] == token_list.get_token("WBNB").address
    assert path[-1] == token_list.get_token("ETH").address


def test_clamp_to_balance_caps_at_holdings(mocker):
    # review fix: selling >= the held balance must clamp below it (avoid revert).
    ps = _ps(mocker)
    ps.account = mocker.Mock()
    ps.account.address = "0x0000000000000000000000000000000000000001"
    fake_erc20 = mocker.Mock()
    fake_erc20.functions.balanceOf.return_value.call.return_value = 10**18  # 1.0 token
    ps.w3.eth.contract.return_value = fake_erc20

    assert ps._clamp_to_balance("CAKE", 5.0) == pytest.approx(0.9999)  # over -> clamped
    assert ps._clamp_to_balance("CAKE", 0.5) == 0.5                    # under -> unchanged


def test_live_swap_uses_exact_approval_and_minout(mocker):
    ps = _ps(mocker, dry_run=False)
    ps.account = mocker.Mock()
    ps.account.address = "0x0000000000000000000000000000000000000001"
    mocker.patch.object(ps, "get_amounts_out", return_value=[10**18, 2 * 10**18])
    mocker.patch.object(ps, "_clamp_to_balance", side_effect=lambda t, a: a)
    approve = mocker.patch.object(ps, "_approve")
    mocker.patch.object(ps, "_build_swap_tx", return_value={})
    send = mocker.patch.object(ps, "_sign_and_send", return_value="0xhash")

    amount_in_wei = tx_builder.to_wei_amount(1.0, token_list.get_token("CAKE").decimals)
    result = ps.swap("CAKE", "USDT", 1.0)

    # Exact-amount approval, never unlimited.
    approve.assert_called_once()
    _, approved_amount = approve.call_args.args
    assert approved_amount == amount_in_wei
    send.assert_called_once()
    assert result.simulated is False
    assert result.tx_hash == "0xhash"
