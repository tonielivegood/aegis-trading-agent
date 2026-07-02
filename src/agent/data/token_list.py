"""Token universe.

Static layers (from disk, unchanged since the contest):
  - `curated_core.json`  — 20 on-chain-verified blue chips (used for stable/WBNB
    valuation infrastructure and as the majors set — NOT auto-expanded post-contest,
    see [[post-contest-product-pivot]]).
  - `eligible_tokens.json` / `is_eligible()` — the OFFICIAL contest allowlist (149
    BEP-20 tokens). VESTIGIAL post-contest: no live entry gate calls `is_eligible()`
    any more (the meme universe moved to Binance's live hot-token feed — see
    `register_discovered` below); only the disabled `track1_compliance` module and
    the dead `event_driven_alpha_momentum.decide_entries()` path still reference it.
  - `tradable_alpha.json` — the liquid subset of the (contest-era) allowlist that had
    a deep enough PancakeSwap route to trade. Still used for MAJOR classification and
    as the legacy meme-scan fallback when `binance_w3w_universe_enabled=False`.

Runtime layer (process-lifetime, not on disk):
  - `register_discovered` — a meme found live via Binance's hot-token feed, already
    server-side-filtered (wash-trading/mint/freeze) and quote-checked (isHoneyPot/
    taxRate) before registration. This is the ACTIVE post-contest meme universe.

`get_token` resolves across core ∪ tradable-alpha ∪ discovered, so pricing/execution
work for any contract the live pipeline actually decided to touch.
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

# Stablecoins (settlement / "safe" floor) — eligible but never momentum-traded.
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "FRAX", "USDD",
               "USDE", "USD1", "LISUSD", "FRXUSD", "USDF", "DUSD", "EURI", "XUSD", "STABLE"}

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
def _alpha_slippage() -> dict[str, float]:
    """symbol(upper) -> pre-measured aggregator slippage at our order size, from the
    offline 1inch universe build (slippage_12usd). Used as the runtime liquidity gate
    so we never re-quote Pancake V2 (which wrongly rejects the liquid majors)."""
    if not ALPHA_PATH.exists():
        return {}
    raw = json.loads(ALPHA_PATH.read_text(encoding="utf-8"))
    return {t["symbol"].upper(): float(t.get("slippage_12usd", t.get("slippage_5usd", 1.0))) for t in raw}


def tradable_slippage(symbol: str) -> float:
    """Pre-measured aggregator slippage for a tradable token (1.0 if unknown = block)."""
    return _alpha_slippage().get(symbol.upper(), 1.0)


@lru_cache(maxsize=1)
def _alpha_ids() -> dict[str, int]:
    """symbol(upper) -> CoinMarketCap id, for unambiguous CMC pricing of the universe."""
    if not ALPHA_PATH.exists():
        return {}
    raw = json.loads(ALPHA_PATH.read_text(encoding="utf-8"))
    return {t["symbol"].upper(): int(t["id"]) for t in raw if t.get("id")}


def cmc_id(symbol: str) -> int | None:
    return _alpha_ids().get(symbol.upper())


@lru_cache(maxsize=1)
def _classes() -> dict[str, str]:
    """symbol(upper) -> 'major' | 'meme', from the tradable universe file."""
    if not ALPHA_PATH.exists():
        return {}
    raw = json.loads(ALPHA_PATH.read_text(encoding="utf-8"))
    return {t["symbol"].upper(): t.get("token_class", "meme") for t in raw}


def token_class(symbol: str) -> str:
    """Trading class of a tradable token (default 'meme' = ride if unknown)."""
    key = symbol.upper()
    if key in _discovered_classes:
        return _discovered_classes[key]
    return _classes().get(key, "meme")


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


@lru_cache(maxsize=1)
def valuation_tokens() -> list[Token]:
    """Every token the wallet could plausibly HOLD, for full-wallet valuation:
    curated core ∪ tradable-alpha, deduped by contract address (core wins).

    Balance reading / equity MUST use this, never the trading subset. The agent
    deploys into the alpha universe, but the old core list was a strict subset of
    it — so any holding outside core (e.g. a leftover alpha position such as LUNC)
    was never read and surfaced as a phantom drawdown that latched the breaker.
    Valuing core ∪ alpha keeps equity == the real wallet the contest scores."""
    out: dict[str, Token] = {}
    for tok in list(_core().values()) + list(_alpha().values()):
        out.setdefault(tok.contract.lower(), tok)
    return list(out.values())


def held_valuation_tokens() -> list[Token]:
    """valuation_tokens() PLUS any runtime-discovered token (a hot-token meme buy,
    outside the static core/alpha files) — NOT cached, since `_discovered` grows
    during the process's life. Without this, a discovered token's balance/price is
    never read at all (real-money bug, 2/7): it silently drops out of equity and
    the exit rails, even though the wallet genuinely holds it."""
    return valuation_tokens() + list(_discovered.values())


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
    """Resolve a token across core ∪ tradable-alpha ∪ runtime-discovered
    (static registries win on overlap — see `register_discovered`)."""
    key = symbol.upper()
    core = _core()
    if key in core:
        return core[key]
    alpha = _alpha()
    if key in alpha:
        return alpha[key]
    if key in _discovered:
        return _discovered[key]
    raise KeyError(f"{symbol} not in tradable set (core, eligible-alpha, or discovered)")


def get_token_by_address(address: str) -> Token | None:
    """Resolve a token by its (case-insensitive) contract address, or None."""
    tok = _by_address().get(address.lower())
    if tok is not None:
        return tok
    for t in _discovered.values():
        if t.contract.lower() == address.lower():
            return t
    return None


# --- Runtime-discovered tokens (Binance hot-token universe, post-contest) ---
# Process-lifetime only (NOT persisted to disk): a token found live via the
# server-side-filtered hot-token feed, outside the static core/alpha files. Kept
# as a SEPARATE layer (never merged into the lru_cached static dicts above) so
# discovery can never shadow or corrupt the hand-verified registries.
_discovered: dict[str, Token] = {}
_discovered_classes: dict[str, str] = {}


def register_discovered(symbol: str, contract: str, decimals: int = 18) -> Token:
    """Register a token found live (e.g. via Binance hot-token) so the rest of the
    pipeline (pricing, execution, valuation) can resolve it via get_token() for the
    rest of this process's life. Always classed 'meme' — the major basket stays on
    the static, hand-verified curated_core.json (never auto-expanded). A symbol
    already present in the static registries is left untouched (static wins)."""
    key = symbol.upper()
    if key in _core() or key in _alpha():
        return get_token(symbol)
    tok = Token(symbol=symbol, contract=contract, decimals=decimals)
    _discovered[key] = tok
    _discovered_classes[key] = "meme"
    return tok


def is_discovered(symbol: str) -> bool:
    return symbol.upper() in _discovered


@lru_cache(maxsize=1)
def _alpha_addrs() -> set[str]:
    return {t.contract.lower() for t in _alpha().values()}


def is_tradable_alpha(address: str) -> bool:
    """True if the address is in the liquid, tradable Alpha subset — including a
    runtime-discovered token (already passed hot-token's server-side filters +
    the just-in-time honeypot/tax check before being registered)."""
    addr = address.lower()
    if addr in _alpha_addrs():
        return True
    return any(t.contract.lower() == addr for t in _discovered.values())


def stablecoins() -> list[Token]:
    return [t for t in _core().values() if t.is_stable]


def is_eligible(address: str) -> bool:
    """True if an address is within the contest-eligible universe."""
    return address.lower() in _eligible_addrs()
