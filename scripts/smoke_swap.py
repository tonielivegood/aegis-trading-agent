"""ONE small REAL swap to verify the live execution path: 2 USDT -> WBNB.

Broadcasts real transactions (approval + swap) and waits for receipts.
Run intentionally; this moves real (tiny) funds.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account

from src.agent.config import settings
from src.agent.data.rpc import get_web3
from src.agent.execution.pancakeswap import PancakeSwap
from src.agent.risk.portfolio import read_onchain_balances

AMOUNT_USDT = 2.0


def show_balances(label: str) -> None:
    bals = read_onchain_balances(settings.agent_wallet_address)
    print(f"  {label}: " + ", ".join(f"{k}={v:.6f}" for k, v in bals.items()))


def main() -> None:
    print(f"Smoke test: swap {AMOUNT_USDT} USDT -> WBNB on PancakeSwap (REAL)\n")
    show_balances("before")

    account = Account.from_key(settings.agent_private_key)
    dex = PancakeSwap(account=account, dry_run=False)

    q = dex.quote("USDT", "WBNB", AMOUNT_USDT)
    print(f"\n  quote: {AMOUNT_USDT} USDT -> expected {q.expected_out_wei / 1e18:.8f} WBNB"
          f"  (min {q.min_out_wei / 1e18:.8f} after slippage)")
    print("  path:", " -> ".join(q.path))

    print("\n  executing (approval + swap, waiting for receipts)...")
    result = dex.swap("USDT", "WBNB", AMOUNT_USDT)
    print(f"  swap simulated={result.simulated}  tx={result.tx_hash}")
    print(f"  explorer: https://bscscan.com/tx/{result.tx_hash}")

    # confirm receipt status
    w3 = get_web3()
    rcpt = w3.eth.get_transaction_receipt(result.tx_hash)
    print(f"  receipt status: {'SUCCESS' if rcpt['status'] == 1 else 'FAILED'}  "
          f"gas used: {rcpt['gasUsed']}")

    print()
    show_balances("after")


if __name__ == "__main__":
    main()
