"""Walk-forward validation.

Slides a contest-length window (default 7 days = 168h) across the full history,
each time giving the strategy a warmup prefix for its lookback, then measuring
only the contest slice. Reports the DISTRIBUTION of outcomes across all windows —
the honest test of robustness for a one-week contest whose direction we can't
predict. We care less about the best window than about the worst and the
DQ rate.
"""
from __future__ import annotations

from . import engine, metrics

DQ_DRAWDOWN = 0.30


def walk_forward(series, times, decide_fn, *, window=168, warmup=168, step=24,
                 start_cash=100.0, fee_bps=25, slippage_bps=50,
                 drawdown_alert=0.20, drawdown_cap=0.30) -> list[dict]:
    n = len(times)
    outcomes: list[dict] = []
    for start in range(warmup, n - window, step):
        lo = start - warmup
        sub = {s: series[s][lo:start + window] for s in series}
        subt = times[lo:start + window]
        r = engine.run_backtest(sub, subt, decide_fn, start_cash=start_cash,
                                fee_bps=fee_bps, slippage_bps=slippage_bps,
                                drawdown_alert=drawdown_alert, drawdown_cap=drawdown_cap)
        eq = r.equity_curve[warmup:]  # measure only the contest slice
        if len(eq) < 2:
            continue
        outcomes.append({
            "return": metrics.total_return(eq),
            "max_drawdown": metrics.max_drawdown(eq),
        })
    return outcomes


def aggregate(outcomes: list[dict]) -> dict:
    """Summarize the outcome distribution — robustness, not best case."""
    if not outcomes:
        return {"windows": 0}
    rets = sorted(o["return"] for o in outcomes)
    dds = [o["max_drawdown"] for o in outcomes]
    n = len(rets)
    return {
        "windows": n,
        "avg_return": sum(rets) / n,
        "median_return": rets[n // 2],
        "worst_return": rets[0],
        "best_return": rets[-1],
        "pct_profitable": sum(1 for r in rets if r > 0) / n,
        "worst_drawdown": max(dds),
        "pct_dq": sum(1 for d in dds if d >= DQ_DRAWDOWN) / n,
    }
