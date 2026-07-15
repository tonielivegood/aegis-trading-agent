import pytest

from src.agent.copy_trade.budget import CopyTradeBudget


def test_starts_with_full_budget_available():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    assert b.available_usd == pytest.approx(15.39)
    assert b.can_open_new() is True


def test_allocate_reduces_available_by_slice_size():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    amount = b.allocate()
    assert amount == pytest.approx(1.5)
    assert b.available_usd == pytest.approx(13.89)


def test_cannot_open_new_once_budget_below_one_slice():
    b = CopyTradeBudget(total_usd=1.4, slice_usd=1.5)
    assert b.can_open_new() is False
    with pytest.raises(RuntimeError):
        b.allocate()


def test_release_returns_the_slice_to_available_budget():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    b.allocate()
    b.release(1.5)
    assert b.available_usd == pytest.approx(15.39)


def test_ten_slices_exhaust_a_fifteen_dollar_budget():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    opened = 0
    while b.can_open_new():
        b.allocate()
        opened += 1
    assert opened == 10
    assert b.available_usd == pytest.approx(0.39, abs=1e-9)
