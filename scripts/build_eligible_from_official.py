"""Rebuild eligible_tokens.json from the OFFICIAL DoraHacks 149-symbol list.

The old fetch_token_list.py took "top-149 BSC by market cap" — a GUESS that only
overlapped the official contest list by ~48. This script takes the official symbols
(transcribed from the DoraHacks page, 19/6/2026) and resolves each to its CMC id +
BSC (BEP-20) contract address via the CMC API, then writes eligible_tokens.json in
the same schema the data layer expects.

Eligibility is matched by CONTRACT ADDRESS, so a wrong contract = trades that don't
count. Symbols are ambiguous on CMC (many tokens share "M", "U", "H", "B", "Q",
"0G"...). This script picks the BSC-platform candidate with the highest market cap
per symbol and PRINTS every ambiguous / unresolved case so a human can verify the
handful that matter against the organizer's canonical list before go-live.
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Official 149 (transcribed from the DoraHacks "Eligible tokens" block, 19/6/2026).
OFFICIAL = [
    "ETH", "USDT", "USDC", "XRP", "TRX", "DOGE", "ZEC", "ADA", "LINK", "BCH", "DAI",
    "TON", "USD1", "USDe", "M", "LTC", "AVAX", "SHIB", "XAUt", "WLFI", "H", "DOT",
    "UNI", "ASTER", "DEXE", "USDD", "ETC", "AAVE", "ATOM", "U", "STABLE", "FIL",
    "INJ", "币安人生", "NIGHT", "FET", "TUSD", "BONK", "PENGU", "CAKE", "SIREN", "LUNC",
    "ZRO", "KITE", "FDUSD", "BEAT", "PIEVERSE", "BTT", "NFT", "EDGE", "FLOKI", "LDO",
    "B", "FF", "PENDLE", "NEX", "STG", "AXS", "TWT", "HOME", "RAY", "COMP", "GWEI",
    "XCN", "GENIUS", "XPL", "BAT", "SKYAI", "APE", "IP", "SFP", "TAG", "NXPC", "AB",
    "SAHARA", "1INCH", "CHEEMS", "BANANAS31", "RIVER", "MYX", "RAVE", "SNX", "FORM",
    "LAB", "HTX", "USDf", "CTM", "BDX", "SLX", "UB", "DUCKY", "FRAX", "BILL", "WFI",
    "KOGE", "ALE", "FRXUSD", "USDF", "GOMINING", "VCNT", "GUA", "DUSD", "SMILEK", "0G",
    "BEAM", "MY", "SOON", "REAL", "Q", "AIOZ", "ZIG", "YFI", "TAC", "lisUSD", "CYS",
    "ZAMA", "TRIA", "HUMA", "PLUME", "ZIL", "XPR", "ZETA", "BabyDoge", "NILA", "ROSE",
    "VELO", "UAI", "BRETT", "OPEN", "BSB", "TOSHI", "BAS", "ACH", "AXL", "LUR", "ELF",
    "KAVA", "APR", "IRYS", "EURI", "XUSD", "BARD", "DUSK", "SUSHI", "PEAQ", "COAI",
    "BDCA", "XAUM",
]


def load_key() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("CMC_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("CMC_API_KEY not in .env")


def cmc(path: str, params: dict, key: str) -> dict:
    url = "https://pro-api.coinmarketcap.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def is_bsc(platform: dict | None) -> bool:
    if not platform:
        return False
    name = (platform.get("name") or "")
    return "BNB" in name or "Smart Chain" in name or platform.get("symbol") == "BNB"


def main() -> None:
    key = load_key()
    # /v2/info accepts a comma list of symbols; ambiguous symbols return a LIST of coins.
    resolved, ambiguous, unresolved = [], [], []
    batch = 40
    for i in range(0, len(OFFICIAL), batch):
        syms = OFFICIAL[i:i + batch]
        try:
            data = cmc("/v2/cryptocurrency/info", {"symbol": ",".join(syms), "aux": "platform"}, key)["data"]
        except Exception as e:  # noqa: BLE001
            print(f"  info batch failed for {syms[:3]}...: {e}")
            unresolved.extend(syms)
            continue
        for sym in syms:
            coins = data.get(sym) or data.get(sym.upper()) or []
            if isinstance(coins, dict):
                coins = [coins]
            bsc = [c for c in coins if is_bsc(c.get("platform"))]
            if not bsc:
                unresolved.append(sym)
                continue
            pick = bsc[0]  # CMC returns most-relevant first
            if len(bsc) > 1:
                ambiguous.append((sym, [(c["id"], c.get("platform", {}).get("token_address")) for c in bsc]))
            resolved.append({
                "id": pick["id"], "symbol": sym, "name": pick.get("name", sym),
                "contract": pick.get("platform", {}).get("token_address"),
            })

    print(f"resolved={len(resolved)}  ambiguous={len(ambiguous)}  unresolved={len(unresolved)}")
    if ambiguous:
        print("\nAMBIGUOUS (verify against organizer's canonical list):")
        for sym, cands in ambiguous:
            print(f"  {sym}: {cands}")
    if unresolved:
        print(f"\nUNRESOLVED (no BSC contract found — needs manual contract): {unresolved}")

    out = REPO / "src" / "agent" / "data" / "eligible_tokens_official.json"
    out.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(resolved)} entries to {out.relative_to(REPO)} (NOT yet swapped in — review first)")


if __name__ == "__main__":
    main()
