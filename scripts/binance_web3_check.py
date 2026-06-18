"""Safe connectivity check for the Binance Wallet Web3 API.

Reads BINANCE_WEB3_API_KEY from the environment, prints a MASKED status, and
probes a harmless market endpoint. Never signs, never broadcasts, never prints
the full key. Run:  python scripts/binance_web3_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.execution import binance_web3


def main() -> None:
    r = binance_web3.connectivity_check()
    print("Binance Web3 API connectivity check")
    print(f"  key present : {r.has_key}")
    print(f"  endpoint    : {r.endpoint}")
    print(f"  reachable   : {r.reachable}  (status={r.status})")
    print(f"  detail      : {r.detail}")
    if not r.has_key:
        print("\nSet BINANCE_WEB3_API_KEY in your environment (do not paste it here).")


if __name__ == "__main__":
    main()
