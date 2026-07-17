"""Thin JSON-RPC client over PUBLIC BSC endpoints — free, no API quota (the reason
we dropped Moralis: 10 wallets x 30s polling exhausted its free daily quota mid-day,
leaving the bot blind; see the v2 spec). Rotates through fallback endpoints because
public nodes have no SLA, and chunks eth_getLogs ranges because public nodes cap
the block span per call.

DEFAULT_ENDPOINTS verified 2026-07-16 to support address-less, topic-only
eth_getLogs (what ChainEventSource needs — it watches wallets across ANY token,
so it can't scope queries to a single contract address). The obvious default
choices do NOT work for this: bsc-dataseed.binance.org/defibit.io reject every
topic-only eth_getLogs call with "limit exceeded" regardless of range size, and
rpc.ankr.com/bsc now requires a paid API key. bsc.publicnode.com also rejects
address-less queries outright. Re-verify before swapping in a new default."""
from __future__ import annotations

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# PancakeSwap V2 pair Swap(address,uint256,uint256,uint256,uint256,address)
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# Uniswap/Pancake V3 pool Swap(address,address,int256,int256,uint160,uint128,int24)
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

DEFAULT_ENDPOINTS = [
    "https://bsc-pokt.nodies.app",
    "https://1rpc.io/bnb",
]


class RpcError(Exception):
    pass


def addr_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


class RpcPool:
    def __init__(self, endpoints: list[str], timeout: int = 15) -> None:
        if not endpoints:
            raise ValueError("need at least one RPC endpoint")
        self._endpoints = list(endpoints)
        self._timeout = timeout

    def call(self, method: str, params: list) -> object:
        last_err: Exception | None = None
        null_result_seen = False
        for url in self._endpoints:
            try:
                r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params},
                                  timeout=self._timeout)
                r.raise_for_status()
                payload = r.json()
                if "error" in payload:
                    raise RpcError(f"{method} on {url}: {payload['error']}")
                result = payload.get("result")
                if result is None:
                    # Some public gateways (seen live on 1rpc.io/bnb) answer with a
                    # bare null "result" instead of a JSON-RPC error when they lack
                    # archive data for an old block/tx — try the next endpoint
                    # rather than trusting this as the real answer. If every
                    # endpoint agrees on null, that's returned below as the
                    # legitimate final answer (e.g. a receipt that truly
                    # doesn't exist yet).
                    null_result_seen = True
                    log.debug("rpc_endpoint_null_result", url=url, method=method)
                    continue
                return result
            except Exception as e:  # noqa: BLE001 — any endpoint failure → try next
                last_err = e
                log.debug("rpc_endpoint_failed", url=url, method=method,
                          error=type(e).__name__)
        if null_result_seen:
            return None
        raise RpcError(f"all RPC endpoints failed for {method}: {last_err}")

    def latest_block(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(self, flt: dict) -> list[dict]:
        return self.call("eth_getLogs", [flt])

    def get_logs_chunked(self, from_block: int, to_block: int, topics: list,
                         address: str | None = None, chunk: int = 2000) -> list[dict]:
        logs: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            flt: dict = {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}
            if address:
                flt["address"] = address
            logs.extend(self.get_logs(flt))
            start = end + 1
        return logs

    def get_receipt(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def get_code(self, address: str) -> str:
        return self.call("eth_getCode", [address, "latest"]) or "0x"
