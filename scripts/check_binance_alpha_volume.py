"""Read-only check for the Binance Alpha 5m volume provider.

Loads the contract->Alpha mapping and queries 5m klines for a few mapped tokens,
printing the normalized volume object. Never signs/broadcasts. If the mapping is
empty or the endpoint is unreachable, prints the fail-safe reason honestly.

Run:  python scripts/check_binance_alpha_volume.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.aegis.binance_alpha_volume import BinanceAlphaKlinesVolumeProvider


def main() -> None:
    p = BinanceAlphaKlinesVolumeProvider()
    print(f"Alpha symbol map entries: {len(p.map)}")
    print(f"API base: {p.api_base}  interval: {p.interval}  baseline candles: {p.baseline_candles}")
    if not p.map:
        print("\nNo mapping — run: python scripts/build_alpha_symbol_map.py (needs network).")
        print("Volume confirmation will FAIL SAFE (no volume signals) until built.")
        return

    for contract in list(p.map)[:5]:
        entry = p.map[contract]
        v = p.get(contract)
        status = "OK" if v.available else f"UNAVAILABLE ({v.unavailable_reason})"
        print(f"\n{entry.get('symbol','?'):<10} {entry['alpha_symbol']:<16} {status}")
        if v.available:
            print(f"   quote_vol_5m={v.current_quote_volume_5m:.0f}  "
                  f"baseline={v.baseline_quote_volume:.0f}  x{v.volume_multiple:.2f}  "
                  f"trades={v.trade_count_5m}  fresh={v.freshness_seconds:.0f}s  "
                  f"conf={v.confidence:.1f}  src={v.source}")


if __name__ == "__main__":
    main()
