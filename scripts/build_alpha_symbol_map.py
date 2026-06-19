"""Build data/alpha_symbol_map.json: eligible BSC contract -> Binance Alpha symbol.

Fetches the official Binance Alpha token list and matches entries to our eligible
allowlist by CONTRACT ADDRESS (chainId 56). We never invent a mapping: a contract
that isn't in the Alpha list (or can't be fetched) is simply omitted, and the
volume provider then reports that token's volume as UNAVAILABLE (fail-safe).

Run:  python scripts/build_alpha_symbol_map.py
If the Alpha list can't be fetched here, the map is left empty and the radar runs
without volume confirmation (safe) until it can be built on a networked host.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests

from src.agent.config import settings

REPO = Path(__file__).resolve().parent.parent
ELIGIBLE = REPO / "src" / "agent" / "data" / "eligible_tokens.json"
OUT = REPO / "src" / "agent" / "data" / "alpha_symbol_map.json"

# Public Binance Alpha token list (read-only). Override via BINANCE_ALPHA_API_BASE.
TOKEN_LIST_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
CHAIN_ID_BSC = "56"


def _fetch_alpha_tokens() -> list[dict]:
    url = settings.binance_alpha_api_base.rstrip("/") + TOKEN_LIST_PATH
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    return data or []


def main() -> None:
    eligible = json.loads(ELIGIBLE.read_text(encoding="utf-8"))
    elig_by_contract = {(t.get("contract") or "").lower(): t for t in eligible if t.get("contract")}

    try:
        alpha_tokens = _fetch_alpha_tokens()
    except Exception as e:  # noqa: BLE001
        print(f"Could not fetch Binance Alpha token list ({type(e).__name__}). "
              f"Writing empty map — volume confirmation will fail safe.")
        alpha_tokens = []

    rows = []
    for a in alpha_tokens:
        if str(a.get("chainId")) != CHAIN_ID_BSC:
            continue
        contract = (a.get("contractAddress") or "").lower()
        if contract not in elig_by_contract:
            continue
        alpha_id = a.get("alphaId") or a.get("tokenId") or ""
        if not alpha_id:
            continue
        rows.append({
            "symbol": elig_by_contract[contract].get("symbol", ""),
            "bsc_contract": contract,
            "alpha_symbol": f"{alpha_id}USDT",
            "alpha_id": alpha_id,
            "base_asset": a.get("symbol", ""),
        })

    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Matched {len(rows)} eligible tokens to Binance Alpha symbols -> {OUT.relative_to(REPO)}")
    if not rows:
        print("(empty — volume provider will report volume UNAVAILABLE and fail safe)")


if __name__ == "__main__":
    main()
