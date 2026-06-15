"""Compare candidate strategies on the same real Binance history."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, data_loader, engine

STRATEGIES = {
    "Preserve (all stable)": lambda p, s, h: [],
    "Buy & Hold (market)": adapters.buy_and_hold_decide,
    "Momentum (baseline)": adapters.momentum_decide,
    "Mean-reversion": adapters.mean_reversion_decide,
    "Vol-basket rebalance": adapters.basket_rebalance_decide,
    "Regime-adaptive": adapters.regime_adaptive_decide,
}


def main() -> None:
    print("Loading Binance hourly history for core tokens...")
    series, times = data_loader.load_history(limit=1000)
    if not series:
        print("No data loaded.")
        return
    print(f"Loaded {len(series)} tokens, {len(times)} bars (~{len(times) / 24:.0f} days)\n")

    print(f"{'Strategy':<24}{'Return':>9}{'MaxDD':>8}{'Sharpe':>8}{'Win%':>7}{'Trades':>8}  Verdict")
    print("-" * 80)
    for name, fn in STRATEGIES.items():
        r = engine.run_backtest(series, times, fn, start_cash=100.0, fee_bps=25, slippage_bps=50)
        m = r.metrics
        dq = m["max_drawdown"] >= 0.30
        ok = (m["total_return"] > 0) and not dq
        verdict = "DQ!" if dq else ("PASS" if ok else "loses")
        print(f"{name:<24}{m['total_return'] * 100:>+8.1f}%{m['max_drawdown'] * 100:>7.1f}%"
              f"{m['sharpe']:>8.2f}{m['win_rate'] * 100:>6.0f}%{m['trade_count']:>8}  {verdict}")


if __name__ == "__main__":
    main()
