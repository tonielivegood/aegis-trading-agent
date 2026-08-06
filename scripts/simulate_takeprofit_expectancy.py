"""Does "enter at 2x, take profit on the way up" survive REAL trading costs?

The 2026-07-30 finding closed off exits after entry: 24 of 25 deaths went from
healthy liquidity to under $1,000 inside ONE 30s sample, so stop-losses cannot
execute — a loss is a 100% loss, not a percentage. The only exit shape left is
selling into strength before the rug. This measures whether that shape pays,
using the 30s films already on disk; the question resolves inside a single 4h
film, so no outcome labels are needed.

Every entered position is accounted for. There are no exclusions:
  - target reached before the pool drains -> sell at the target
  - pool drains first                     -> 100% loss
  - film ends with neither                -> sell at the last observed price
An earlier version EXCLUDED the still-open positions from the win rate, which
flatters the result: those are disproportionately tokens that went nowhere.

Costs applied: PancakeSwap fee both ways, the token's own buy/sell tax from the
recorded GoPlus record, gas, and price impact from trading against a finite
pool. Fill timing is bracketed rather than guessed — see FillTiming.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.copy_trade.launch_collector import DEAD_LIQ_USD  # noqa: E402

ENTRY_MULTIPLE = 2.0
TARGETS = [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]

# PancakeSwap V2 charges 0.25% per swap; the GoPlus records confirm pool_fee
# "0.0025" on these pairs.
DEX_FEE = 0.0025
# BSC gas for a swap, generously rounded up. Measured receipts put it far lower,
# but it is charged twice (in and out) and it is noise against a 5x payout.
GAS_USD = 0.30


@dataclass(frozen=True)
class Config:
    size_usd: float = 100.0
    # 0 = fill at the price the bot saw when it decided (a BSC block is 0.45s,
    # so a tx really does land almost immediately). 1 = fill a full 30s poll
    # later. The truth is bracketed by running both; neither is a guess.
    fill_delay: int = 1
    dex_fee: float = DEX_FEE
    gas_usd: float = GAS_USD


# A price that moves more than this between two consecutive 30s samples is a bad
# print, not a trade. Real winners climb far more gradually — the median biggest
# single-sample jump across winners is x1.18. Measured 5/8: one "winner" jumped
# x22,996 in one sample, and DexScreener has separately returned $1.7 BILLION
# liquidity for a $30k pool, so bad prints in this feed are established fact.
MAX_SANE_SAMPLE_JUMP = 10.0


def load_token_samples(path: Path) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Samples per token, plus a count of what was rejected and why.

    Only the FIRST film of a token is kept. A restart clears the collector's
    in-memory `seen` set, so 30 tokens were armed more than once; concatenating
    those films by address stitches two separate price histories into one series
    and invents moves that never happened.
    """
    films: dict[str, list[list[dict]]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            tok = row.get("token_address")
            if not tok:
                continue
            if row.get("event") == "arm":
                films.setdefault(tok, []).append([])
            elif row.get("event") == "sample" and films.get(tok):
                films[tok][-1].append(row)

    rejected = {"re_armed": 0, "bad_print": 0}
    out: dict[str, list[dict]] = {}
    for tok, takes in films.items():
        if len(takes) > 1:
            rejected["re_armed"] += 1
        samples = sorted(takes[0], key=lambda s: s.get("ts") or 0.0)
        if _has_bad_print(samples):
            rejected["bad_print"] += 1
            continue
        if samples:
            out[tok] = samples
    return out, rejected


def _has_bad_print(samples: list[dict]) -> bool:
    prices = [s["price"] for s in samples if s.get("price")]
    return any(prices[i + 1] / prices[i] > MAX_SANE_SAMPLE_JUMP
               or prices[i] / prices[i + 1] > MAX_SANE_SAMPLE_JUMP
               for i in range(len(prices) - 1))


def load_goplus(path: Path) -> dict[str, dict]:
    """Latest GoPlus record per token; `security` rows supersede the launch row."""
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("goplus"):
                out[row["token_address"]] = row["goplus"]
    return out


def _tax(goplus: dict | None, key: str) -> float:
    """A tax GoPlus cannot state is treated as zero — it is unknown, not high.
    Measured 5/8: among tokens that reached a 6x target, 0 of 173 covered were
    honeypots and the worst sell_tax was 2%, so this is not where the risk is."""
    try:
        return float((goplus or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_unsellable(goplus: dict | None) -> bool:
    g = goplus or {}
    return g.get("is_honeypot") == "1" or g.get("cannot_sell_all") == "1"


def find_entry(samples: list[dict], require_dominance: bool = False) -> int | None:
    """Index of the first sample at >= ENTRY_MULTIPLE x the arm price."""
    if not samples:
        return None
    base = samples[0].get("price")
    if not base:
        return None
    for i, s in enumerate(samples):
        if s.get("price") is None or s["price"] < base * ENTRY_MULTIPLE:
            continue
        if require_dominance and not ((s.get("buys_m5") or 0) > (s.get("sells_m5") or 0)):
            continue
        return i
    return None


def _impact(size_usd: float, liq_usd: float | None) -> float:
    """Fraction of price lost to trading against a finite pool. Constant-product
    slippage is ~size/liquidity for small trades.
    ponytail: linear approximation, only valid while size << liquidity. At the
    $100 sizes here that is 0.2% against a $50k pool. Revisit above ~1% of pool."""
    if not liq_usd or liq_usd <= 0:
        return 1.0
    return min(size_usd / liq_usd, 1.0)


def simulate_token(samples: list[dict], goplus: dict | None, target: float,
                   cfg: Config = Config(),
                   require_dominance: bool = False) -> dict | None:
    """Net multiple returned on one position, or None if it never triggered."""
    i = find_entry(samples, require_dominance)
    if i is None:
        return None
    after = samples[i:]
    buy_i = min(cfg.fill_delay, len(after) - 1)
    buy = after[buy_i]
    if not buy.get("price"):
        return None

    # EVERY return below carries entry_ts and exit_ts. The bankroll walk sorts on
    # entry_ts, so a path that omitted them sorted its trades to the very front —
    # and since the omitting paths were the total losses, that stacked ~75% of
    # the losses ahead of every winner and reported -100% at every position size.
    def _done(outcome, net, exit_ts):
        return {"outcome": outcome, "net_multiple": net,
                "entry_ts": buy.get("ts"), "exit_ts": exit_ts}

    # Bought into a pool that had already drained: the position is worthless.
    if buy.get("liq") is not None and buy["liq"] < DEAD_LIQ_USD:
        return _done("dead_on_arrival", 0.0, buy.get("ts"))
    # A token that cannot be sold is a total loss no matter what the price does,
    # and the capital never comes back — hold the slot to the end of the film.
    if is_unsellable(goplus):
        return _done("unsellable", 0.0, after[-1].get("ts"))

    buy_px = buy["price"] * (1 + _impact(cfg.size_usd, buy.get("liq")))
    spent = cfg.size_usd + cfg.gas_usd
    tokens = (cfg.size_usd * (1 - cfg.dex_fee) * (1 - _tax(goplus, "buy_tax"))) / buy_px

    rest = after[buy_i:]
    death_i = next((j for j, s in enumerate(rest)
                    if s.get("liq") is not None and s["liq"] < DEAD_LIQ_USD), None)
    want = buy["price"] * target
    hit_i = next((j for j, s in enumerate(rest)
                  if s.get("price") is not None and s["price"] >= want), None)

    if hit_i is not None and (death_i is None or hit_i <= death_i):
        sell_i, outcome = min(hit_i + cfg.fill_delay, len(rest) - 1), "win"
    elif death_i is not None:
        return _done("rugged", 0.0, rest[death_i].get("ts"))
    else:
        sell_i, outcome = len(rest) - 1, "open_closed_at_end"

    sell = rest[sell_i]
    if not sell.get("price") or (sell.get("liq") is not None
                                 and sell["liq"] < DEAD_LIQ_USD):
        return _done("rugged", 0.0, sell.get("ts"))

    sell_px = sell["price"] * (1 - _impact(cfg.size_usd, sell.get("liq")))
    proceeds = (tokens * sell_px * (1 - cfg.dex_fee)
                * (1 - _tax(goplus, "sell_tax")) - cfg.gas_usd)
    return _done(outcome, max(proceeds, 0.0) / spent, sell.get("ts"))


def _terminal(rows: list[dict], cfg: Config, bankroll: float,
              max_concurrent: int) -> float:
    """Walk the trades in the order they actually happened, holding capital for
    the life of each position. Expectancy alone cannot say whether a strategy
    survives: this one loses everything on ~75% of trades, so position size and
    how much capital is tied up at once decide whether the edge is ever reached."""
    open_pos: list[tuple[float, float]] = []       # (exit_ts, payout)
    for r in sorted(rows, key=lambda r: r.get("entry_ts") or 0.0):
        now = r.get("entry_ts") or 0.0
        for exit_ts, payout in [p for p in open_pos if p[0] <= now]:
            bankroll += payout
        open_pos = [p for p in open_pos if p[0] > now]
        if len(open_pos) >= max_concurrent or bankroll < cfg.size_usd:
            continue                                # no slot, or cannot afford it
        bankroll -= cfg.size_usd
        open_pos.append((r.get("exit_ts") or now,
                         r["net_multiple"] * cfg.size_usd))
    return bankroll + sum(payout for _, payout in open_pos)


def summarize(results: list[dict], target: float) -> dict:
    n = len(results)
    if not n:
        return {"n": 0}
    mults = [r["net_multiple"] for r in results]
    wins = sum(1 for r in results if r["outcome"] == "win")
    total = sum(1 for r in results if r["net_multiple"] == 0.0)
    profitable = sum(1 for m in mults if m > 1.0)
    return {
        "target": target, "n": n, "wins": wins, "total_losses": total,
        "profitable": profitable, "profit_rate": profitable / n,
        "expectancy": sum(mults) / n - 1.0,
        "median_multiple": sorted(mults)[n // 2],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1] / "data" / "launch_collector"
    ap.add_argument("--films", default=str(root / "films.jsonl"))
    ap.add_argument("--snapshots", default=str(root / "snapshots.jsonl"))
    ap.add_argument("--size-usd", type=float, default=100.0)
    ap.add_argument("--dominance", action="store_true",
                    help="also require buys_m5 > sells_m5 at entry")
    ap.add_argument("--risk", type=float, metavar="TARGET",
                    help="bankroll walk at this take-profit target instead")
    ap.add_argument("--bankroll", type=float, default=500.0)
    args = ap.parse_args()

    by_token, rejected = load_token_samples(Path(args.films))
    goplus = load_goplus(Path(args.snapshots))
    print(f"excluded: {rejected['re_armed']} re-armed (only first film kept), "
          f"{rejected['bad_print']} with a bad price print")

    if args.risk:
        print(f"bankroll walk @ {args.risk}x target, start ${args.bankroll:.0f}, "
              f"{len(by_token)} tokens filmed over the collected window\n")
        print(f"{'size':>7} {'slots':>6} {'final':>10} {'return':>9}")
        for size in (5.0, 10.0, 25.0, 50.0, 100.0):
            cfg = Config(size_usd=size, fill_delay=1)
            rows = [r for r in
                    (simulate_token(s, goplus.get(t), args.risk, cfg, args.dominance)
                     for t, s in by_token.items()) if r]
            for slots in (3, 5, 10):
                end = _terminal(rows, cfg, args.bankroll, slots)
                print(f"{size:>6.0f}$ {slots:>6} {end:>10.2f} "
                      f"{end / args.bankroll - 1:>+8.1%}")
        return

    print(f"tokens with film data: {len(by_token)}   size ${args.size_usd:.0f}/trade")
    for delay, label in ((0, "immediate fill"), (1, "one 30s poll late")):
        cfg = Config(size_usd=args.size_usd, fill_delay=delay)
        print(f"\n--- {label} ---")
        print(f"{'target':>7} {'trades':>7} {'wins':>6} {'zeros':>6} "
              f"{'profitable':>11} {'median':>8} {'expectancy':>11}")
        for target in TARGETS:
            rows = [r for r in
                    (simulate_token(s, goplus.get(t), target, cfg, args.dominance)
                     for t, s in by_token.items()) if r]
            st = summarize(rows, target)
            if not st["n"]:
                continue
            print(f"{target:>6.1f}x {st['n']:>7} {st['wins']:>6} "
                  f"{st['total_losses']:>6} {st['profit_rate']:>10.1%} "
                  f"{st['median_multiple']:>8.3f} {st['expectancy']:>+11.3f}")


if __name__ == "__main__":
    main()
