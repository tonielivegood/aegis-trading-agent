"""Accelerated soak test — hammer the live tick loop in dry-run to surface
operational bugs (RPC flakiness, CMC limits, state persistence, crashes) that
unit tests don't catch. Moves no money. Uses an isolated runtime dir so it never
corrupts real drawdown/trade state.
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.agent.agent_loop as al

SOAK_RUNTIME = Path("data/runtime/soak")
SOAK_RUNTIME.mkdir(parents=True, exist_ok=True)


def run_phase(name: str, ticks: int, balances_fn) -> dict:
    al.read_onchain_balances = balances_fn  # may be real or simulated
    al.DRAWDOWN_FILE = SOAK_RUNTIME / f"{name}_drawdown.json"
    al.TRADES_FILE = SOAK_RUNTIME / f"{name}_trades.json"
    # fresh state for the phase
    for f in (al.DRAWDOWN_FILE, al.TRADES_FILE):
        if f.exists():
            f.unlink()

    durations, equities, errors = [], [], []
    n_orders_total = 0
    print(f"\n--- Phase '{name}': {ticks} ticks ---")
    for i in range(ticks):
        t0 = time.time()
        try:
            res = al.tick(dry_run=True)
            dt = time.time() - t0
            durations.append(dt)
            equities.append(res["equity"])
            n_orders_total += res["orders"]
            if i < 3 or i == ticks - 1:
                print(f"  tick {i:2d}: {dt:5.2f}s  equity=${res['equity']:.2f}  "
                      f"orders={res['orders']}  action={res['action']}")
        except Exception:
            errors.append(traceback.format_exc())
            print(f"  tick {i:2d}: ERROR")

    # verify state files were written
    state_ok = al.DRAWDOWN_FILE.exists() and al.TRADES_FILE.exists()
    return {
        "name": name, "ticks": ticks, "errors": errors,
        "avg_s": sum(durations) / len(durations) if durations else 0,
        "max_s": max(durations) if durations else 0,
        "equity_first": equities[0] if equities else None,
        "equity_last": equities[-1] if equities else None,
        "orders_total": n_orders_total, "state_ok": state_ok,
    }


def main() -> None:
    real_balances = al.read_onchain_balances  # capture the real function

    def funded(_wallet):
        return {"USDT": 100.0, "BNB": 0.02}

    results = []
    results.append(run_phase("real_wallet", 12, real_balances))
    results.append(run_phase("funded_sim", 12, funded))

    print("\n" + "=" * 60)
    print("SOAK SUMMARY")
    print("=" * 60)
    total_err = 0
    for r in results:
        ne = len(r["errors"])
        total_err += ne
        print(f"{r['name']:<14} ticks={r['ticks']} errors={ne} "
              f"avg={r['avg_s']:.2f}s max={r['max_s']:.2f}s "
              f"equity {r['equity_first']:.2f}->{r['equity_last']:.2f} "
              f"orders={r['orders_total']} state_ok={r['state_ok']}")
    print(f"\nTOTAL ERRORS: {total_err}")
    if total_err:
        print("\nFirst error traceback:")
        for r in results:
            if r["errors"]:
                print(r["errors"][0])
                break
    else:
        print("CLEAN — no crashes across all ticks.")


if __name__ == "__main__":
    main()
