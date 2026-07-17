import pytest

from scripts.build_bsc_smart_wallets import (
    assemble_candidates, block_at_timestamp, dexscreener_pair, gmgn_maker_counts,
)
from src.agent.copy_trade.rpc_pool import RpcError

W1, W2, W3 = "0x" + "1"*40, "0x" + "2"*40, "0x" + "3"*40
TOKEN = "0x" + "a" * 40


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload
    def raise_for_status(self):
        pass


def test_gmgn_maker_counts():
    trades = [{"maker": W1}, {"maker": W1}, {"maker": W2}, {"maker": None}]
    assert gmgn_maker_counts(trades) == {W1: 2, W2: 1}


def test_assemble_candidates_merges_sources():
    cands = assemble_candidates(gmgn_counts={W1: 4, W2: 1},
                                early_counts={W1: 3, W3: 2})
    by_addr = {c["address"]: c for c in cands}
    assert by_addr[W1]["sources"] == ["gmgn", "early_buyer"]
    assert by_addr[W1]["score"] > by_addr[W2]["score"]   # both-sources + wins beat gmgn-only
    assert by_addr[W3]["sources"] == ["early_buyer"]


def test_dexscreener_pair_skips_pair_missing_created_at(monkeypatch):
    # real-world case (LAB, 2026-07-16): the highest-liquidity BSC pair has no
    # pairCreatedAt, but a lower-liquidity one does — must fall back to it.
    payload = {"pairs": [
        {"chainId": "bsc", "pairAddress": "0xhigh", "pairCreatedAt": None,
         "liquidity": {"usd": 900}},
        {"chainId": "bsc", "pairAddress": "0xlow", "pairCreatedAt": 1700000000000,
         "liquidity": {"usd": 100}},
        {"chainId": "ethereum", "pairAddress": "0xeth", "pairCreatedAt": 1700000000000,
         "liquidity": {"usd": 999999}},
    ]}
    monkeypatch.setattr("scripts.build_bsc_smart_wallets.requests.get",
                        lambda url, timeout=None: FakeResp(payload))
    pair = dexscreener_pair(TOKEN)
    assert pair["pairAddress"] == "0xlow"


def test_dexscreener_pair_none_when_no_pair_has_created_at(monkeypatch):
    payload = {"pairs": [
        {"chainId": "bsc", "pairAddress": "0xhigh", "pairCreatedAt": None,
         "liquidity": {"usd": 900}},
    ]}
    monkeypatch.setattr("scripts.build_bsc_smart_wallets.requests.get",
                        lambda url, timeout=None: FakeResp(payload))
    assert dexscreener_pair(TOKEN) is None


class _FakePool:
    """Simulates a chain with an exact 3s block time, genesis (block 0) at ts=0.
    Records every block number queried so tests can assert the search never
    probes near genesis for a recent target timestamp."""
    def __init__(self, latest_block: int, missing_blocks: set[int] | None = None):
        self.latest_block_ = latest_block
        self.missing = missing_blocks or set()
        self.queried: list[int] = []

    def latest_block(self) -> int:
        return self.latest_block_

    def call(self, method, params):
        assert method == "eth_getBlockByNumber"
        block_num = int(params[0], 16)
        self.queried.append(block_num)
        if block_num in self.missing:
            return None
        return {"timestamp": hex(block_num * 3)}


def test_block_at_timestamp_finds_correct_block_without_probing_genesis():
    pool = _FakePool(latest_block=10_000_000)
    target_block = 9_800_000
    target_ts = target_block * 3
    found = block_at_timestamp(pool, target_ts)
    assert found == target_block
    assert min(pool.queried) > 9_000_000  # never wandered anywhere near genesis


def test_block_at_timestamp_raises_when_latest_block_has_no_data():
    pool = _FakePool(latest_block=10_000_000, missing_blocks={10_000_000})
    with pytest.raises(RpcError):
        block_at_timestamp(pool, 9_800_000 * 3)


def test_block_at_timestamp_raises_when_a_probed_block_has_no_data():
    pool = _FakePool(latest_block=10_000_000, missing_blocks={9_800_000})
    with pytest.raises(RpcError):
        block_at_timestamp(pool, 9_800_000 * 3)
