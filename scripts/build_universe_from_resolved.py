"""Build the live universe from the volume-resolved 149 list.

Input : src/agent/data/eligible_resolved.json (147 resolved {symbol,id,name,contract,
        vol24h,token_class}).
Output:
  - eligible_tokens.json  = the full allowlist (147) for is_eligible() (by contract).
  - tradable_alpha.json   = non-stable tokens that have an on-chain PancakeSwap route
                            with acceptable depth, carrying decimals + token_class.

Stablecoins are eligible but never momentum-traded (settlement only), so they are
excluded from the tradable set. On-chain calls are fanned out so the build is fast.
Offline tooling; never imported by the live loop. Re-run before go-live.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web3 import Web3

from src.agent.config import settings
from src.agent.data.price_feed import ROUTER_ABI
from src.agent.data.rpc import get_web3

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "src" / "agent" / "data"
RESOLVED = DATA / "eligible_resolved.json"

PROBE_USD = 5.0
MAX_SLIPPAGE = 0.06
USDT_DECIMALS = 18
STABLE = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "FRAX", "USDD", "USDE", "USD1",
          "LISUSD", "FRXUSD", "USDF", "DUSD", "EURI", "XUSD", "STABLE", "BUSD"}

_DEC_ABI = [{"inputs": [], "name": "decimals",
             "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
             "stateMutability": "view", "type": "function"}]


def main() -> None:
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8"))

    # 1) full allowlist (by contract) — every eligible token, stablecoins included.
    (DATA / "eligible_tokens.json").write_text(
        json.dumps([{"id": t.get("id"), "symbol": t["symbol"], "name": t.get("name", t["symbol"]),
                     "contract": t["contract"], "token_class": t.get("token_class", "meme")}
                    for t in resolved], ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) tradable subset: non-stable, routable, deep enough.
    w3 = get_web3()
    router = w3.eth.contract(address=Web3.to_checksum_address(settings.pancake_router), abi=ROUTER_ABI)
    USDT = Web3.to_checksum_address(settings.usdt_address)
    WBNB = Web3.to_checksum_address(settings.wbnb_address)

    def quote(addr, amount_wei):
        paths = [[addr, USDT]] if addr.lower() == WBNB.lower() else [[addr, WBNB, USDT], [addr, USDT]]
        for p in paths:
            try:
                a = router.functions.getAmountsOut(amount_wei, p).call()
                if a and a[-1] > 0:
                    return a[-1] / 10 ** USDT_DECIMALS
            except Exception:  # noqa: BLE001
                continue
        return None

    candidates = [t for t in resolved if t["symbol"].upper() not in STABLE]

    def assess(t):
        sym = t["symbol"]
        try:
            addr = Web3.to_checksum_address(t["contract"])
        except Exception:  # noqa: BLE001
            return None, (sym, "bad addr")
        try:
            dec = w3.eth.contract(address=addr, abi=_DEC_ABI).functions.decimals().call()
        except Exception:  # noqa: BLE001
            dec = 18
        one = 10 ** dec
        spot = quote(addr, one)
        if not spot or spot <= 0:
            return None, (sym, "no route")
        toks = PROBE_USD / spot
        outp = quote(addr, int(toks * one))
        if not outp or outp <= 0:
            return None, (sym, "no depth")
        slip = max(0.0, (spot - outp / toks) / spot)
        if slip > MAX_SLIPPAGE:
            return None, (sym, f"slip {slip:.0%}")
        return {"symbol": sym, "contract": addr, "decimals": dec,
                "token_class": t.get("token_class", "meme"), "price": round(spot, 8),
                "slippage_5usd": round(slip, 4), "id": t.get("id")}, None

    tradable, dropped = [], []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for ok, bad in ex.map(assess, candidates):
            (tradable if ok else dropped).append(ok or bad)

    tradable.sort(key=lambda x: x["slippage_5usd"])
    (DATA / "tradable_alpha.json").write_text(
        json.dumps(tradable, indent=2, ensure_ascii=False), encoding="utf-8")

    maj = sum(1 for t in tradable if t["token_class"] == "major")
    print(f"allowlist={len(resolved)}  candidates(non-stable)={len(candidates)}  "
          f"TRADABLE={len(tradable)} (major={maj}, meme={len(tradable)-maj})  dropped={len(dropped)}")
    print("dropped sample:", ", ".join(f"{s}({r})" for s, r in dropped[:15]))


if __name__ == "__main__":
    main()
