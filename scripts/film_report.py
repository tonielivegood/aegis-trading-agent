"""Phase-2 film scorecard — did the accumulation pattern we filmed precede a gem?

    .venv/bin/python scripts/film_report.py            # default 7-day window
    .venv/bin/python scripts/film_report.py --days 14

Reads watchlist_films.jsonl (arm/sample/disarm events written by the stakeout
recorder — src/agent/copy_trade/watchlist.py). Groups rows into per-token films,
computes shape fingerprints (base_ratio, holder_growth_pct, liq_ratio, max_top_pct)
from each film's samples, and scores the outcome (max price multiple since arm,
via GeckoTerminal). Prints one row per film plus a median-fingerprint split of
2x+ outcomes vs <2x — THIS is what tunes the Phase-2 entry thresholds.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gem_report import _read_jsonl, fetch_max_price_since

ROOT = Path(__file__).resolve().parents[1]
FILMS_PATH = ROOT / "data" / "copy_trade" / "watchlist_films.jsonl"


# ---------- pure logic (tested) ----------

def load_films(rows: list[dict]) -> dict[str, list[dict]]:
    """token_address -> list of film dicts (arm order, including a still-active
    one with disarmed=None if the last arm has no matching disarm yet)."""
    films: dict[str, list[dict]] = {}
    open_films: dict[str, dict] = {}
    for r in rows:
        token = r["token_address"]
        event = r["event"]
        if event == "arm":
            film = {"token_address": token, "armed_at": r["ts"], "arm_price": r["price"],
                     "arm_liquidity": r["liquidity"], "samples": [], "disarmed": None}
            films.setdefault(token, []).append(film)
            open_films[token] = film
        elif event == "sample":
            film = open_films.get(token)
            if film is not None:
                sample = {k: v for k, v in r.items() if k not in ("token_address", "event")}
                film["samples"].append(sample)
        elif event == "disarm":
            film = open_films.pop(token, None)
            if film is not None:
                film["disarmed"] = r["reason"]
    return films


def film_fingerprints(film: dict) -> dict:
    """Shape signals computed from one film's samples. None where data is
    insufficient rather than a misleading number."""
    samples = film["samples"]
    n = len(samples)

    prices = [s["price"] for s in samples[-30:]]
    base_ratio = (max(prices) / min(prices)
                  if len(prices) >= 2 and all(prices) else None)

    holder_readings = [s["holders"] for s in samples if s.get("holders") is not None]
    holder_growth_pct = None
    if len(holder_readings) >= 2 and holder_readings[0]:
        holder_growth_pct = holder_readings[-1] / holder_readings[0] - 1

    liq_ratio = None
    if samples and film.get("arm_liquidity") and samples[-1].get("liq") is not None:
        liq_ratio = samples[-1]["liq"] / film["arm_liquidity"]

    top_pcts = [s["top_pct"] for s in samples if s.get("top_pct") is not None]
    max_top_pct = max(top_pcts) if top_pcts else None

    return {"n_samples": n, "base_ratio": base_ratio,
            "holder_growth_pct": holder_growth_pct, "liq_ratio": liq_ratio,
            "max_top_pct": max_top_pct}


# ---------- report ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase-2 film scorecard (self-built data)")
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args()
    horizon = time.time() - args.days * 86400

    films_by_token = load_films(_read_jsonl(FILMS_PATH))
    all_films = [(token, f) for token, fs in films_by_token.items() for f in fs
                 if f["armed_at"] >= horizon]
    all_films.sort(key=lambda tf: tf[1]["armed_at"])

    print(f"\n{'=' * 100}\n  FILM FINGERPRINTS vs OUTCOMES — last {args.days:g} days, "
          f"{len(all_films)} films\n{'=' * 100}")
    if not all_films:
        print("  no films yet — let the stakeout recorder collect for a few days")
        return

    print(f"  {'token':<12} {'armed_at':<17} {'n':>3} {'base':>6} {'hgrow':>7} "
          f"{'liq':>6} {'top%':>6} {'status':<12} {'mult':>6}")
    outcomes_2x: list[dict] = []
    outcomes_lt2x: list[dict] = []
    for token, film in all_films:
        fp = film_fingerprints(film)
        mult = None
        if film["arm_price"]:
            mx = fetch_max_price_since(token, film["armed_at"])
            if mx is not None:
                mult = mx / film["arm_price"]
            time.sleep(0.5)                       # GeckoTerminal free-tier politeness

        when = datetime.fromtimestamp(film["armed_at"], tz=timezone.utc).strftime("%m-%d %H:%M")
        status = "active" if film["disarmed"] is None else film["disarmed"]
        base_s = f"{fp['base_ratio']:.2f}" if fp["base_ratio"] is not None else "?"
        hgrow_s = f"{fp['holder_growth_pct']:.1%}" if fp["holder_growth_pct"] is not None else "?"
        liq_s = f"{fp['liq_ratio']:.2f}" if fp["liq_ratio"] is not None else "?"
        top_s = f"{fp['max_top_pct']:.1%}" if fp["max_top_pct"] is not None else "?"
        mult_s = f"{mult:.1f}x" if mult is not None else "?"
        print(f"  {token[:10]:<12} {when:<17} {fp['n_samples']:>3} "
              f"{base_s:>6} {hgrow_s:>7} {liq_s:>6} {top_s:>6} "
              f"{status:<12} {mult_s:>6}")

        if mult is not None:
            (outcomes_2x if mult >= 2 else outcomes_lt2x).append(fp)

    def _median(group: list[dict], key: str):
        vals = [g[key] for g in group if g[key] is not None]
        return f"{statistics.median(vals):.3f}" if vals else "?"

    print(f"\n  scored {len(outcomes_2x) + len(outcomes_lt2x)}/{len(all_films)} films "
          f"({len(outcomes_2x)} did >=2x, {len(outcomes_lt2x)} did not)")
    for key in ("base_ratio", "holder_growth_pct", "liq_ratio", "max_top_pct"):
        print(f"  {key:<18} >=2x median: {_median(outcomes_2x, key):>8}   "
              f"<2x median: {_median(outcomes_lt2x, key):>8}")
    print("  (tight base_ratio + rising holders + stable liq before a >=2x run = "
          "the Phase-2 entry gate; <2x medians show what to gate OUT)")


if __name__ == "__main__":
    main()
