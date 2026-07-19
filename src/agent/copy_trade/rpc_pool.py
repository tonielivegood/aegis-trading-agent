"""Thin JSON-RPC client over PUBLIC BSC endpoints — free, no API quota (the reason
we dropped Moralis: 10 wallets x 30s polling exhausted its free daily quota mid-day,
leaving the bot blind; see the v2 spec). Rotates through fallback endpoints because
public nodes have no SLA, and chunks eth_getLogs ranges because public nodes cap
the block span per call.

Two endpoint tiers (split 2026-07-17 after live 429s/timeouts): only
eth_getLogs is restricted-per-provider — bsc-dataseed.binance.org/defibit.io
reject topic-only getLogs outright ("limit exceeded" at any range),
rpc.ankr.com/bsc needs a paid key, bsc.publicnode.com requires an address
filter — but every OTHER method (receipts, blocks, eth_call, nonce) works fine
on the high-capacity dataseed nodes. Funneling ALL calls through the two
getLogs-capable free endpoints rate-limited them within minutes of going live.
So: DEFAULT_LOGS_ENDPOINTS carries only eth_getLogs; DEFAULT_ENDPOINTS
(dataseed first) carries everything else. Re-verify per-provider getLogs
support before swapping in new defaults.

2026-07-19: both nodies and 1rpc went intermittently rate-limited/timing out
simultaneously (nodies read-timeouts, 1rpc hard "usage limit for your current
plan"), stalling the poll loop for hours. Re-probed ~20 free BSC RPC candidates
against the bot's REAL query shape (topics=[TRANSFER_TOPIC, None, <50 wallet
topics>], no `address` field, from both this dev machine and the VPS itself) —
most reject topic-only/address-less getLogs outright or cap at <=10 blocks.
Found two new working providers: 56.rpc.thirdweb.com (accepts up to 1000
blocks/call — 4x nodies' cap, 20x 1rpc's — survived a 10-call rapid burst with
zero errors, verified from the VPS) and bsc-mainnet.gateway.tatum.io (100
blocks/call, better than 1rpc, worse than nodies). Both added as earlier-tried
fallbacks; thirdweb goes first since it's clearly the strongest. Kept nodies/
1rpc in the rotation since their outages have been intermittent, not
permanent — more independent providers lowers the odds of a simultaneous
all-fail again."""
from __future__ import annotations

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# PancakeSwap V2 pair Swap(address,uint256,uint256,uint256,uint256,address)
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# Uniswap/Pancake V3 pool Swap(address,address,int256,int256,uint160,uint128,int24)
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# General-purpose calls (receipts, blocks, eth_call, nonce…) — dataseed nodes
# are fast and don't rate-limit these; the getLogs-capable providers sit last
# as emergency fallback.
DEFAULT_ENDPOINTS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://56.rpc.thirdweb.com",
    "https://bsc-pokt.nodies.app",
    "https://1rpc.io/bnb",
]
# eth_getLogs only — free providers verified (2026-07-19, from the VPS itself)
# to accept address-less, topic-only queries with the bot's real 50-wallet
# filter. Ordered strongest-cap-first: thirdweb (1000 blocks) > nodies (250) >
# tatum (100) > 1rpc (50).
DEFAULT_LOGS_ENDPOINTS = [
    "https://56.rpc.thirdweb.com",
    "https://bsc-pokt.nodies.app",
    "https://bsc-mainnet.gateway.tatum.io",
    "https://1rpc.io/bnb",
]


class RpcError(Exception):
    pass


def addr_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


class RpcPool:
    def __init__(self, endpoints: list[str], timeout: int = 15,
                 logs_endpoints: list[str] | None = None) -> None:
        if not endpoints:
            raise ValueError("need at least one RPC endpoint")
        self._endpoints = list(endpoints)
        self._logs_endpoints = list(logs_endpoints) if logs_endpoints else self._endpoints
        self._timeout = timeout
        self._logs_rotation = 0

    def call(self, method: str, params: list) -> object:
        last_err: Exception | None = None
        null_result_seen = False
        if method == "eth_getLogs":
            # Round-robin the starting endpoint so the getLogs load spreads
            # evenly instead of hammering the first provider until it
            # rate-limits (which is how 1rpc.io's daily quota got burned within
            # the first live hour, 2026-07-17). Failover order still covers
            # every endpoint on error.
            i = self._logs_rotation % len(self._logs_endpoints)
            self._logs_rotation += 1
            endpoints = self._logs_endpoints[i:] + self._logs_endpoints[:i]
        else:
            endpoints = self._endpoints
        for url in endpoints:
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
