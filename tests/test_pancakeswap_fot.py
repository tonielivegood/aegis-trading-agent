# tests/test_pancakeswap_fot.py
"""Fee-on-transfer support: the SupportingFee router function must be used and the
real received amount recorded — taxed memes revert on the plain variant and deliver
less than the quote (root cause of the stuck 金狗/未来协议 exits, 2026-07-05)."""
from unittest.mock import MagicMock

from src.agent.execution.pancakeswap import PancakeSwap, SwapResult


def _fake_dex(monkeypatch=None, slippage_bps=None):
    w3 = MagicMock()
    account = MagicMock()
    account.address = "0x" + "a" * 40
    dex = PancakeSwap(w3=w3, account=account, dry_run=True, slippage_bps=slippage_bps)
    return dex, w3


def test_slippage_override_wins_over_settings():
    dex, _ = _fake_dex(slippage_bps=1500)
    assert dex.slippage_bps == 1500


def test_default_slippage_still_from_settings():
    from src.agent.config import settings
    dex, _ = _fake_dex(slippage_bps=None)
    assert dex.slippage_bps == settings.slippage_bps


def test_build_swap_tx_uses_supporting_fee_variant():
    dex, w3 = _fake_dex()
    q = MagicMock(amount_in_wei=1, min_out_wei=1, path=["0xa", "0xb"])
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.gas_price = 1
    dex._build_swap_tx(q)
    assert dex.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens.called
    assert not dex.router.functions.swapExactTokensForTokens.called


def test_swap_result_has_received_out_wei_default():
    r = SwapResult("A", "B", 1, 2, 1, simulated=True)
    assert r.received_out_wei == 0
