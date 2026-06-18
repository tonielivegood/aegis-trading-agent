"""Token universe.

Three layers:
  - `curated_core.json`  — 20 on-chain-verified blue chips (used for stable/WBNB
    valuation infrastructure and as the original majors set).
  - `eligible_tokens.json` — the OFFICIAL contest allowlist (149 BEP-20 tokens).
    Eligibility is matched strictly by CONTRACT ADDRESS — there is no "majors are
    always eligible" assumption (that was the root bug that aimed the agent at a
    non-scoring universe).
  - `tradable_alpha.json` — the liquid subset of the allowlist that actually has
    a deep enough PancakeSwap route to trade (built by scripts/build_alpha_universe.py).
    This is the universe the contest strategy deploys into.

`get_token` resolves across core ∪ tradable-alpha, so pricing/execution work for
any eligible contract (no more KeyError outside the majors).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from web3 import Web3

DATA_DIR = Path(__file__).resolve().parent
CORE_PATH = DATA_DIR / "curated_core.json"
ELIGIBLE_PATH = DATA_DIR / "eligible_tokens.json"
ALPHA_PATH = DATA_DIR / "tradable_alpha.json"

# Stablecoins among the core — used by the risk layer for the "safe" floor.
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI"}

# Non-stable core tokens ordered by approximate BSC/Binance liquidity. Used to
# pick a concentrated basket when capital is small (so each order isn't dust).
LIQUIDITY_PRIORITY = [
    "BTCB", "ETH", "WBNB", "CAKE", "XRP", "ADA", "DOGE", "LINK",
    "DOT", "AVAX", "LTC", "UNI", "ATOM", "INJ", "FIL", "TWT",
]


class Token(BaseModel):
    symbol: str
    contract: str
    decimals: int = 18

    @property
    def address(self) -> str:
        return Web3.to_checksum_address(self.contract)

    @property
    def is_stable(self) -> bool:
        return self.symbol in STABLECOINS


@lru_cache(maxsize=1)
def _core() -> dict[str, Token]:
    raw = json.loads(CORE_PATH.read_text(encoding="utf-8"))
    return {t["symbol"]: Token(**t) for t in raw}


@lru_cache(maxsize=1)
def _alpha() -> dict[str, Token]:
    """The liquid, tradable subset of the official allowlist (built offline).
    Keyed by UPPER-cased symbol; non-ASCII symbols upper-case to themselves."""
    if not ALPHA_PATH.exists():
        return {}
    raw = json.loads(ALPHA_PATH.read_text(encoding="utf-8"))
    out: dict[str, Token] = {}
    for t in raw:
        tok = Token(symbol=t["symbol"], contract=t["contract"], decimals=t.get("decimals", 18))
        out[t["symbol"].upper()] = tok
    return out


@lru_cache(maxsize=1)
def _by_address() -> dict[str, Token]:
    out: dict[str, Token] = {}
    for tok in list(_core().values()) + list(_alpha().values()):
        out[tok.contract.lower()] = tok
    return out


@lru_cache(maxsize=1)
def _eligible_addrs() -> set[str]:
    """The OFFICIAL allowlist addresses — strictly, with no majors shortcut."""
    if not ELIGIBLE_PATH.exists():
        return set()
    raw = json.loads(ELIGIBLE_PATH.read_text(encoding="utf-8"))
    return {t["contract"].lower() for t in raw if t.get("contract")}


def tradable_tokens() -> list[Token]:
    """The set the agent is allowed to trade (curated, verified, liquid)."""
    return list(_core().values())


def tradable_symbols() -> list[str]:
    return list(_core().keys())


def basket_symbols(n: int) -> list[str]:
    """Top-`n` most liquid non-stable core tokens — the basket to deploy into.
    Concentrating into the most liquid majors keeps per-order size meaningful
    (above the min-order/fee threshold) when capital is small."""
    core = _core()
    ordered = [s for s in LIQUIDITY_PRIORITY if s in core]
    return ordered[:n]


def alpha_symbols() -> list[str]:
    """Liquid, tradable eligible tokens — the contest deploy universe."""
    return list(_alpha().keys())


def tradable_alpha_tokens() -> list[Token]:
    return list(_alpha().values())


def get_token(symbol: str) -> Token:
    """Resolve a token across core ∪ tradable-alpha (core wins on overlap)."""
    key = symbol.upper()
    core = _core()
    if key in core:
        return core[key]
    alpha = _alpha()
    if key in alpha:
        return alpha[key]
    raise KeyError(f"{symbol} not in tradable set (core or eligible-alpha)")


def get_token_by_address(address: str) -> Token | None:
    """Resolve a token by its (case-insensitive) contract address, or None."""
    return _by_address().get(address.lower())


def stablecoins() -> list[Token]:
    return [t for t in _core().values() if t.is_stable]


def is_eligible(address: str) -> bool:
    """True if an address is within the contest-eligible universe."""
    return address.lower() in _eligible_addrs()
