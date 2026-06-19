"""Walk-forward backtest on ELIGIBLE tokens that actually have long history.

Most of the 149 eligible tokens are brand-new memes with no history. But a subset
are established (TWT, SFP, EPS, TKO, SANTOS, ALPINE, WIN, ROSE, WRX...) and DO have
multi-year CMC daily data. We fetch those, report real coverage, and run the same
7-day walk-forward used for the majors — but on the REAL eligible universe.

HONEST SCOPE: daily bars validate HOLD/risk behaviour (drawdown, DQ, up-week
capture) on eligible tokens. They do NOT represent the intraday event-alpha
(5h hold / 5m volume / live catalysts) — that remains live-only. No faked data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, cmc_data_loader, walk_forward

# Established eligible tokens likely to carry multi-year CMC daily history.
CANDIDATES = ["TWT", "SFP", "EPS", "TKO", "SANTOS", "ALPINE", "WIN", "ROSE", "WRX", "BURN"]

STRATEGIES = {
    "Preserve (cash)": lambda p, s, h: [],
    "Buy & Hold eligible": adapters.buy_and_hold_decide,
    "Hold + breaker": adapters.hold_with_breaker_decide,
    "Fractional 50% + breaker": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.50),
    "Fractional 80% + breaker": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.80),
    "Concentr top-2 (80%)": lambda p, s, h: adapters.concentrated_momentum_decide(p, s, h, top_n=2, deploy_frac=0.80),
}


def main() -> None:
    print("Fetching CMC daily history for established ELIGIBLE tokens...")
    series, times = cmc_data_loader.load_history_cmc(symbols=CANDIDATES, interval="daily", count=2000)
    if not series:
        print("No CMC history returned for any candidate — cannot backtest eligible tokens.")
        return

    got = sorted(series.keys())
    missing = [s for s in CANDIDATES if s not in series]
    print(f"\nCoverage: {len(got)}/{len(CANDIDATES)} tokens returned history -> {', '.join(got)}")
    if missing:
        print(f"No history (skipped): {', '.join(missing)}")
    print(f"Aligned window: {len(times)} daily bars (~{len(times)/365:.1f} yr overlap across the set)")
    print("NOTE: daily resolution UNDERSTATES intraday drawdown; this is HOLD/risk validation,")
    print("      not the intraday catalyst event-alpha.\n")

    hdr = f"{'Strategy':<26}{'AvgRet':>8}{'Median':>8}{'Worst':>8}{'Best':>8}{'Win%':>7}{'WorstDD':>9}{'DQ%':>6}"
    print(hdr)
    print("-" * len(hdr))
    windows = 0
    for name, fn in STRATEGIES.items():
        out = walk_forward.walk_forward(series, times, fn, window=7, warmup=14, step=3)
        a = walk_forward.aggregate(out)
        if not a.get("windows"):
            print(f"{name:<26}  (no windows)")
            continue
        windows = a["windows"]
        print(f"{name:<26}{a['avg_return']*100:>+7.1f}%{a['median_return']*100:>+7.1f}%"
              f"{a['worst_return']*100:>+7.1f}%{a['best_return']*100:>+7.1f}%"
              f"{a['pct_profitable']*100:>6.0f}%{a['worst_drawdown']*100:>8.1f}%{a['pct_dq']*100:>5.0f}%")
    print(f"\nWindows per strategy: {windows} (each = 7-day contest sim, 14-day warmup, on eligible tokens)")


if __name__ == "__main__":
    main()
