"""Pull recent BSC smart-money trades from gmgn-cli and merge the trading wallets into
data/copy_trade/config.json's target_wallets (§1 of the design spec — GMGN is the
primary signal source, capped at the free-tier ceiling of 10 tracked wallets).

Run: python scripts/fetch_gmgn_smart_money.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"


def extract_wallets(trades: list[dict], max_wallets: int) -> list[dict]:
    seen: dict[str, None] = {}
    for t in trades:
        addr = t.get("maker")
        if addr and addr not in seen:
            seen[addr] = None
        if len(seen) >= max_wallets:
            break
    return [
        {
            "address": addr,
            "label": f"GMGN_SMART_{i + 1}",
            "role": "GMGN smart-money (BSC, auto-sourced)",
            "priority": 5,
            "monitor": True,
        }
        for i, addr in enumerate(seen)
    ]


def merge_wallets(existing: list[dict], new: list[dict]) -> list[dict]:
    existing_addrs = {w["address"].lower() for w in existing}
    merged = list(existing)
    for w in new:
        if w["address"].lower() not in existing_addrs:
            merged.append(w)
            existing_addrs.add(w["address"].lower())
    return merged


def main() -> None:
    # ponytail: shutil.which resolves the npm .cmd/.ps1 shim on Windows, where bare
    # "gmgn-cli" isn't found by subprocess without shell=True (PATHEXT isn't applied).
    gmgn_cli = shutil.which("gmgn-cli") or "gmgn-cli"
    proc = subprocess.run(
        [gmgn_cli, "track", "smartmoney", "--chain", "bsc", "--limit", "100", "--raw"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if proc.returncode != 0:
        print(f"gmgn-cli failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    trades = json.loads(proc.stdout).get("list", [])
    wallets = extract_wallets(trades, max_wallets=10)

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["target_wallets"] = merge_wallets(config["target_wallets"], wallets)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Merged {len(wallets)} GMGN smart-money wallets into {CONFIG_PATH}")


if __name__ == "__main__":
    main()
