"""DRY_RUN runner for the Aegis catalyst intelligence layer.

Aggregates all enabled catalyst sources (Tier-1 authority adapters are only
active when their credential/flag is set; the manual JSON feed always runs),
maps events to eligible tokens by contract/symbol, scores with freshness decay,
and prints the per-token catalyst signals. Read-only — never signs/broadcasts.

Run:  python scripts/run_catalyst_scanner.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.aegis.catalyst_scanner import CatalystScanner


def main() -> None:
    scanner = CatalystScanner()
    active = ", ".join(getattr(s, "name", "?") for s in scanner.sources)
    print(f"Active catalyst sources: {active}")
    print("(Tier-1 network adapters appear only when enabled via env; manual feed always on.)\n")

    signals = scanner.scan()
    if not signals:
        print("No catalyst signals from current sources / manual feed.")
        return

    print(f"{'symbol':<10}{'tier':>5}{'score':>7}{'conf':>6}{'matched':>10}{'fresh(s)':>10}  status")
    print("-" * 60)
    for s in signals:
        print(f"{s.symbol:<10}{s.source_tier:>5}{s.score:>7.0f}{s.confidence:>6.2f}"
              f"{s.matched_by:>10}{s.freshness_seconds:>10.0f}  {s.status}")
    print("\nNote: a catalyst signal alone = WATCHLIST. Entry still needs eligible+liquid, "
          "price + real Binance Alpha 5m volume confirmation, and risk gates (all DRY_RUN).")


if __name__ == "__main__":
    main()
