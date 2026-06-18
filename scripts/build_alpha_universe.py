"""Build the TRADABLE eligible-token universe for the contest's 149-token allowlist.

Routability alone isn't enough — a thin/honeypot pool will eat a small order in
slippage. For each eligible token we:
  1. fetch on-chain decimals (ERC-20),
  2. get spot price via PancakeSwap (token->WBNB->USDT, fallback token->USDT),
  3. estimate the price impact of a small (~PROBE_USD) market buy.

We keep tokens that are routable AND whose small-trade slippage is under
MAX_SLIPPAGE. Output -> src/agent/data/tradable_alpha.json, the universe the
live agent actually trades. Re-run before go-live (Alpha liquidity moves fast).

Offline tooling; never imported by the live loop.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web3 import Web3

from src.agent.config import settings
from src.agent.data.price_feed import ROUTER_ABI
from src.agent.data.rpc import get_web3

REPO = Path(__file__).resolve().parent.parent
ELIGIBLE = REPO / "src" / "agent" / "data" / "eligible_tokens.json"
OUT = REPO / "src" / "agent" / "data" / "tradable_alpha.json"

PROBE_USD = 5.0          # representative order size on a ~$36 wallet
MAX_SLIPPAGE = 0.06      # drop tokens where a $5 buy moves price >6%
USDT_DECIMALS = 18       # BSC USDT

_ERC20_DECIMALS_ABI = [{
    "inputs": [], "name": "decimals",
    "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
    "stateMutability": "view", "type": "function",
}]


def main() -> None:
    elig = json.loads(ELIGIBLE.read_text(encoding="utf-8"))
    w3 = get_web3()
    router = w3.eth.contract(
        address=Web3.to_checksum_address(settings.pancake_router), abi=ROUTER_ABI)
    USDT = Web3.to_checksum_address(settings.usdt_address)
    WBNB = Web3.to_checksum_address(settings.wbnb_address)

    def quote(addr, amount_wei):
        paths = [[addr, WBNB, USDT], [addr, USDT]]
        if addr.lower() == WBNB.lower():
            paths = [[addr, USDT]]
        for p in paths:
            try:
                amounts = router.functions.getAmountsOut(amount_wei, p).call()
                if amounts and amounts[-1] > 0:
                    return amounts[-1] / 10 ** USDT_DECIMALS
            except Exception:
                continue
        return None

    tradable, dropped = [], []
    for t in elig:
        sym, addr_raw = t.get("symbol", "?"), t.get("contract")
        if not addr_raw:
            dropped.append((sym, "no addr"))
            continue
        try:
            addr = Web3.to_checksum_address(addr_raw)
        except Exception:
            dropped.append((sym, "bad addr"))
            continue

        try:
            dec = w3.eth.contract(address=addr, abi=_ERC20_DECIMALS_ABI).functions.decimals().call()
        except Exception:
            dec = 18
        one = 10 ** dec

        spot = quote(addr, one)                 # USD price of 1 token
        if not spot or spot <= 0:
            dropped.append((sym, "no route"))
            continue

        tokens_for_probe = PROBE_USD / spot
        out_probe = quote(addr, int(tokens_for_probe * one))
        if not out_probe or out_probe <= 0:
            dropped.append((sym, "no depth"))
            continue
        eff_price = out_probe / tokens_for_probe
        slippage = max(0.0, (spot - eff_price) / spot)

        if slippage <= MAX_SLIPPAGE:
            tradable.append({
                "symbol": sym, "contract": addr, "decimals": dec,
                "price": round(spot, 8), "slippage_5usd": round(slippage, 4),
                "id": t.get("id"),
            })
        else:
            dropped.append((sym, f"slip {slippage:.0%}"))

    tradable.sort(key=lambda x: x["slippage_5usd"])  # deepest liquidity first
    OUT.write_text(json.dumps(tradable, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"eligible: {len(elig)}   TRADABLE (routable + depth): {len(tradable)}   dropped: {len(dropped)}")
    print(f"written -> {OUT.relative_to(REPO)}\n")
    print(f"{'symbol':<14}{'price($)':>14}{'slip@$5':>9}")
    print("-" * 37)
    for t in tradable[:30]:
        print(f"{t['symbol']:<14}{t['price']:>14.8f}{t['slippage_5usd']*100:>8.1f}%")
    print(f"\n... {len(tradable)} total. dropped reasons sample:",
          ", ".join(f"{s}({r})" for s, r in dropped[:12]))


if __name__ == "__main__":
    main()
