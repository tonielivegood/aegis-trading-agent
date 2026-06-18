"""Walk-forward validation across every 7-day window in extended history.

Answers: 'if the contest week were any given week in the past, how would each
strategy do?' — reporting the DISTRIBUTION, not a single window.

Two data sources:
  - default  : Binance public hourly (~125 days), 168h window — dense intraday
               resolution, the primary validation.
  - --cmc    : CoinMarketCap Pro DAILY history back to ~2014, 7-day window — a
               multi-year, multi-regime CROSS-CHECK of the production strategy
               (CMC is also the contest's own price source). Daily resolution
               UNDERSTATES intraday drawdown, so treat its WorstDD as a lower
               bound, not a replacement for the hourly run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, cmc_data_loader, data_loader, walk_forward

# Full set — only meaningful at hourly resolution (some use hour-based lookbacks).
STRATEGIES = {
    "Preserve (all stable)": lambda p, s, h: [],
    "Buy & Hold (market)": adapters.buy_and_hold_decide,
    "Vol-basket rebalance": adapters.basket_rebalance_decide,
    "Preservation-first": adapters.preservation_first_decide,
    "Hold + breaker (-20%)": adapters.hold_with_breaker_decide,
    "Fractional 50% + breaker": adapters.fractional_hold_decide,
    "Concentr top-2 (80%)": lambda p, s, h: adapters.concentrated_momentum_decide(
        p, s, h, top_n=2, deploy_frac=0.80),
    "Concentr top-2 (60%)": lambda p, s, h: adapters.concentrated_momentum_decide(
        p, s, h, top_n=2, deploy_frac=0.60),
    "Concentr top-1 (50%cap)": lambda p, s, h: adapters.concentrated_momentum_decide(
        p, s, h, top_n=1, deploy_frac=0.80),
}

# Resolution-agnostic subset for the DAILY cross-check: these depend only on
# portfolio state + the (bar-agnostic) drawdown breaker, so they translate
# cleanly to daily bars. Includes the production strategy.
DAILY_STRATEGIES = {
    "Preserve (all stable)": lambda p, s, h: [],
    "Buy & Hold (market)": adapters.buy_and_hold_decide,
    "Hold + breaker (-20%)": adapters.hold_with_breaker_decide,
    "Fractional 50% + breaker": adapters.fractional_hold_decide,
    "Concentr top-2 (80%)": lambda p, s, h: adapters.concentrated_momentum_decide(
        p, s, h, top_n=2, deploy_frac=0.80),
    "Concentr top-1 (50%cap)": lambda p, s, h: adapters.concentrated_momentum_decide(
        p, s, h, top_n=1, deploy_frac=0.80),
}


def _run(series, times, strategies, *, window, warmup, step, period_label) -> None:
    hdr = f"{'Strategy':<26}{'AvgRet':>8}{'Median':>8}{'Worst':>8}{'Best':>8}{'Win%':>7}{'WorstDD':>9}{'DQ%':>6}"
    print(hdr)
    print("-" * len(hdr))
    windows = 0
    for name, fn in strategies.items():
        outcomes = walk_forward.walk_forward(series, times, fn, window=window, warmup=warmup, step=step)
        a = walk_forward.aggregate(outcomes)
        if not a.get("windows"):
            print(f"{name:<26}  (no windows)")
            continue
        windows = a["windows"]
        print(f"{name:<26}{a['avg_return']*100:>+7.1f}%{a['median_return']*100:>+7.1f}%"
              f"{a['worst_return']*100:>+7.1f}%{a['best_return']*100:>+7.1f}%"
              f"{a['pct_profitable']*100:>6.0f}%{a['worst_drawdown']*100:>8.1f}%{a['pct_dq']*100:>5.0f}%")
    print(f"\n(windows per strategy: {windows}, each = {window}-{period_label} contest sim with {warmup}-{period_label} warmup)")


def main() -> None:
    if "--cmc" in sys.argv:
        print("Loading DAILY history from CoinMarketCap Pro (contest price source, multi-year)...")
        series, times = cmc_data_loader.load_history_cmc(interval="daily", count=4000)
        if not series:
            print("No data.")
            return
        print(f"[CMC Pro daily] {len(series)} tokens, {len(times)} bars (~{len(times) / 365:.1f} yrs)")
        print("NOTE: daily resolution understates intraday drawdown — WorstDD is a lower bound.\n")
        _run(series, times, DAILY_STRATEGIES, window=7, warmup=14, step=3, period_label="day")
    else:
        print("Loading extended Binance hourly history (this fetches a few pages)...")
        series, times = data_loader.load_history(limit=3000)
        if not series:
            print("No data.")
            return
        print(f"[Binance hourly] {len(series)} tokens, {len(times)} bars (~{len(times) / 24:.0f} days)\n")
        _run(series, times, STRATEGIES, window=168, warmup=168, step=24, period_label="hour")


if __name__ == "__main__":
    main()
