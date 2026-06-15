"""Resilient BSC RPC client with multi-endpoint failover.

Rotates across several free public BSC endpoints so a single laggy/down node
doesn't stall the agent during the live window. Returns a connected web3.Web3.
"""
from __future__ import annotations

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

# Free public BSC mainnet endpoints. The configured one is tried first.
FALLBACK_ENDPOINTS = [
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed1.defibit.io/",
    "https://bsc-dataseed1.ninicoin.io/",
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",
    "https://rpc.ankr.com/bsc",
]


def _candidates() -> list[str]:
    seen: list[str] = []
    for url in [settings.bsc_rpc_url, *FALLBACK_ENDPOINTS]:
        if url and url not in seen:
            seen.append(url)
    return seen


def connect(timeout: int = 12) -> Web3:
    """Return the first healthy Web3 connection, or raise if all fail."""
    last_err: Exception | None = None
    for url in _candidates():
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
            # BSC is PoA — inject middleware so block parsing works.
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected() and w3.eth.chain_id == settings.bsc_chain_id:
                log.debug("rpc_connected", endpoint=url)
                return w3
        except Exception as e:  # noqa: BLE001 — try next endpoint
            last_err = e
            log.warning("rpc_endpoint_failed", endpoint=url, error=str(e))
            continue
    raise ConnectionError(f"All BSC RPC endpoints failed. Last error: {last_err}")


_w3: Web3 | None = None


def get_web3() -> Web3:
    """Cached singleton connection (reconnects automatically if it drops)."""
    global _w3
    if _w3 is None or not _w3.is_connected():
        _w3 = connect()
    return _w3
