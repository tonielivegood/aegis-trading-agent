"""On-chain registration for BNB Hack 2026 Track 1.

Calls register() on the hackathon contract from the agent wallet.
The contract records msg.sender as the participant (immutable).

Usage:
    python -m src.agent.registration.register_agent --check     # read-only status
    python -m src.agent.registration.register_agent --register  # send tx (irreversible)

Security:
    - Private key is read from AGENT_PRIVATE_KEY env var only, never logged.
    - Transaction is signed locally; key never leaves this process.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ABI_PATH = Path(__file__).resolve().parent / "contract_abi.json"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    # Real env vars take precedence
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith(("AGENT_", "BSC_", "HACKATHON_"))})
    return env


def rpc_call(rpc: str, method: str, params: list) -> dict:
    req = urllib.request.Request(
        rpc,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(f"RPC error: {out['error']}")
    return out["result"]


def selector(sig: str) -> str:
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode()).hex()[:8]


def fmt_ts(ts: int) -> str:
    if ts == 0:
        return "0 (unset)"
    return f"{ts} ({datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d %H:%M UTC})"


def get_status(env: dict[str, str]) -> dict:
    rpc, contract, wallet = env["BSC_RPC_URL"], env["HACKATHON_CONTRACT"], env["AGENT_WALLET_ADDRESS"]
    start = int(rpc_call(rpc, "eth_call", [{"to": contract, "data": selector("registrationStart()")}, "latest"]), 16)
    deadline = int(rpc_call(rpc, "eth_call", [{"to": contract, "data": selector("registrationDeadline()")}, "latest"]), 16)
    addr_arg = wallet.lower().replace("0x", "").rjust(64, "0")
    is_reg = int(rpc_call(rpc, "eth_call", [{"to": contract, "data": selector("isRegistered(address)") + addr_arg}, "latest"]), 16) == 1
    balance = int(rpc_call(rpc, "eth_getBalance", [wallet, "latest"]), 16)
    now = int(datetime.now(tz=timezone.utc).timestamp())
    return {
        "start": start, "deadline": deadline, "now": now,
        "is_registered": is_reg, "balance_wei": balance,
        "window_open": start <= now <= deadline,
    }


def print_status(env: dict[str, str], st: dict) -> None:
    print("=" * 60)
    print("REGISTRATION STATUS")
    print("=" * 60)
    print(f"Contract            : {env['HACKATHON_CONTRACT']}")
    print(f"Agent wallet        : {env['AGENT_WALLET_ADDRESS']}")
    print(f"registrationStart   : {fmt_ts(st['start'])}")
    print(f"registrationDeadline: {fmt_ts(st['deadline'])}")
    print(f"Now (UTC)           : {fmt_ts(st['now'])}")
    print(f"Window open now?    : {st['window_open']}")
    print(f"Already registered? : {st['is_registered']}")
    print(f"Wallet BNB balance  : {st['balance_wei'] / 1e18:.6f} BNB")
    print("=" * 60)


def register(env: dict[str, str], st: dict) -> int:
    from eth_account import Account

    if st["is_registered"]:
        print("Already registered — nothing to do.")
        return 0
    if not st["window_open"]:
        print("ERROR: registration window is not open. Aborting.")
        return 1
    if st["balance_wei"] == 0:
        print("ERROR: wallet has 0 BNB — cannot pay gas. Fund the wallet first.")
        return 1

    pk = env["AGENT_PRIVATE_KEY"]
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = Account.from_key(pk)
    if acct.address.lower() != env["AGENT_WALLET_ADDRESS"].lower():
        print("ERROR: private key does not match AGENT_WALLET_ADDRESS. Aborting.")
        return 1

    from eth_utils import to_checksum_address

    rpc, contract = env["BSC_RPC_URL"], to_checksum_address(env["HACKATHON_CONTRACT"])
    chain_id = int(env.get("BSC_CHAIN_ID", "56"))
    nonce = int(rpc_call(rpc, "eth_getTransactionCount", [acct.address, "latest"]), 16)
    gas_price = int(rpc_call(rpc, "eth_gasPrice", []), 16)

    tx = {
        "to": contract,
        "data": selector("register()"),
        "nonce": nonce,
        "gas": 120_000,
        "gasPrice": int(gas_price * 1.2),  # 20% buffer
        "chainId": chain_id,
        "value": 0,
    }
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    tx_hash = rpc_call(rpc, "eth_sendRawTransaction", [raw])
    print(f"Transaction sent: {tx_hash}")
    print(f"Track it: https://bscscan.com/tx/{tx_hash}")
    print("Waiting for confirmation (re-run with --check to verify isRegistered=True).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="read-only status")
    p.add_argument("--register", action="store_true", help="send register() tx (irreversible)")
    args = p.parse_args()

    env = load_env()
    required = ["BSC_RPC_URL", "HACKATHON_CONTRACT", "AGENT_WALLET_ADDRESS"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {missing}")
        return 1

    st = get_status(env)
    print_status(env, st)

    if args.register:
        return register(env, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
