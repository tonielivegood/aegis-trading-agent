from unittest.mock import patch

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


def test_reconcile_sets_available_to_total_minus_open_usd():
    b = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    b.reconcile(9.0)   # e.g. 3 real positions opened at a since-changed slice size
    assert b.available_usd == pytest.approx(7.14)


def test_reconcile_clamps_to_zero_and_warns_on_overcommitment():
    b = CopyTradeBudget(total_usd=10.0, slice_usd=3.0)
    with patch("src.agent.copy_trade.budget.log") as mock_log:
        b.reconcile(12.0)   # loaded positions total more than the configured budget
    assert b.available_usd == 0.0          # never negative
    mock_log.warning.assert_called_once()
