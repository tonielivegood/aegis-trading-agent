"""Fetch the eligible BEP-20 token universe from CoinMarketCap.

The hackathon defines eligibility as "BEP-20 tokens listed on CMC (149 tokens)".
We reconstruct that universe by pulling CMC listings and filtering to tokens whose
platform is BNB Smart Chain, ranked by market cap. Output saved as JSON for the
data layer.

NOTE: The EXACT official 149-list should still be cross-checked against DoraHacks.
This produces the high-confidence top BSC tokens, which covers the tradable core.
"""
import json
import urllib.request
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def cmc_get(path: str, params: dict, api_key: str) -> dict:
    url = "https://pro-api.coinmarketcap.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def main() -> None:
    env = load_env()
    key = env["CMC_API_KEY"]

    # Pull top 1000 by market cap; each entry includes its platform.
    data = cmc_get(
        "/v1/cryptocurrency/listings/latest",
        {"start": "1", "limit": "5000", "convert": "USD", "sort": "market_cap"},
        key,
    )

    bsc_tokens = []
    for c in data["data"]:
        plat = c.get("platform")
        if not plat:
            continue
        # BNB Smart Chain platform — CMC uses id 1839 (BNB) / name "BNB Smart Chain (BEP20)"
        name = (plat.get("name") or "")
        if "BNB" in name or "Smart Chain" in name or plat.get("symbol") == "BNB":
            q = c["quote"]["USD"]
            bsc_tokens.append({
                "id": c["id"],
                "symbol": c["symbol"],
                "name": c["name"],
                "contract": plat.get("token_address"),
                "market_cap": q.get("market_cap"),
                "volume_24h": q.get("volume_24h"),
                "price": q.get("price"),
            })

    bsc_tokens.sort(key=lambda t: (t["market_cap"] or 0), reverse=True)
    top = bsc_tokens[:149]

    out_dir = REPO / "src" / "agent" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "eligible_tokens.json"
    out_path.write_text(json.dumps(top, indent=2), encoding="utf-8")

    print(f"Total BSC tokens found in CMC listings: {len(bsc_tokens)}")
    print(f"Saved top {len(top)} to {out_path.relative_to(REPO)}")
    print("\nTop 20 by market cap:")
    for i, t in enumerate(top[:20], 1):
        mc = (t["market_cap"] or 0) / 1e9
        print(f"  {i:2d}. {t['symbol']:10s} {t['name'][:28]:28s} ${mc:7.2f}B  {t['contract']}")


if __name__ == "__main__":
    main()
