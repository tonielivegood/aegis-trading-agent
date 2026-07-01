"""Region/compliance check for Binance's Web3 Wallet (W3W) API — run this on a
CANDIDATE VPS/location BEFORE migrating the bot there.

Read-only: signs one price lookup for USDT/BSC, no swap, no signing of any
blockchain transaction, no broadcast. Binance's compliance block (code 40304,
confirmed from the old Hostinger-US VPS) is about the calling IP/region, not
the API key/secret — so this tells you whether a candidate machine can reach
W3W before you spend time migrating the whole bot to it.

Needs BINANCE_WEB3_API_KEY + BINANCE_WEB3_API_SECRET in the environment/.env
(the same ones already used for execution — never paste them anywhere else).

Run:  python scripts/binance_w3w_region_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.execution import binance_web3


def main() -> None:
    print("Binance W3W region/compliance check (read-only, no signing, no broadcast)\n")
    r = binance_web3.check_region()
    print(f"  endpoint    : {r.endpoint}")
    print(f"  credentials : {'present' if r.has_credentials else 'MISSING'}")
    print(f"  api_code    : {r.api_code}")
    print(f"  detail      : {r.detail}")
    print()
    if not r.has_credentials:
        print("Set BINANCE_WEB3_API_KEY and BINANCE_WEB3_API_SECRET in .env, then re-run.")
    elif r.ok:
        print("GO — this IP/region can reach Binance W3W. Safe to migrate the bot here.")
    else:
        print("BLOCKED — do NOT migrate the bot to this location; try a different region.")


if __name__ == "__main__":
    main()
