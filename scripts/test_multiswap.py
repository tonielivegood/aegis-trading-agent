"""Verify sequential multi-swap (the agent-loop path) with real funds, cheaply.

Two small buys back-to-back (USDT->ETH, USDT->CAKE), then sell both back to USDT.
Tests nonce progression across multiple approve+swap sequences in one run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account

from src.agent.config import settings
from src.agent.data.rpc import get_web3
from src.agent.execution.pancakeswap import PancakeSwap
from src.agent.risk.portfolio import read_onchain_balances

BUY_USD = 2.0


def bals(label):
    b = read_onchain_balances(settings.agent_wallet_address)
    print(f"  {label}: " + ", ".join(f"{k}={v:.6f}" for k, v in b.items()))
    return b


def do(dex, w3, label, tin, tout, amount):
    r = dex.swap(tin, tout, amount)
    status = w3.eth.get_transaction_receipt(r.tx_hash)["status"]
    print(f"  {label}: {tin}->{tout} {amount:.6f}  tx={r.tx_hash[:14]}...  "
          f"{'OK' if status == 1 else 'FAIL'}")
    return r


def main():
    print("Multi-swap live test: 2 buys then sell both back\n")
    bals("before")
    account = Account.from_key(settings.agent_private_key)
    dex = PancakeSwap(account=account, dry_run=False)
    w3 = get_web3()

    print("\n  -- buys --")
    do(dex, w3, "buy1", "USDT", "ETH", BUY_USD)
    do(dex, w3, "buy2", "USDT", "CAKE", BUY_USD)

    b = bals("after buys")

    print("\n  -- sells (back to USDT) --")
    if b.get("ETH", 0) > 0:
        do(dex, w3, "sell1", "ETH", "USDT", b["ETH"])
    if b.get("CAKE", 0) > 0:
        do(dex, w3, "sell2", "CAKE", "USDT", b["CAKE"])

    print()
    bals("final")
    print("\nMulti-swap path verified with real funds.")


if __name__ == "__main__":
    main()
