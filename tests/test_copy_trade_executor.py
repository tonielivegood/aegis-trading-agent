import pytest

from src.agent.config import settings
from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.executor import handle_alert
from src.agent.copy_trade.positions import CopyPosition, PositionStore
from src.agent.copy_trade.swap_parser import ParsedSwap

BUY = ParsedSwap(
    hash="0x1", wallet="0xshark1", direction="buy", token_symbol="GEM",
    token_address="0xgem1", token_decimals=9, token_amount=12345.0,
    counter_symbol="USDT", usd_value=None, timestamp="2026-07-15T10:00:00Z",
)
SELL = ParsedSwap(
    hash="0x2", wallet="0xshark1", direction="sell", token_symbol="GEM",
    token_address="0xgem1", token_decimals=9, token_amount=12345.0,
    counter_symbol="USDT", usd_value=None, timestamp="2026-07-15T11:00:00Z",
)


def _mock_executors(mocker):
    winning = mocker.MagicMock()
    winning.swap.return_value = mocker.MagicMock(simulated=False, tx_hash="0xexec1")
    return {"1inch": winning}, winning


def test_buy_signal_allocates_budget_registers_token_and_executes(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mock_safety = mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(True, 9))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mock_rank = mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    mock_register.assert_called_once_with("GEM", "0xgem1", 9)
    winning.swap.assert_called_once_with("USDT", "GEM", 1.5)
    assert budget.available_usd == pytest.approx(13.89)
    assert store.find("0xgem1", "0xshark1") is not None
    # Pin the slice-size fix: gating calls must use the per-trade slice (1.5),
    # not the full pool (15.39).
    mock_safety.assert_called_once_with(
        settings.usdt_address, "0xgem1", str(int(1.5 * 10**18))
    )
    mock_rank.assert_called_once_with(executors, "USDT", "GEM", 1.5)


def test_buy_signal_releases_budget_when_no_route_found(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(True, 9))
    mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=[])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    winning.swap.assert_not_called()
    assert budget.available_usd == pytest.approx(15.39)
    assert store.find("0xgem1", "0xshark1") is None


def test_buy_signal_skipped_when_safety_check_fails(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(False, None))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    winning.swap.assert_not_called()
    mock_register.assert_not_called()
    assert budget.available_usd == pytest.approx(15.39)
    assert store.find("0xgem1", "0xshark1") is None


def test_buy_signal_skipped_when_budget_exhausted(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(True, 9))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=1.0, slice_usd=1.5)  # already too small
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    winning.swap.assert_not_called()
    mock_register.assert_not_called()


def test_sell_signal_closes_matching_position_and_releases_budget(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=13.89, slice_usd=1.5)  # 1 slice already spent
    store = PositionStore(tmp_path / "positions.json")
    store.open_position(CopyPosition(
        token_symbol="GEM", token_address="0xgem1", token_decimals=9,
        source_wallet="0xshark1", usd_size=1.5, token_amount=12345.0,
        opened_at="2026-07-15T10:00:00Z",
    ))

    handle_alert(SELL, budget, store, executors)

    winning.swap.assert_called_once_with("GEM", "USDT", 12345.0)
    assert store.find("0xgem1", "0xshark1") is None
    assert budget.available_usd == pytest.approx(15.39)


def test_sell_signal_for_untracked_position_is_a_noop(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(SELL, budget, store, executors)  # never bought this one

    winning.swap.assert_not_called()
