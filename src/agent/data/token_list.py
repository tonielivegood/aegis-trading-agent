"""Token universe: the verified curated core we actually trade.

Loads `curated_core.json` (20 on-chain-verified blue chips) as the tradable set,
and `eligible_tokens.json` (CMC-derived 149) as a reference eligibility check.
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
def _eligible_addrs() -> set[str]:
    if not ELIGIBLE_PATH.exists():
        return set()
    raw = json.loads(ELIGIBLE_PATH.read_text(encoding="utf-8"))
    addrs = {t["contract"].lower() for t in raw if t.get("contract")}
    # Core tokens are always considered eligible.
    addrs |= {t.contract.lower() for t in _core().values()}
    return addrs


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


def get_token(symbol: str) -> Token:
    try:
        return _core()[symbol.upper()]
    except KeyError as e:
        raise KeyError(f"{symbol} not in curated tradable set") from e


def stablecoins() -> list[Token]:
    return [t for t in _core().values() if t.is_stable]


def is_eligible(address: str) -> bool:
    """True if an address is within the contest-eligible universe."""
    return address.lower() in _eligible_addrs()
