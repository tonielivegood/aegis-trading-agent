"""Region check for the Binance FUTURES API (fapi.binance.com) — separate from
scripts/binance_w3w_region_check.py, which only tests the Wallet Web3 API.

Futures and W3W are different products with independent geo-blocking, so
passing one does NOT mean the other passes. This hits a PUBLIC, unauthenticated
endpoint (no key needed) — Binance returns HTTP 451 ("Unavailable For Legal
Reasons") when the calling IP is in a restricted location for derivatives.

Note: this only tells you if the IP itself is geo-blocked. Binance also gates
futures ACCESS by the account's own KYC-verified country of residence,
independent of which IP you connect from — this script can't check that part.

Run:  python scripts/binance_futures_region_check.py
"""
import requests

_URL = "https://fapi.binance.com/fapi/v1/ping"
_TIMEOUT_S = 15


def main() -> None:
    print("Binance Futures API region check (public endpoint, no key needed)\n")
    print(f"  endpoint : {_URL}")
    try:
        resp = requests.get(_URL, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        print(f"  result   : unreachable ({type(e).__name__})")
        return
    print(f"  status   : {resp.status_code}")
    if resp.status_code == 451:
        print("\nBLOCKED — this IP/region is restricted for Binance Futures.")
    elif resp.status_code == 200:
        print("\nGO — this IP can reach the Binance Futures API.")
        print("(This only checks the IP. Your account's own KYC country of residence")
        print(" separately determines whether Futures is actually offered to you.)")
    else:
        print(f"\nUnexpected status {resp.status_code} — investigate before relying on this.")


if __name__ == "__main__":
    main()
