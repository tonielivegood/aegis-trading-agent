"""Event-driven backtest simulation.

Walks the price series one step at a time. At each step the strategy sees ONLY
history up to and including the current bar (no lookahead), returns TradeOrders,
and those orders are filled at the current price with fee + slippage applied as a
drag. Produces an equity curve and per-trade PnL for the metrics module.

The same TradeOrder/PortfolioState types as live trading are used, so a strategy
that backtests well behaves identically in the live loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..data.token_list import STABLECOINS
from ..risk.drawdown import DrawdownTracker
from ..strategy.base_strategy import PortfolioState, TradeOrder
from . import metrics

STABLE = "USDT"


@dataclass
class BacktestResult:
    equity_curve: list[float]
    timestamps: list
    trade_pnls: list[float]
    metrics: dict = field(default_factory=dict)


def _value(holdings: dict[str, float], prices: dict[str, float]) -> float:
    return sum(amt * prices.get(sym, 0.0) for sym, amt in holdings.items())


def run_backtest(
    series: dict[str, list[float]],
    timestamps: list,
    decide_fn,
    *,
    start_cash: float = 100.0,
    fee_bps: int = 25,
    slippage_bps: int = 50,
    drawdown_alert: float = 0.20,
    drawdown_cap: float = 0.30,
) -> BacktestResult:
    symbols = list(series.keys())
    n = len(timestamps)
    cost_mult = (1 - fee_bps / 10_000) * (1 - slippage_bps / 10_000)

    holdings: dict[str, float] = {STABLE: start_cash}  # USDT treated as $1
    cost_basis: dict[str, tuple[float, float]] = {}    # sym -> (amount, total_cost)
    drawdown = DrawdownTracker(drawdown_alert, drawdown_cap)

    equity_curve: list[float] = []
    trade_pnls: list[float] = []

    for i in range(n):
        prices = {sym: series[sym][i] for sym in symbols}
        prices[STABLE] = 1.0
        for s in STABLECOINS:
            prices.setdefault(s, 1.0)

        equity = _value(holdings, prices)
        drawdown.update(equity)

        token_values = {
            sym: holdings.get(sym, 0.0) * prices.get(sym, 0.0)
            for sym in symbols if sym not in STABLECOINS
        }
        stable_value = sum(holdings.get(s, 0.0) for s in STABLECOINS if s in holdings)
        state = PortfolioState(
            equity_usd=equity, risk_value_usd=equity - stable_value,
            stable_value_usd=stable_value, token_values_usd=token_values,
            drawdown_tripped=drawdown.breaker_tripped(), cap_breached=drawdown.cap_breached(),
        )

        history = {sym: series[sym][: i + 1] for sym in symbols}
        orders: list[TradeOrder] = decide_fn(prices, state, history) or []

        for o in orders:
            _apply_order(o, prices, holdings, cost_basis, cost_mult, trade_pnls)

        equity_curve.append(_value(holdings, prices))

    result = BacktestResult(equity_curve, timestamps, trade_pnls)
    result.metrics = metrics.summarize(equity_curve, trade_pnls)
    return result


def _apply_order(o: TradeOrder, prices, holdings, cost_basis, cost_mult, trade_pnls) -> None:
    price_in = prices.get(o.token_in, 0.0)
    price_out = prices.get(o.token_out, 0.0)
    if price_in <= 0 or price_out <= 0 or o.amount_in_usd <= 0:
        return

    spend_units = o.amount_in_usd / price_in
    if holdings.get(o.token_in, 0.0) < spend_units:
        return  # insufficient balance — skip (mirrors a reverted live tx)

    holdings[o.token_in] = holdings.get(o.token_in, 0.0) - spend_units
    usd_out = o.amount_in_usd * cost_mult
    recv_units = usd_out / price_out
    holdings[o.token_out] = holdings.get(o.token_out, 0.0) + recv_units

    # Track realized PnL when closing a risk position back into stable.
    if o.token_out in STABLECOINS and o.token_in not in STABLECOINS:
        amt, cost = cost_basis.get(o.token_in, (0.0, 0.0))
        if amt > 0:
            avg = cost / amt
            sold = min(spend_units, amt)
            trade_pnls.append((price_in - avg) * sold - (o.amount_in_usd - usd_out))
            cost_basis[o.token_in] = (amt - sold, cost - avg * sold)
    elif o.token_in in STABLECOINS and o.token_out not in STABLECOINS:
        amt, cost = cost_basis.get(o.token_out, (0.0, 0.0))
        cost_basis[o.token_out] = (amt + recv_units, cost + o.amount_in_usd)
