"""Does "enter at 2x, take profit on the way up" have positive expectancy?

The 2026-07-30 finding closed off exits after entry: 24 of 25 deaths went from
healthy liquidity to under $1,000 inside ONE 30s sample, so stop-losses cannot
execute — a loss is a 100% loss, not a percentage. The only exit shape left is
selling into strength before the rug. This script is the first test of whether
that shape ever pays for itself, using the 30s films already on disk — no
outcome labels needed, since the question resolves within a single 4h film.

ponytail: idealized fills (mid-price, no slippage/tax/gas). Real fills are
worse, so a positive number here is a floor to interrogate, not a result to
trade on.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.copy_trade.launch_collector import DEAD_LIQ_USD  # noqa: E402

ENTRY_MULTIPLE = 2.0
TARGETS = [1.3, 1.5, 2.0, 2.5, 3.0, 4.0]   # multiple of ENTRY price, not arm price


def load_token_samples(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("event") == "sample" and row.get("token_address"):
                out.setdefault(row["token_address"], []).append(row)
    for samples in out.values():
        samples.sort(key=lambda s: s.get("ts") or 0.0)
    return out


def find_entry(samples: list[dict], require_dominance: bool) -> int | None:
    """Index of the first sample at >= ENTRY_MULTIPLE x the arm price."""
    if not samples:
        return None
    base = samples[0]["price"]
    if not base:
        return None
    for i, s in enumerate(samples):
        if s.get("price") is None:
            continue
        if s["price"] < base * ENTRY_MULTIPLE:
            continue
        if require_dominance and not ((s.get("buys_m5") or 0) > (s.get("sells_m5") or 0)):
            continue
        return i
    return None


def simulate_token(samples: list[dict], require_dominance: bool) -> dict:
    """One token's outcome at every target: 'win' (index, target), 'loss', or
    'open'. Win priority on a tie: a monitoring bot would see the take-profit
    condition satisfied in the same tick it would have seen the death."""
    entry_i = find_entry(samples, require_dominance)
    if entry_i is None:
        return {"entered": False}

    entry_price = samples[entry_i]["price"]
    after = samples[entry_i:]
    death_i = next((j for j, s in enumerate(after)
                    if s.get("liq") is not None and s["liq"] < DEAD_LIQ_USD), None)

    results = {}
    for target in TARGETS:
        want = entry_price * target
        win_i = next((j for j, s in enumerate(after)
                     if s.get("price") is not None and s["price"] >= want), None)
        if win_i is not None and (death_i is None or win_i <= death_i):
            results[target] = "win"
        elif death_i is not None:
            results[target] = "loss"
        else:
            results[target] = "open"
    return {"entered": True, "results": results}


def expectancy(outcomes: list[dict], target: float) -> dict:
    labels = [o["results"][target] for o in outcomes if o["entered"]]
    wins = labels.count("win")
    losses = labels.count("loss")
    opens = labels.count("open")
    resolved = wins + losses
    win_rate = wins / resolved if resolved else None
    ev = (win_rate * (target - 1) - (1 - win_rate)) if win_rate is not None else None
    return {"wins": wins, "losses": losses, "open": opens, "resolved": resolved,
            "win_rate": win_rate, "expectancy_per_unit": ev}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--films", default=str(Path(__file__).resolve().parents[1]
                                          / "data" / "launch_collector" / "films.jsonl"))
    ap.add_argument("--dominance", action="store_true",
                    help="also require buys_m5 > sells_m5 at entry")
    args = ap.parse_args()

    by_token = load_token_samples(Path(args.films))
    outcomes = [simulate_token(s, args.dominance) for s in by_token.values()]
    entered = [o for o in outcomes if o["entered"]]

    print(f"tokens with film data : {len(outcomes)}")
    print(f"reached {ENTRY_MULTIPLE}x (entered)  : {len(entered)} "
          f"({100 * len(entered) / len(outcomes):.1f}%)")
    print()
    print(f"{'target':>8} {'wins':>6} {'losses':>7} {'open':>6} "
          f"{'win_rate':>9} {'need_to_BE':>11} {'expectancy':>11}")
    for target in TARGETS:
        e = expectancy(entered, target)
        need = 1 / target
        wr = f"{e['win_rate']:.1%}" if e["win_rate"] is not None else "n/a"
        ev = f"{e['expectancy_per_unit']:+.3f}" if e["expectancy_per_unit"] is not None else "n/a"
        print(f"{target:>7.1f}x {e['wins']:>6} {e['losses']:>7} {e['open']:>6} "
              f"{wr:>9} {need:>10.1%} {ev:>11}")


if __name__ == "__main__":
    main()
