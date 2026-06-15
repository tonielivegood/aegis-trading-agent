"""Performance metrics computed from an equity curve and trade PnLs.

All pure functions. These are how we compare strategies objectively, so they
must be correct — they are tested against hand-computed values.
"""
from __future__ import annotations

import math

# Hourly data: 24 * 365 periods per year for annualization.
PERIODS_PER_YEAR = 24 * 365


def total_return(equity_curve: list[float]) -> float:
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return 0.0
    return (equity_curve[-1] - equity_curve[0]) / equity_curve[0]


def max_drawdown(equity_curve: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction."""
    peak = float("-inf")
    worst = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def returns_series(equity_curve: list[float]) -> list[float]:
    out = []
    for prev, cur in zip(equity_curve, equity_curve[1:]):
        out.append((cur - prev) / prev if prev > 0 else 0.0)
    return out


def volatility(returns: list[float], annualized: bool = True) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    return sd * math.sqrt(PERIODS_PER_YEAR) if annualized else sd


def sharpe(returns: list[float], annualized: bool = True) -> float:
    """Risk-free rate assumed 0 (short horizon)."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    sd = volatility(returns, annualized=False)
    if sd == 0:
        return 0.0
    ratio = mean / sd
    return ratio * math.sqrt(PERIODS_PER_YEAR) if annualized else ratio


def win_rate(trade_pnls: list[float]) -> float:
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def summarize(equity_curve: list[float], trade_pnls: list[float]) -> dict:
    rets = returns_series(equity_curve)
    return {
        "total_return": total_return(equity_curve),
        "max_drawdown": max_drawdown(equity_curve),
        "sharpe": sharpe(rets),
        "volatility": volatility(rets),
        "win_rate": win_rate(trade_pnls),
        "trade_count": len(trade_pnls),
        "final_equity": equity_curve[-1] if equity_curve else 0.0,
    }
