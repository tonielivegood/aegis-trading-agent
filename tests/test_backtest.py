"""Backtest engine tests — written test-first (TDD).

Two things must be provably correct or the whole evidence base is worthless:
  - metrics math (return, max drawdown, Sharpe, win rate)
  - the simulation has NO lookahead bias and applies fees/slippage as a drag
"""
from __future__ import annotations

import pytest

from src.agent.backtest import engine, metrics


# ----------------------------- metrics (pure) -----------------------------

def test_total_return():
    assert metrics.total_return([100.0, 110.0]) == pytest.approx(0.10)
    assert metrics.total_return([100.0, 90.0]) == pytest.approx(-0.10)


def test_total_return_empty_or_zero_safe():
    assert metrics.total_return([]) == 0.0
    assert metrics.total_return([0.0, 50.0]) == 0.0


def test_max_drawdown():
    # peak 120 then trough 90 -> 25% drawdown
    assert metrics.max_drawdown([100.0, 120.0, 90.0, 150.0]) == pytest.approx(0.25)


def test_max_drawdown_monotonic_up_is_zero():
    assert metrics.max_drawdown([100.0, 110.0, 120.0]) == 0.0


def test_sharpe_zero_variance_is_zero():
    assert metrics.sharpe([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_positive_for_positive_mean():
    assert metrics.sharpe([0.01, 0.02, -0.005, 0.015]) > 0


def test_win_rate():
    assert metrics.win_rate([1.0, -1.0, 2.0, 3.0]) == pytest.approx(0.75)
    assert metrics.win_rate([]) == 0.0


# ----------------------------- engine -----------------------------

def _flat_series(symbols, n, price=10.0):
    return {s: [price] * n for s in symbols}


def test_flat_market_no_trades_preserves_cash():
    series = _flat_series(["CAKE"], 200)
    ts = list(range(200))
    no_op = lambda prices, state, history: []
    result = engine.run_backtest(series, ts, no_op, start_cash=100.0)
    assert result.equity_curve[-1] == pytest.approx(100.0)


def test_no_lookahead_history_is_bounded_by_current_step():
    series = {"CAKE": [float(i) for i in range(1, 51)]}
    ts = list(range(50))
    seen_lengths = []

    def spy(prices, state, history):
        seen_lengths.append(len(history["CAKE"]))
        return []

    engine.run_backtest(series, ts, spy, start_cash=100.0)
    # At step i the strategy may see exactly i+1 points — never the future.
    assert seen_lengths == list(range(1, 51))


def test_fees_and_slippage_reduce_returns_on_round_trip():
    # Buy then sell at the SAME price must lose money to costs (no free lunch).
    series = {"CAKE": [10.0] * 10}
    ts = list(range(10))

    def buy_then_sell(prices, state, history):
        from src.agent.strategy.base_strategy import TradeOrder
        step = len(history["CAKE"])
        if step == 2 and state.stable_value_usd > 50:
            return [TradeOrder("USDT", "CAKE", 50.0, "buy")]
        if step == 5 and state.token_values_usd.get("CAKE", 0) > 0:
            return [TradeOrder("CAKE", "USDT", state.token_values_usd["CAKE"], "sell")]
        return []

    result = engine.run_backtest(series, ts, buy_then_sell, start_cash=100.0,
                                 fee_bps=25, slippage_bps=50)
    assert result.equity_curve[-1] < 100.0          # costs ate into capital
    assert result.equity_curve[-1] > 98.0           # but only the fee/slippage drag


def test_rising_price_with_buy_and_hold_grows_equity():
    series = {"CAKE": [10.0 + i * 0.1 for i in range(50)]}  # steady uptrend
    ts = list(range(50))

    def buy_once(prices, state, history):
        from src.agent.strategy.base_strategy import TradeOrder
        if len(history["CAKE"]) == 2 and state.stable_value_usd > 50:
            return [TradeOrder("USDT", "CAKE", 50.0, "buy")]
        return []

    result = engine.run_backtest(series, ts, buy_once, start_cash=100.0)
    assert result.equity_curve[-1] > 100.0


def test_result_includes_metrics_summary():
    series = _flat_series(["CAKE"], 50)
    ts = list(range(50))
    result = engine.run_backtest(series, ts, lambda p, s, h: [], start_cash=100.0)
    assert "total_return" in result.metrics
    assert "max_drawdown" in result.metrics
    assert "trade_count" in result.metrics


# ----------------------------- walk-forward aggregation -----------------------------

def test_walk_forward_aggregate_distribution():
    from src.agent.backtest.walk_forward import aggregate
    outcomes = [
        {"return": 0.10, "max_drawdown": 0.05},
        {"return": -0.05, "max_drawdown": 0.35},   # this one would be DQ'd
        {"return": 0.02, "max_drawdown": 0.10},
    ]
    agg = aggregate(outcomes)
    assert agg["windows"] == 3
    assert agg["worst_return"] == -0.05
    assert agg["best_return"] == 0.10
    assert agg["pct_profitable"] == pytest.approx(2 / 3)
    assert agg["worst_drawdown"] == 0.35
    assert agg["pct_dq"] == pytest.approx(1 / 3)


def test_walk_forward_aggregate_empty_safe():
    from src.agent.backtest.walk_forward import aggregate
    assert aggregate([]) == {"windows": 0}
