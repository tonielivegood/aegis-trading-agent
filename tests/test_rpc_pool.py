"""RpcPool: endpoint fallback, chunked getLogs, topic helpers. No real network."""
import pytest

from src.agent.copy_trade.rpc_pool import (
    RpcError, RpcPool, TRANSFER_TOPIC, addr_topic,
)


def test_addr_topic_pads_and_lowercases():
    t = addr_topic("0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa")
    assert t == "0x000000000000000000000000a5200dc306d8273f9ccdbf5221a6cc3916ac2ffa"
    assert len(t) == 66


def test_transfer_topic_constant():
    assert TRANSFER_TOPIC == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def test_call_falls_over_to_next_endpoint(monkeypatch):
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if url == "http://bad":
            raise ConnectionError("down")
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": "0x10"})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    pool = RpcPool(["http://bad", "http://good"])
    assert pool.call("eth_blockNumber", []) == "0x10"
    assert calls == ["http://bad", "http://good"]


def test_call_raises_rpc_error_when_all_endpoints_fail(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise ConnectionError("down")
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    with pytest.raises(RpcError):
        RpcPool(["http://a", "http://b"]).call("eth_blockNumber", [])


def test_call_raises_rpc_error_on_error_payload(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return FakeResponse({"jsonrpc": "2.0", "id": 1,
                             "error": {"code": -32000, "message": "range too large"}})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    with pytest.raises(RpcError):
        RpcPool(["http://a"]).call("eth_getLogs", [{}])


def test_call_skips_null_result_and_tries_next_endpoint(monkeypatch):
    # Real-world case (1rpc.io/bnb, 2026-07-16): a gateway with no archive data
    # for an old block answers {"result": null} instead of a JSON-RPC error.
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if url == "http://no-archive":
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": None})
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"timestamp": "0x1"}})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    pool = RpcPool(["http://no-archive", "http://has-archive"])
    assert pool.call("eth_getBlockByNumber", ["0x1", False]) == {"timestamp": "0x1"}
    assert calls == ["http://no-archive", "http://has-archive"]


def test_call_returns_none_when_every_endpoint_gives_null_result(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": None})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    pool = RpcPool(["http://a", "http://b"])
    assert pool.call("eth_getTransactionReceipt", ["0xnonexistent"]) is None


def test_get_logs_routes_to_logs_endpoints_other_methods_to_general(monkeypatch):
    # Real incident, 2026-07-17: funneling receipts/blocks through the only two
    # getLogs-capable free endpoints rate-limited them within minutes of go-live.
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append((json["method"], url))
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": "0x1"})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    pool = RpcPool(["http://general"], logs_endpoints=["http://logs-only"])
    pool.call("eth_getTransactionReceipt", ["0xabc"])
    pool.call("eth_getLogs", [{}])
    assert calls == [("eth_getTransactionReceipt", "http://general"),
                     ("eth_getLogs", "http://logs-only")]


def test_logs_endpoints_default_to_general_when_not_given(monkeypatch):
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": []})
    monkeypatch.setattr("src.agent.copy_trade.rpc_pool.requests.post", fake_post)
    RpcPool(["http://only"]).call("eth_getLogs", [{}])
    assert calls == ["http://only"]


def test_latest_block_parses_hex(monkeypatch):
    monkeypatch.setattr(RpcPool, "call", lambda self, m, p: "0x2a")
    assert RpcPool(["x"]).latest_block() == 42


def test_get_logs_chunked_splits_ranges(monkeypatch):
    seen_ranges = []
    def fake_get_logs(self, flt):
        seen_ranges.append((int(flt["fromBlock"], 16), int(flt["toBlock"], 16)))
        return [{"blockNumber": flt["fromBlock"]}]
    monkeypatch.setattr(RpcPool, "get_logs", fake_get_logs)
    logs = RpcPool(["x"]).get_logs_chunked(100, 4599, topics=[TRANSFER_TOPIC], chunk=2000)
    assert seen_ranges == [(100, 2099), (2100, 4099), (4100, 4599)]
    assert len(logs) == 3
