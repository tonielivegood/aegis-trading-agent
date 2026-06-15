"""Walk-forward validation across every 7-day window in extended history.

Answers: 'if the contest week were any given week in the past ~4 months, how
would each strategy do?' — reporting the DISTRIBUTION, not a single window.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, data_loader, walk_forward

STRATEGIES = {
    "Preserve (all stable)": lambda p, s, h: [],
    "Buy & Hold (market)": adapters.buy_and_hold_decide,
    "Vol-basket rebalance": adapters.basket_rebalance_decide,
    "Preservation-first": adapters.preservation_first_decide,
    "Hold + breaker (-20%)": adapters.hold_with_breaker_decide,
    "Fractional 50% + breaker": adapters.fractional_hold_decide,
}


def main() -> None:
    print("Loading extended Binance history (this fetches a few pages)...")
    series, times = data_loader.load_history(limit=3000)
    if not series:
        print("No data.")
        return
    print(f"Loaded {len(series)} tokens, {len(times)} bars (~{len(times) / 24:.0f} days)\n")

    hdr = f"{'Strategy':<22}{'AvgRet':>8}{'Median':>8}{'Worst':>8}{'Best':>8}{'Win%':>7}{'WorstDD':>9}{'DQ%':>6}"
    print(hdr)
    print("-" * len(hdr))
    for name, fn in STRATEGIES.items():
        outcomes = walk_forward.walk_forward(series, times, fn, window=168, warmup=168, step=24)
        a = walk_forward.aggregate(outcomes)
        if not a.get("windows"):
            print(f"{name:<22}  (no windows)")
            continue
        print(f"{name:<22}{a['avg_return']*100:>+7.1f}%{a['median_return']*100:>+7.1f}%"
              f"{a['worst_return']*100:>+7.1f}%{a['best_return']*100:>+7.1f}%"
              f"{a['pct_profitable']*100:>6.0f}%{a['worst_drawdown']*100:>8.1f}%{a['pct_dq']*100:>5.0f}%")
    print(f"\n(windows per strategy: {a['windows']}, each = 168h contest sim with 168h warmup)")


if __name__ == "__main__":
    main()
