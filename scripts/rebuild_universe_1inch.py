"""Rebuild the tradable universe using 1inch aggregator routing (not Pancake-V2).

For every eligible token, ask 1inch what it can actually fill a ~$12 USDT buy at,
measure the price impact ($12 route vs a near-spot $1 route), and keep tokens that
clear the gate (major ≤4%, meme ≤6%). Pancake-V2 quoting wrongly excluded most
liquid majors (UNI/DOT/AAVE showed 30-93%); 1inch fills them at <0.5%.

Writes a NEW src/agent/data/tradable_alpha.json (decimals fetched on-chain).
Run on the VPS (has ONEINCH_API_KEY + RPC):  .venv/bin/python scripts/rebuild_universe_1inch.py
"""
from __future__ import annotations

import io
import json
import time

import requests
from web3 import Web3

from src.agent.config import settings
from src.agent.data.rpc import get_web3
from src.agent.data.token_list import STABLECOINS

USDT = "0x55d398326f99059ff775485246999027b3197955"
BASE = "https://api.1inch.dev/swap/v6.0/56"
HEADERS = {"Authorization": f"Bearer {settings.oneinch_api_key}"}
GATE = {"major": 0.04, "meme": 0.06}
DATA = "src/agent/data/eligible_resolved.json"
OUT = "src/agent/data/tradable_alpha_new.json"   # staging — review before replacing the live file
_DECIMALS_SEL = "0x313ce567"


def _quote_out(contract: str, usdt_amount: int) -> int | None:
    try:
        r = requests.get(f"{BASE}/quote", headers=HEADERS, timeout=12, params={
            "src": USDT, "dst": Web3.to_checksum_address(contract),
            "amount": usdt_amount * 10**18})
        if r.status_code != 200:
            return None
        return int(r.json().get("dstAmount", 0) or 0)
    except Exception:
        return None


def _impact(contract: str) -> float | None:
    out12 = _quote_out(contract, 12)
    if not out12:
        return None
    time.sleep(1.1)
    out1 = _quote_out(contract, 1)
    if not out1:
        return None
    rate1, rate12 = out1 / 1.0, out12 / 12.0
    return max(0.0, (rate1 - rate12) / rate1) if rate1 > 0 else None


def _decimals(w3, contract: str) -> int:
    try:
        raw = w3.eth.call({"to": Web3.to_checksum_address(contract), "data": _DECIMALS_SEL})
        return int.from_bytes(raw, "big") or 18
    except Exception:
        return 18


def main() -> None:
    w3 = get_web3()
    toks = [t for t in json.load(io.open(DATA, encoding="utf-8"))
            if t["symbol"] not in STABLECOINS and t.get("contract")]
    out = []
    for i, t in enumerate(toks, 1):
        cls = t.get("token_class", "meme")
        imp = _impact(t["contract"])
        time.sleep(1.1)
        status = "no-route" if imp is None else f"{imp*100:.2f}%"
        keep = imp is not None and imp <= GATE.get(cls, 0.06)
        if keep:
            out.append({"symbol": t["symbol"], "contract": Web3.to_checksum_address(t["contract"]),
                        "decimals": _decimals(w3, t["contract"]), "token_class": cls,
                        "slippage_12usd": round(imp, 4), "id": t.get("id")})
        print(f"[{i}/{len(toks)}] {t['symbol']:<10} {cls:<6} {status:<9} {'KEEP' if keep else 'drop'}", flush=True)
    out.sort(key=lambda x: (x["token_class"], x["slippage_12usd"]))
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    nmaj = sum(1 for x in out if x["token_class"] == "major")
    print(f"\nNEW tradable_alpha.json: {len(out)} tokens ({nmaj} major + {len(out)-nmaj} meme)")


if __name__ == "__main__":
    main()
