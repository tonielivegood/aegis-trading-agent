"""Conditional walk-forward: how do strategies behave in UP weeks vs DOWN weeks?

The full-sample averages are dominated by a down/chop market. For a one-week
contest scored on raw return, the decision-relevant question is the conditional:
GIVEN the contest week is bullish, which strategy captures more? We bucket the
identical windows by the market's own direction (buy & hold sign) and compare.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.backtest import adapters, data_loader, walk_forward

STRATS = {
    "Market (B&H)": adapters.buy_and_hold_decide,
    "Hold+brk 50%": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.50),
    "Hold+brk 65%": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.65),
    "Hold+brk 80%": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.80),
    "Hold+brk 95%": lambda p, s, h: adapters.fractional_hold_decide(p, s, h, deploy_frac=0.95),
}


def main() -> None:
    series, times = data_loader.load_history(limit=3000)
    print(f"[hourly] {len(series)} tokens, {len(times)} bars\n")

    # Per-window return lists (same window order across strategies => index-paired).
    rets = {}
    for name, fn in STRATS.items():
        out = walk_forward.walk_forward(series, times, fn, window=168, warmup=168, step=24)
        rets[name] = [o["return"] for o in out]

    market = rets["Market (B&H)"]
    up = [i for i, r in enumerate(market) if r > 0]
    down = [i for i, r in enumerate(market) if r <= 0]
    print(f"windows: {len(market)}  (up={len(up)}, down={len(down)})\n")

    hdr = f"{'Strategy':<22}{'UP avg':>9}{'UP best':>9}{'UP win%':>9}{'DOWN avg':>10}{'DOWN best':>11}"
    print(hdr)
    print("-" * len(hdr))
    for name, rs in rets.items():
        up_r = [rs[i] for i in up]
        dn_r = [rs[i] for i in down]
        up_avg = sum(up_r) / len(up_r) if up_r else 0.0
        up_best = max(up_r) if up_r else 0.0
        up_win = sum(1 for r in up_r if r > 0) / len(up_r) if up_r else 0.0
        dn_avg = sum(dn_r) / len(dn_r) if dn_r else 0.0
        dn_best = max(dn_r) if dn_r else 0.0
        print(f"{name:<22}{up_avg*100:>+8.1f}%{up_best*100:>+8.1f}%{up_win*100:>8.0f}%"
              f"{dn_avg*100:>+9.1f}%{dn_best*100:>+10.1f}%")


if __name__ == "__main__":
    main()
