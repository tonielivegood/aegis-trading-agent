"""Robustness validation of the LIVE config against multi-year CMC daily history.

Two checks, both on the production strategy (fractional hold + drawdown breaker):

  --sweep  : grid over deploy_frac x basket_size (and the breaker threshold),
             reported as the outcome DISTRIBUTION across every 7-day window in
             ~5.7 years of CMC daily data. Confirms the live config is not
             overfit to the original 125-day hourly window — we want the
             worst-case bounded and DQ% = 0 across regimes, not the best return.

  --stress : run the production config through the worst historical crash weeks
             (May-2021, LUNA, FTX, Aug-2024) and show the breaker keeps the
             7-day drawdown well under the 30% disqualification cap.

Daily resolution understates intraday drawdown, so treat WorstDD as a lower
bound. Research tooling only — never touches the live path.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, cmc_data_loader, engine, metrics, walk_forward
from src.agent.config import settings
from src.agent.data import token_list

WINDOW, WARMUP, STEP = 7, 14, 3   # 7-day contest slice, 14-day warmup, slide 3 days

DEPLOY_FRACS = [0.40, 0.50, 0.60, 0.70, 0.80]
BASKET_SIZES = [4, 6, 8, 12]
BREAKERS = [0.12, 0.15, 0.20, 0.25, 0.30]

# Worst weeks in the data's range to stress the breaker (start-of-week dates).
CRASH_WEEKS = {
    "May 2021 crash": "2021-05-17",
    "LUNA collapse": "2022-05-09",
    "FTX collapse": "2022-11-07",
    "Aug 2024 unwind": "2024-08-05",
}


def _frac_fn(df):
    return lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=df)


def _subset(series, n):
    syms = [s for s in token_list.basket_symbols(n) if s in series]
    return {s: series[s] for s in syms}


def _row(label, series, fn, breaker):
    out = walk_forward.walk_forward(series, times_g, fn, window=WINDOW, warmup=WARMUP,
                                    step=STEP, drawdown_alert=breaker)
    a = walk_forward.aggregate(out)
    if not a.get("windows"):
        print(f"{label:<28}  (no windows)")
        return
    print(f"{label:<28}{a['avg_return']*100:>+7.1f}%{a['median_return']*100:>+7.1f}%"
          f"{a['worst_return']*100:>+7.1f}%{a['pct_profitable']*100:>6.0f}%"
          f"{a['worst_drawdown']*100:>8.1f}%{a['pct_dq']*100:>5.0f}%")


def run_sweep(series):
    live = f"(live: frac={settings.deploy_frac}, basket={settings.basket_size}, breaker={settings.max_drawdown_alert})"
    hdr = f"{'Config':<28}{'AvgRet':>8}{'Median':>8}{'Worst':>8}{'Win%':>6}{'WorstDD':>9}{'DQ%':>6}"

    print(f"\n=== deploy_frac x basket_size  (breaker -20%)  {live} ===")
    print(hdr)
    print("-" * len(hdr))
    for n in BASKET_SIZES:
        sub = _subset(series, n)
        for df in DEPLOY_FRACS:
            star = " *" if (df == settings.deploy_frac and n == settings.basket_size) else ""
            _row(f"frac={df:.2f} basket={n}{star}", sub, _frac_fn(df), 0.20)

    print(f"\n=== breaker threshold  (frac={settings.deploy_frac}, basket={settings.basket_size}) ===")
    print(hdr)
    print("-" * len(hdr))
    sub = _subset(series, settings.basket_size)
    for b in BREAKERS:
        star = " *" if b == settings.max_drawdown_alert else ""
        _row(f"breaker -{b*100:.0f}%{star}", sub, _frac_fn(settings.deploy_frac), b)
    print("\n  * = current live config")


def _nearest_index(target_iso):
    target = int(datetime.fromisoformat(target_iso).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return min(range(len(times_g)), key=lambda i: abs(times_g[i] - target))


def _window_result(series, fn, idx, breaker):
    lo, hi = idx - WARMUP, idx + WINDOW
    if lo < 0 or hi > len(times_g):
        return None
    sub = {s: series[s][lo:hi] for s in series}
    r = engine.run_backtest(sub, times_g[lo:hi], fn, drawdown_alert=breaker)
    eq = r.equity_curve[WARMUP:]  # contest slice only
    return metrics.total_return(eq), metrics.max_drawdown(eq)


def run_stress(series):
    sub = _subset(series, settings.basket_size)
    print(f"\n=== Crash-week stress test  (production: frac={settings.deploy_frac}, "
          f"basket={settings.basket_size}, breaker -{settings.max_drawdown_alert*100:.0f}%) ===")
    hdr = f"{'Crash week':<20}{'B&H ret':>9}{'B&H DD':>8}{'Prod ret':>10}{'Prod DD':>9}{'DQ?':>5}"
    print(hdr)
    print("-" * len(hdr))
    for name, date in CRASH_WEEKS.items():
        idx = _nearest_index(date)
        bh = _window_result(sub, adapters.buy_and_hold_decide, idx, settings.max_drawdown_alert)
        prod = _window_result(sub, _frac_fn(settings.deploy_frac), idx, settings.max_drawdown_alert)
        if not bh or not prod:
            print(f"{name:<20}  (outside data range)")
            continue
        dq = "DQ" if prod[1] >= 0.30 else "ok"
        print(f"{name:<20}{bh[0]*100:>+8.1f}%{bh[1]*100:>7.1f}%"
              f"{prod[0]*100:>+9.1f}%{prod[1]*100:>8.1f}%{dq:>5}")


def main():
    global times_g
    print("Loading CMC Pro daily history (multi-year)...")
    series, times_g = cmc_data_loader.load_history_cmc(interval="daily", count=4000)
    if not series:
        print("No data.")
        return
    start = datetime.fromtimestamp(times_g[0] / 1000, timezone.utc).date()
    print(f"[CMC Pro daily] {len(series)} tokens, {len(times_g)} bars "
          f"({start} -> present, ~{len(times_g)/365:.1f} yrs)")

    do_sweep = "--stress" not in sys.argv or "--sweep" in sys.argv
    do_stress = "--sweep" not in sys.argv or "--stress" in sys.argv
    if do_sweep:
        run_sweep(series)
    if do_stress:
        run_stress(series)


if __name__ == "__main__":
    main()
