"""Verify canonical BSC blue-chip token contracts on-chain.

Calls symbol() and decimals() on each candidate address. Only addresses whose
on-chain symbol matches the expected symbol are written to curated_core.json.
This protects against using a wrong/scam contract.
"""
import json
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Canonical BSC mainnet addresses for deep-liquidity, definitely-eligible tokens.
CANDIDATES = {
    "WBNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
    "BTCB": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
    "ETH":  "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    "USDT": "0x55d398326f99059fF775485246999027B3197955",
    "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "BUSD": "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
    "DAI":  "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
    "XRP":  "0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE",
    "ADA":  "0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47",
    "DOGE": "0xbA2aE424d960c26247Dd6c32edC70B295c744C43",
    "DOT":  "0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402",
    "LTC":  "0x4338665CBB7B2485A8855A139b75D5e34AB0DB94",
    "LINK": "0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD",
    "UNI":  "0xBf5140A22578168FD562DCcF235E5D43A02ce9B1",
    "AVAX": "0x1CE0c2827e2eF14D5C4f29a091d735A204794041",
    "TWT":  "0x4B0F1812e5Df2A09796481Ff14017e6005508003",
    "ATOM": "0x0Eb3a705fc54725037CC9e008bDede697f62F335",
    "INJ":  "0xa2B726B1145A4773F68593CF171187d8EBe4d495",
    "FIL":  "0x0D8Ce2A99Bb6e3B7Db580eD848240e4a0F9aE153",
}

# function selectors
SYM_SEL = "0x95d89b41"       # symbol()
DEC_SEL = "0x313ce567"       # decimals()


def load_env():
    env = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def eth_call(rpc, to, data):
    req = urllib.request.Request(
        rpc,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                         "params": [{"to": to, "data": data}, "latest"]}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read()).get("result")


def decode_string(hexdata):
    if not hexdata or hexdata == "0x":
        return ""
    raw = bytes.fromhex(hexdata[2:])
    # ABI dynamic string: offset(32) + length(32) + data
    if len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        return raw[64:64 + length].decode("utf-8", errors="ignore")
    # some tokens (old) return bytes32
    return raw.rstrip(b"\x00").decode("utf-8", errors="ignore")


def main():
    env = load_env()
    rpc = env["BSC_RPC_URL"]
    verified = []
    print(f"{'SYM':<7}{'ONCHAIN':<10}{'DEC':<5}{'STATUS':<8}{'ADDRESS'}")
    for sym, addr in CANDIDATES.items():
        try:
            onchain_sym = decode_string(eth_call(rpc, addr, SYM_SEL))
            dec = int(eth_call(rpc, addr, DEC_SEL), 16)
            ok = onchain_sym.upper() == sym.upper()
            status = "OK" if ok else "MISMATCH"
            print(f"{sym:<7}{onchain_sym[:9]:<10}{dec:<5}{status:<8}{addr}")
            if ok:
                verified.append({"symbol": sym, "contract": addr, "decimals": dec})
        except Exception as e:
            print(f"{sym:<7}{'ERROR':<10}{'-':<5}{'ERR':<8}{addr}  ({type(e).__name__})")

    out = REPO / "src" / "agent" / "data" / "curated_core.json"
    out.write_text(json.dumps(verified, indent=2), encoding="utf-8")
    print(f"\nVerified {len(verified)}/{len(CANDIDATES)} core tokens -> {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
