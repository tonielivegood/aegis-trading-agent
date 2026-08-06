"""Is there a TIGHTER entry that makes a LOWER take-profit target pay?

anh TONiE's question after 12 straight live losses: enter more selectively,
accept less profit, carry less risk. On its own that fails — 3x and 4x targets
are negative expectancy across the whole population. The untested combination is
whether a filter that raises the survival rate lets a lower target work, which
would cut variance hard: at 2x you need 50% to break even, at 6x only 17%.

Every candidate is chosen on the FIRST half of the data by time and then applied
blind to the second. A sweep reported on the data that chose it finds noise, and
this project has already buried five filter hypotheses that looked good until
they were checked.

The lead hypothesis is liquidity TREND, taken from the two BABYMARS positions
that reached 9.19x and 6.91x live on 2026-08-06: their pool grew from $36k to
$117k while price climbed, i.e. real money was arriving. A pump with a flat or
shrinking pool is a different animal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.simulate_takeprofit_expectancy import (  # noqa: E402
    DEAD_LIQ_USD, Config, find_entry, load_goplus, load_token_samples,
    simulate_token, summarize,
)

TARGETS = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]
LOOKBACK = 4          # samples (~2 min) used to judge a trend


def _ratio(a, b):
    total = (a or 0) + (b or 0)
    return (a or 0) / total if total else None


def features(samples: list[dict], i: int) -> dict:
    """What is knowable at the moment the 2x triggers — nothing after it."""
    s = samples[i]
    past = samples[max(0, i - LOOKBACK)]
    liq, liq_before = s.get("liq"), past.get("liq")
    return {
        "liq_growth": (liq / liq_before) if liq and liq_before else None,
        "buy_share": _ratio(s.get("buys_m5"), s.get("sells_m5")),
        "vol_to_liq": (s["vol_m5"] / liq) if s.get("vol_m5") and liq else None,
        "minutes_to_2x": (s["ts"] - samples[0]["ts"]) / 60.0,
        "top5_pct": s.get("top5_pct"),
    }


# (name, feature key, keep-if) — each is a hypothesis, not a knob to tune.
FILTERS = [
    ("none", None, lambda v: True),
    ("liq_growing_5pct", "liq_growth", lambda v: v is not None and v >= 1.05),
    ("liq_growing_20pct", "liq_growth", lambda v: v is not None and v >= 1.20),
    ("liq_not_shrinking", "liq_growth", lambda v: v is not None and v >= 1.00),
    ("buy_share_60", "buy_share", lambda v: v is not None and v >= 0.60),
    ("buy_share_80", "buy_share", lambda v: v is not None and v >= 0.80),
    ("churn_5pct", "vol_to_liq", lambda v: v is not None and v >= 0.05),
    ("slow_climb_10min", "minutes_to_2x", lambda v: v is not None and v >= 10),
    ("top5_under_50", "top5_pct", lambda v: v is not None and v <= 50),
]


def build(by_token, goplus, cfg):
    """One row per token that triggers: its features and its outcome per target."""
    rows = []
    for tok, s in by_token.items():
        i = find_entry(s, False)
        if i is None:
            continue
        liq = s[i].get("liq")
        if liq is None or liq < 10_000.0:
            continue                       # the live bot's own gate, fail closed
        outcomes = {t: simulate_token(s, goplus.get(tok), t, cfg) for t in TARGETS}
        if any(o is None for o in outcomes.values()):
            continue
        rows.append({"ts": s[0]["ts"], "f": features(s, i), "o": outcomes})
    return rows


def score(rows, name, key, keep, target):
    sel = [r for r in rows if key is None or keep(r["f"].get(key))]
    if len(sel) < 30:                      # too few to mean anything
        return None
    return summarize([r["o"][target] for r in sel], target)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1] / "data" / "launch_collector"
    ap.add_argument("--films", default=str(root / "films.jsonl"))
    ap.add_argument("--snapshots", default=str(root / "snapshots.jsonl"))
    args = ap.parse_args()

    by_token, rejected = load_token_samples(Path(args.films))
    goplus = load_goplus(Path(args.snapshots))
    cfg = Config(size_usd=0.5, fill_delay=1)
    rows = build(by_token, goplus, cfg)
    rows.sort(key=lambda r: r["ts"])
    cut = len(rows) // 2
    first, second = rows[:cut], rows[cut:]
    combos = len(FILTERS) * len(TARGETS)
    print(f"excluded {rejected['re_armed']} re-armed, {rejected['bad_print']} bad print")
    print(f"triggers: {len(rows)}  (first half {len(first)}, held out {len(second)})")
    print(f"combinations tried: {combos} — the more tried, the likelier a fluke\n")

    ranked = []
    for name, key, keep in FILTERS:
        for target in TARGETS:
            st = score(first, name, key, keep, target)
            if st:
                ranked.append((st["expectancy"], name, key, keep, target, st))
    ranked.sort(reverse=True, key=lambda r: r[0])

    print("BEST ON THE FIRST HALF, then the SAME rule on data it never saw:")
    print(f"{'filter':<20} {'target':>7} {'in-sample':>22} {'HELD OUT':>24}")
    for exp, name, key, keep, target, st in ranked[:8]:
        oos = score(second, name, key, keep, target)
        if not oos:
            continue
        print(f"{name:<20} {target:>6.1f}x "
              f"{st['n']:>6} trades {st['expectancy']:>+7.3f} "
              f"|{oos['n']:>7} trades {oos['profit_rate']:>6.1%} "
              f"{oos['expectancy']:>+7.3f}")

    print("\nBASELINE (no filter), held-out half, every target:")
    for target in TARGETS:
        oos = score(second, "none", None, lambda v: True, target)
        if oos:
            print(f"  {target:>4.1f}x  {oos['n']:>5} trades  "
                  f"{oos['profit_rate']:>6.1%}  {oos['expectancy']:>+7.3f}")


if __name__ == "__main__":
    main()
