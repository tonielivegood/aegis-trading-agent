"""Verify the private key in .env matches AGENT_WALLET_ADDRESS.
Does NOT print the private key. Only compares derived vs declared address.
"""
import sys
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def main() -> int:
    from eth_account import Account

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        print("ERROR: .env not found")
        return 1

    env = load_env(env_path)
    pk = env.get("AGENT_PRIVATE_KEY", "")
    declared = env.get("AGENT_WALLET_ADDRESS", "")

    if not pk or pk.startswith("PASTE_"):
        print("ERROR: AGENT_PRIVATE_KEY not filled in")
        return 1
    if not declared or declared.startswith("PASTE_"):
        print("ERROR: AGENT_WALLET_ADDRESS not filled in")
        return 1

    if not pk.startswith("0x"):
        pk = "0x" + pk

    try:
        acct = Account.from_key(pk)
    except Exception as e:
        print(f"ERROR: invalid private key ({type(e).__name__})")
        return 1

    derived = acct.address
    # Checksum-insensitive compare
    match = derived.lower() == declared.lower()

    print(f"Declared address : {declared}")
    print(f"Derived address  : {derived}")
    print(f"Checksummed form : {derived}")
    print(f"MATCH            : {'YES ✓' if match else 'NO ✗ — key does NOT match address!'}")
    return 0 if match else 2


if __name__ == "__main__":
    sys.exit(main())
