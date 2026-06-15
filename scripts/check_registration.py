"""Read live state from the hackathon registration contract via raw eth_call.
Shows registration window (start/deadline) and whether the agent wallet is registered.
Uses only stdlib + eth_hash (from eth-account) — no web3 needed.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def selector(sig: str) -> str:
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode()).hex()[:8]


def eth_call(rpc: str, to: str, data: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    req = urllib.request.Request(
        rpc,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def fmt_ts(ts: int) -> str:
    if ts == 0:
        return "0 (unset)"
    return f"{ts}  ({datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    rpc = env["BSC_RPC_URL"]
    contract = env["HACKATHON_CONTRACT"]
    wallet = env["AGENT_WALLET_ADDRESS"]

    start = int(eth_call(rpc, contract, selector("registrationStart()")), 16)
    deadline = int(eth_call(rpc, contract, selector("registrationDeadline()")), 16)

    # isRegistered(address): selector + 32-byte left-padded address
    addr_arg = wallet.lower().replace("0x", "").rjust(64, "0")
    is_reg_raw = eth_call(rpc, contract, selector("isRegistered(address)") + addr_arg)
    is_registered = int(is_reg_raw, 16) == 1

    now = int(datetime.now(tz=timezone.utc).timestamp())

    print("=" * 60)
    print("HACKATHON REGISTRATION CONTRACT — LIVE STATE")
    print("=" * 60)
    print(f"Contract           : {contract}")
    print(f"Agent wallet       : {wallet}")
    print(f"registrationStart  : {fmt_ts(start)}")
    print(f"registrationDeadline: {fmt_ts(deadline)}")
    print(f"Now (UTC)          : {fmt_ts(now)}")
    print("-" * 60)
    window_open = start <= now <= deadline if (start and deadline) else None
    print(f"Registration window OPEN now? : {window_open}")
    print(f"Agent wallet REGISTERED?      : {is_registered}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
