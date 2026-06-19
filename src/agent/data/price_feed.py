"""USD price feed for portfolio valuation.

Primary source is the on-chain PancakeSwap V2 quote (getAmountsOut) — this is the
ground-truth price the agent can actually execute at, and it avoids CMC's
symbol-collision bug (e.g. symbol "BTCB" matching a scam token on CMC). CMC is
used only as a fallback when a token has no on-chain route.

CMC quote data (% change, volume) is consumed separately by the signal layer,
where token identity is pinned by CMC id rather than symbol.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

from ..config import settings
from ..monitor.logger import get_logger
from . import cmc_client, token_list
from .rpc import get_web3

log = get_logger(__name__)

# Minimal PancakeSwap V2 Router ABI (just getAmountsOut).
ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _router():
    w3 = get_web3()
    return w3.eth.contract(address=Web3.to_checksum_address(settings.pancake_router), abi=ROUTER_ABI)


def onchain_price_usd(symbol: str) -> float | None:
    """Price of 1 token in USD via PancakeSwap, or None if no route.

    Native "BNB" is priced via its wrapped form WBNB.
    """
    if symbol.upper() == "BNB":
        symbol = "WBNB"
    tok = token_list.get_token(symbol)
    if tok.is_stable:
        return 1.0

    usdt = Web3.to_checksum_address(settings.usdt_address)
    wbnb = Web3.to_checksum_address(settings.wbnb_address)
    one = 10 ** tok.decimals

    paths = [[tok.address, usdt]]
    if tok.address.lower() != wbnb.lower():
        paths.append([tok.address, wbnb, usdt])

    router = _router()
    for path in paths:
        try:
            amounts = router.functions.getAmountsOut(one, path).call()
            out = amounts[-1] / 1e18  # USDT has 18 decimals on BSC
            if out > 0:
                return out
        except Exception:  # noqa: BLE001 — try next route
            continue
    return None


_PRICE_WORKERS = 8


def _safe_onchain_price(sym: str) -> tuple[str, float | None]:
    try:
        return sym, onchain_price_usd(sym)
    except Exception as e:  # noqa: BLE001
        log.warning("onchain_price_failed", symbol=sym, error=str(e))
        return sym, None


def get_prices(symbols: list[str]) -> dict[str, float]:
    """USD price per symbol. On-chain PancakeSwap first; CMC fallback for any missing.

    On-chain quotes are independent read-only RPC calls, so we fan them out across a
    small thread pool — sequential pricing of the whole Alpha universe was the tick's
    dominant latency (≈50s for ~40 tokens) and broke the 60s event cadence.
    """
    prices: dict[str, float] = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=min(_PRICE_WORKERS, len(symbols))) as ex:
            for sym, p in ex.map(_safe_onchain_price, symbols):
                if p is not None:
                    prices[sym] = p

    missing = [s for s in symbols if s not in prices]
    if missing:
        try:
            quotes = cmc_client.get_quotes(missing)
            for sym in missing:
                p = quotes.get(sym, {}).get("price")
                if p:
                    prices[sym] = float(p)
                    log.debug("price_from_cmc_fallback", symbol=sym, price=p)
        except Exception as e:  # noqa: BLE001
            log.warning("cmc_fallback_failed", error=str(e))
    return prices


def get_price(symbol: str) -> float | None:
    return get_prices([symbol]).get(symbol)
