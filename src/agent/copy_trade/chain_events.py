"""Signal source v2: watch the tracked wallets' ERC-20 Transfer events straight from
public BSC RPC (replaces Moralis polling — free, no quota, lower latency).

Direction semantics per the v2 spec:
  "in"  = wallet RECEIVED a token AND the same tx contains a DEX Swap event
          (drops airdrops/plain transfers — spam tokens shower smart wallets daily);
  "out" = token LEFT the wallet, by any means (swap, multi-hop, plain transfer,
          CEX deposit) — for exit purposes a wallet abandoning the token is the
          signal, however it leaves. This is the root fix for the v1 parser
          missing multi-hop sells."""
from __future__ import annotations

from dataclasses import dataclass

from ..monitor.logger import get_logger
from .rpc_pool import RpcPool, TRANSFER_TOPIC, V2_SWAP_TOPIC, V3_SWAP_TOPIC, addr_topic

log = get_logger(__name__)


@dataclass(frozen=True)
class WalletEvent:
    wallet: str          # lowercase tracked wallet
    token_address: str   # lowercase ERC-20 contract
    direction: str       # "in" | "out"
    amount_raw: int
    tx_hash: str
    block: int


def _topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


class ChainEventSource:
    def __init__(self, pool: RpcPool, wallets: list[str], start_block: int,
                 ignore_tokens: set[str] | None = None) -> None:
        self._pool = pool
        self._wallet_topics = [addr_topic(w) for w in wallets]
        self._wallets = {w.lower() for w in wallets}
        self._ignore = {t.lower() for t in (ignore_tokens or set())}
        # Backlog-replay guard: never look before process start (the 01:45 16/7
        # phantom-position incident was a fresh state.json replaying history).
        self.last_processed = start_block
        self._receipt_swap_cache: dict[str, bool] = {}

    def poll(self) -> list[WalletEvent]:
        latest = self._pool.latest_block()
        if latest <= self.last_processed:
            return []
        frm, to = self.last_processed + 1, latest
        events: list[WalletEvent] = []
        # two filtered queries: transfers TO any tracked wallet, then FROM
        for position, direction in ((2, "in"), (1, "out")):
            topics: list = [TRANSFER_TOPIC, None, None]
            topics[position] = self._wallet_topics
            # chunk=40: free public endpoints cap eth_getLogs ranges hard
            # (1rpc.io/bnb at 50 blocks, nodies.app at 250 — confirmed live
            # 2026-07-17). Without this, any poll gap over the cap (a slow
            # scan, a brief outage, a burst of confirmed events needing extra
            # receipt lookups) raises here BEFORE last_processed advances —
            # and since it never advances on failure, the gap only grows on
            # every subsequent tick, permanently blinding the bot with no
            # self-recovery. Small chunking makes get_logs_chunked's own
            # splitting loop absorb any gap size instead.
            for lg in self._pool.get_logs_chunked(frm, to, topics=topics, chunk=40):
                ev = self._to_event(lg, direction)
                if ev is not None:
                    events.append(ev)
        self.last_processed = latest
        events.sort(key=lambda e: e.block)
        return events

    def _to_event(self, lg: dict, direction: str) -> WalletEvent | None:
        topics = lg.get("topics", [])
        if len(topics) < 3:
            return None
        address, block_number = lg.get("address"), lg.get("blockNumber")
        if address is None or block_number is None:
            return None   # malformed log from a flaky public RPC — skip it
        token = address.lower()
        if token in self._ignore:
            return None
        wallet = _topic_addr(topics[2] if direction == "in" else topics[1])
        if wallet not in self._wallets:
            return None
        tx_hash = lg["transactionHash"]
        if direction == "in" and not self._tx_has_swap(tx_hash):
            return None   # airdrop / plain transfer — not a buy
        return WalletEvent(wallet=wallet, token_address=token, direction=direction,
                           amount_raw=int(lg.get("data", "0x0"), 16),
                           tx_hash=tx_hash, block=int(block_number, 16))

    def _tx_has_swap(self, tx_hash: str) -> bool:
        if tx_hash in self._receipt_swap_cache:
            return self._receipt_swap_cache[tx_hash]
        receipt = self._pool.get_receipt(tx_hash) or {}
        has = any(l.get("topics") and l["topics"][0] in (V2_SWAP_TOPIC, V3_SWAP_TOPIC)
                  for l in receipt.get("logs", []))
        self._receipt_swap_cache[tx_hash] = has
        if len(self._receipt_swap_cache) > 2000:   # ponytail: crude cap, fine for 50 wallets
            self._receipt_swap_cache.clear()
        return has
