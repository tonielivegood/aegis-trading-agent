"""Safe, read-only Binance Wallet Web3 API environment check.

Prints the Web3 config status with the API key MASKED (never the full key),
shows which capabilities are enabled, and — only if the layer is enabled and a
key is present — performs a harmless connectivity probe. It NEVER signs, NEVER
broadcasts, and NEVER sends a transaction. Fails safe with a clear message when
configuration is missing.

Run:  python scripts/check_binance_web3_env.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.config import mask_secret, settings
from src.agent.execution import binance_web3


def main() -> None:
    print("Binance Wallet Web3 API — environment check (read-only)\n")
    print(f"  BINANCE_WEB3_ENABLED            : {settings.binance_web3_enabled}")
    print(f"  API key                        : {mask_secret(settings.binance_web3_api_key)}")
    print(f"  API secret                     : {mask_secret(settings.binance_web3_api_secret)}")
    print(f"  base URL                       : {settings.binance_web3_base_url}")
    print(f"  quote enabled                  : {settings.binance_web3_quote_enabled}")
    print(f"  execution (unsigned) enabled   : {settings.binance_web3_execution_enabled}")
    print(f"  broadcast enabled              : {settings.binance_web3_broadcast_enabled}  (must stay False)")
    print(f"  MEV protection                 : {settings.binance_web3_mev_protection_enabled}")
    print(f"  Alpha market data enabled      : {settings.binance_alpha_market_data_enabled}")
    print(f"  DRY_RUN                        : {settings.dry_run}")

    if settings.binance_web3_broadcast_enabled:
        print("\n  WARNING: broadcast flag is True — Aegis still never auto-signs/broadcasts here.")

    if not settings.binance_web3_enabled:
        print("\nBinance Web3 layer is DISABLED. Set BINANCE_WEB3_ENABLED=true in .env to enable. "
              "(Safe default; Alpha market data + execution remain on their own paths.)")
        return
    if not settings.binance_web3_api_key:
        print("\nNo BINANCE_WEB3_API_KEY set — failing safe. Add it to your local .env "
              "(never paste it into chat).")
        return

    print("\nRunning harmless connectivity probe (no signing, no broadcast)...")
    r = binance_web3.connectivity_check()
    print(f"  endpoint  : {r.endpoint}")
    print(f"  reachable : {r.reachable}  (status={r.status})")
    print(f"  detail    : {r.detail}")


if __name__ == "__main__":
    main()
