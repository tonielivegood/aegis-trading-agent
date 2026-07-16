# BSC Smart-Money Cluster + Shadow-Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v2 copy-trade pipeline from `docs/superpowers/specs/2026-07-16-cluster-signal-filter-design.md`: a self-built 50-wallet BSC smart-money list (Part 1) feeding a cluster-gated, shadow-mode-first trading monitor that reads signals from public BSC RPC instead of Moralis (Part 2).

**Architecture:** Part 1 is a manually-run local script that seeds candidates from GMGN + an early-buyer scan over Transfer logs, scores them, and writes `data/copy_trade/wallets.json`. Part 2 replaces the Moralis polling core of `src/agent/copy_trade/monitor.py` with an `eth_getLogs`-based event source, a RAM-only cluster tracker (3 buys / 15 min), a trade engine that fills either paper (shadow) or real positions, exit by 2-of-cluster outflow or a -70% price valve, and a report script. Real execution reuses the existing best-execution/PancakeSwap stack, with the PancakeSwap executor fixed to handle fee-on-transfer tokens.

**Tech Stack:** Python 3 (repo `.venv`), `requests`, `web3`, `pytest` (all already installed — **no new dependencies**). Public BSC JSON-RPC, DexScreener API (no key), GoPlus API (no key), BscScan API (key already in `.env`), gmgn-cli (configured on the local Windows machine only).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-16-cluster-signal-filter-design.md` — re-read before starting any task.
- No new pip dependencies. Run tests with `python -m pytest tests/<file> -v` from repo root.
- Tests must NEVER hit the network or broadcast transactions — mock `requests` and `web3` objects.
- Money values (verbatim from spec): `slice_usd: 3.0`, `total_budget_usd: 16.14`, max 5 concurrent positions, `min_wallets: 3`, `exit_wallets: 2`, `window_minutes: 15`, valve at -70% (`valve_drop_pct: 0.70`), `shadow_mode: true` on deploy.
- The old `auto_execute` flag is DELETED, replaced by the single `shadow_mode` switch.
- Startup must never act on historical events (`start_block` = latest block at process start).
- Shadow (paper) positions live in `data/copy_trade/shadow_positions.json`, never in `positions.json`.
- Follow existing code style: module docstrings explaining "why", `structlog`-style `log.info("event_name", key=value)` via `src.agent.monitor.logger.get_logger`, Vietnamese comments acceptable.
- Commit after every task with a descriptive message.

## File Structure

```
src/agent/copy_trade/
  rpc_pool.py          (new)      thin JSON-RPC client, endpoint fallback, chunked getLogs
  wallet_discovery.py  (new)      pure functions: early buyers, candidate merge, scoring
  chain_events.py      (new)      WalletEvent + ChainEventSource (getLogs → typed events)
  cluster_signal.py    (new)      ClusterBuySignalTracker (RAM-only)
  prices.py            (new)      DexScreener price + GoPlus taxes
  trade_engine.py      (new)      open/exit/valve logic, shadow vs live fills, trade journal
  positions.py         (modify)   new fields, find_by_token, close_by_token, update
  monitor.py           (rewrite)  scan loop on chain_events; Moralis code deleted
  executor.py          (modify)   handle_alert removed; buy/sell helpers reused by engine
  swap_parser.py       (DELETE)   Moralis-specific, dead after rewrite
scripts/
  build_bsc_smart_wallets.py (new)  Part 1 CLI (manual, local)
  shadow_report.py           (new)  shadow PnL report
src/agent/execution/pancakeswap.py (modify)  fee-on-transfer swap + received_out_wei
data/copy_trade/
  wallets.json         (new, generated)  the 50-wallet list
  config.json          (modify)          new copy_settings, no auto_execute/target_wallets
tests/
  test_rpc_pool.py, test_wallet_discovery.py, test_chain_events.py,
  test_cluster_signal.py, test_prices.py, test_trade_engine.py,
  test_pancakeswap_fot.py, test_copy_trade_monitor.py (rewrite),
  test_copy_trade_positions.py (extend)
```

Scope note: Part 1 and Part 2 share `rpc_pool.py`, so they live in one plan; Task 4 ends with a **hard checkpoint** (user reviews the wallet list) but Part 2 tasks do not depend on that data to be built and tested.

---

## Phase A — Part 1: 50-wallet BSC smart-money list

### Task 1: `rpc_pool.py` — JSON-RPC client with fallback + chunked getLogs

**Files:**
- Create: `src/agent/copy_trade/rpc_pool.py`
- Test: `tests/test_rpc_pool.py`

**Interfaces:**
- Consumes: nothing (stdlib + `requests` only).
- Produces (used by Tasks 2, 4, 6):
  - `TRANSFER_TOPIC: str`, `V2_SWAP_TOPIC: str`, `V3_SWAP_TOPIC: str`
  - `addr_topic(address: str) -> str` — 32-byte-padded lowercase topic form
  - `class RpcError(Exception)`
  - `class RpcPool`: `__init__(endpoints: list[str], timeout: int = 15)`,
    `call(method: str, params: list) -> object` (rotates endpoints, raises `RpcError` when all fail),
    `latest_block() -> int`,
    `get_logs(flt: dict) -> list[dict]`,
    `get_logs_chunked(from_block: int, to_block: int, topics: list, address: str | None = None, chunk: int = 2000) -> list[dict]`,
    `get_receipt(tx_hash: str) -> dict | None`,
    `get_code(address: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rpc_pool.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rpc_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.copy_trade.rpc_pool'`

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/rpc_pool.py
"""Thin JSON-RPC client over PUBLIC BSC endpoints — free, no API quota (the reason
we dropped Moralis: 10 wallets x 30s polling exhausted its free daily quota mid-day,
leaving the bot blind; see the v2 spec). Rotates through fallback endpoints because
public nodes have no SLA, and chunks eth_getLogs ranges because public nodes cap
the block span per call."""
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
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://rpc.ankr.com/bsc",
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
        for url in self._endpoints:
            try:
                r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params},
                                  timeout=self._timeout)
                r.raise_for_status()
                payload = r.json()
                if "error" in payload:
                    raise RpcError(f"{method} on {url}: {payload['error']}")
                return payload.get("result")
            except Exception as e:  # noqa: BLE001 — any endpoint failure → try next
                last_err = e
                log.debug("rpc_endpoint_failed", url=url, method=method,
                          error=type(e).__name__)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rpc_pool.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/rpc_pool.py tests/test_rpc_pool.py
git commit -m "feat(copy_trade): RpcPool - public BSC JSON-RPC with fallback + chunked getLogs"
```

---

### Task 2: `wallet_discovery.py` — early buyers + candidate merge (pure functions)

**Files:**
- Create: `src/agent/copy_trade/wallet_discovery.py`
- Test: `tests/test_wallet_discovery.py`

**Interfaces:**
- Consumes: log dicts in raw eth_getLogs shape (`topics`, `address`, `blockNumber`).
- Produces (used by Tasks 3, 4):
  - `early_buyers(logs: list[dict], exclude: set[str], max_buyers: int = 200) -> list[str]` — first-seen unique recipients (lowercase), ordered.
  - `cross_winner_candidates(buyers_by_token: dict[str, list[str]], min_tokens: int = 2) -> dict[str, int]` — address → number of winner tokens bought early.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wallet_discovery.py
from src.agent.copy_trade.wallet_discovery import (
    cross_winner_candidates, early_buyers,
)
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, addr_topic

W1 = "0x1111111111111111111111111111111111111111"
W2 = "0x2222222222222222222222222222222222222222"
W3 = "0x3333333333333333333333333333333333333333"
PAIR = "0x9999999999999999999999999999999999999999"
ZERO = "0x0000000000000000000000000000000000000000"


def _transfer_log(to_addr: str, block: int) -> dict:
    return {"address": "0xtoken", "blockNumber": hex(block),
            "topics": [TRANSFER_TOPIC, addr_topic(PAIR), addr_topic(to_addr)]}


def test_early_buyers_first_seen_order_dedup_and_exclusions():
    logs = [
        _transfer_log(ZERO, 1),          # excluded (zero)
        _transfer_log(W1, 2),
        _transfer_log(PAIR, 3),          # excluded (the pair itself)
        _transfer_log(W2, 4),
        _transfer_log(W1, 5),            # duplicate
    ]
    assert early_buyers(logs, exclude={PAIR, ZERO}) == [W1, W2]


def test_early_buyers_caps_at_max():
    logs = [_transfer_log(f"0x{i:040x}", i) for i in range(1, 50)]
    assert len(early_buyers(logs, exclude=set(), max_buyers=10)) == 10


def test_cross_winner_candidates_requires_min_tokens():
    buyers = {"tokA": [W1, W2], "tokB": [W1, W3], "tokC": [W1]}
    cands = cross_winner_candidates(buyers, min_tokens=2)
    assert cands == {W1: 3}          # W1 early in 3 winners; W2/W3 only in 1 each
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_wallet_discovery.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/wallet_discovery.py
"""Part 1 of the v2 spec: build our OWN BSC smart-money list instead of blindly
trusting GMGN's public smart_degen tag (public info = no edge). Pure functions here;
network orchestration lives in scripts/build_bsc_smart_wallets.py so everything in
this module is unit-testable offline."""
from __future__ import annotations


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def early_buyers(logs: list[dict], exclude: set[str], max_buyers: int = 200) -> list[str]:
    """First-seen unique Transfer recipients of a token's earliest logs — the wallets
    that were in BEFORE the run. Caller passes logs from pair creation onward and
    excludes the pair/zero/router addresses."""
    excl = {a.lower() for a in exclude}
    seen: dict[str, None] = {}
    for lg in logs:
        if len(lg.get("topics", [])) < 3:
            continue
        to_addr = _topic_to_addr(lg["topics"][2])
        if to_addr in excl or to_addr in seen:
            continue
        seen[to_addr] = None
        if len(seen) >= max_buyers:
            break
    return list(seen)


def cross_winner_candidates(buyers_by_token: dict[str, list[str]],
                            min_tokens: int = 2) -> dict[str, int]:
    """Address → how many distinct winner tokens it bought early. Only wallets early
    in >= min_tokens different winners qualify — one lucky entry is noise, repeated
    early entries across independent winners is the signal we're mining."""
    counts: dict[str, int] = {}
    for buyers in buyers_by_token.values():
        for addr in set(buyers):
            counts[addr] = counts.get(addr, 0) + 1
    return {a: c for a, c in counts.items() if c >= min_tokens}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_wallet_discovery.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/wallet_discovery.py tests/test_wallet_discovery.py
git commit -m "feat(copy_trade): early-buyer extraction + cross-winner candidate merge"
```

---

### Task 3: `wallet_discovery.py` — filters and scoring

**Files:**
- Modify: `src/agent/copy_trade/wallet_discovery.py` (append)
- Test: `tests/test_wallet_discovery.py` (append)

**Interfaces:**
- Consumes: `RpcPool.get_code` (Task 1); BscScan `account/txlist` JSON shape.
- Produces (used by Task 4):
  - `wallet_activity(bscscan_key: str, address: str, now_ts: int) -> dict | None` — `{"tx_7d": int, "tx_per_day": float, "last_tx_age_days": float}`; `None` on API failure.
  - `passes_filters(activity: dict, code: str) -> tuple[bool, str]` — (ok, reason).
  - `score_candidate(wins_early: int, gmgn_hits: int, in_both: bool) -> float`
  - `build_ranked_list(candidates: list[dict], top_n: int = 50) -> list[dict]` — sorted by `score` desc, truncated.

Filter thresholds (locked here per spec delegation): reject if `code != "0x"` (contract), `tx_per_day > 100` (MEV/sniper bot), `last_tx_age_days > 7` (cold wallet), `tx_7d < 3` (not actively trading).
Score: `min(wins_early, 5) * 2.0 + min(gmgn_hits, 10) * 0.3 + (3.0 if in_both else 0.0)` — early-winner evidence dominates, GMGN activity is a tie-breaker, presence in both sources is a strong confirmation.

- [ ] **Step 1: Write the failing tests (append to tests/test_wallet_discovery.py)**

```python
from src.agent.copy_trade.wallet_discovery import (
    build_ranked_list, passes_filters, score_candidate, wallet_activity,
)


def test_passes_filters_rejects_contract_bot_cold_and_inactive():
    ok_act = {"tx_7d": 20, "tx_per_day": 10.0, "last_tx_age_days": 1.0}
    assert passes_filters(ok_act, code="0x")[0] is True
    assert passes_filters(ok_act, code="0x6080")[0] is False          # contract
    assert passes_filters({**ok_act, "tx_per_day": 500.0}, "0x")[0] is False  # bot
    assert passes_filters({**ok_act, "last_tx_age_days": 9.0}, "0x")[0] is False  # cold
    assert passes_filters({**ok_act, "tx_7d": 1}, "0x")[0] is False   # barely trades


def test_score_candidate_weights():
    assert score_candidate(wins_early=3, gmgn_hits=0, in_both=False) == 6.0
    assert score_candidate(wins_early=0, gmgn_hits=10, in_both=False) == 3.0
    assert score_candidate(wins_early=2, gmgn_hits=5, in_both=True) == 2*2.0 + 1.5 + 3.0
    assert score_candidate(wins_early=99, gmgn_hits=99, in_both=True) == 10.0 + 3.0 + 3.0


def test_build_ranked_list_sorts_and_truncates():
    cands = [{"address": f"0x{i:040x}", "score": float(i)} for i in range(60)]
    ranked = build_ranked_list(cands, top_n=50)
    assert len(ranked) == 50
    assert ranked[0]["score"] == 59.0


def test_wallet_activity_parses_txlist(monkeypatch):
    now = 1_000_000_000
    txs = [{"timeStamp": str(now - 3600)}, {"timeStamp": str(now - 86400 * 2)},
           {"timeStamp": str(now - 86400 * 10)}]
    class FakeResp:
        status_code = 200
        def json(self):
            return {"status": "1", "result": txs}
        def raise_for_status(self):
            pass
    monkeypatch.setattr("src.agent.copy_trade.wallet_discovery.requests.get",
                        lambda url, params=None, timeout=None: FakeResp())
    act = wallet_activity("KEY", "0xabc", now_ts=now)
    assert act["tx_7d"] == 2
    assert act["last_tx_age_days"] < 1.0


def test_wallet_activity_returns_none_on_api_failure(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise ConnectionError("down")
    monkeypatch.setattr("src.agent.copy_trade.wallet_discovery.requests.get", boom)
    assert wallet_activity("KEY", "0xabc", now_ts=0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_wallet_discovery.py -v`
Expected: new tests FAIL with ImportError (names not defined); Task 2 tests still pass.

- [ ] **Step 3: Write the implementation (append to wallet_discovery.py)**

```python
import requests  # move to the top of the file with the other imports

BSCSCAN_URL = "https://api.etherscan.io/v2/api"   # etherscan v2 multichain, chainid=56

MAX_TX_PER_DAY = 100.0     # above this it's a sniper/MEV bot, not a human trader
MAX_LAST_TX_AGE_DAYS = 7.0
MIN_TX_7D = 3


def wallet_activity(bscscan_key: str, address: str, now_ts: int) -> dict | None:
    """Recent-activity profile from BscScan txlist (key already in .env). None on
    any failure — the caller drops the candidate rather than guessing."""
    try:
        r = requests.get(BSCSCAN_URL, params={
            "chainid": 56, "module": "account", "action": "txlist",
            "address": address, "page": 1, "offset": 200, "sort": "desc",
            "apikey": bscscan_key}, timeout=20)
        r.raise_for_status()
        result = r.json().get("result")
        if not isinstance(result, list):
            return None
        stamps = [int(t["timeStamp"]) for t in result if t.get("timeStamp")]
        if not stamps:
            return {"tx_7d": 0, "tx_per_day": 0.0, "last_tx_age_days": 9e9}
        week_ago = now_ts - 7 * 86400
        tx_7d = sum(1 for s in stamps if s >= week_ago)
        span_days = max((now_ts - min(stamps)) / 86400, 1.0)
        return {"tx_7d": tx_7d,
                "tx_per_day": len(stamps) / span_days,
                "last_tx_age_days": (now_ts - max(stamps)) / 86400}
    except Exception:  # noqa: BLE001
        return None


def passes_filters(activity: dict, code: str) -> tuple[bool, str]:
    if code != "0x":
        return False, "contract"
    if activity["tx_per_day"] > MAX_TX_PER_DAY:
        return False, "bot_tx_rate"
    if activity["last_tx_age_days"] > MAX_LAST_TX_AGE_DAYS:
        return False, "cold_wallet"
    if activity["tx_7d"] < MIN_TX_7D:
        return False, "inactive"
    return True, "ok"


def score_candidate(wins_early: int, gmgn_hits: int, in_both: bool) -> float:
    """Early-winner evidence dominates (it's OUR mined signal); GMGN trade frequency
    is a tie-breaker; showing up in both independent sources is strong confirmation."""
    return (min(wins_early, 5) * 2.0
            + min(gmgn_hits, 10) * 0.3
            + (3.0 if in_both else 0.0))


def build_ranked_list(candidates: list[dict], top_n: int = 50) -> list[dict]:
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:top_n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_wallet_discovery.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/wallet_discovery.py tests/test_wallet_discovery.py
git commit -m "feat(copy_trade): wallet filters (contract/bot/cold) + scoring for top-50 list"
```

---

### Task 4: `scripts/build_bsc_smart_wallets.py` — orchestration CLI + wallets.json

**Files:**
- Create: `scripts/build_bsc_smart_wallets.py`
- Test: `tests/test_build_bsc_smart_wallets.py`

**Interfaces:**
- Consumes: `RpcPool` (Task 1), all of `wallet_discovery` (Tasks 2-3), gmgn-cli subprocess pattern from `scripts/fetch_gmgn_smart_money.py` (`track smartmoney --chain bsc --limit 500 --raw`, trades carry `maker`), DexScreener token endpoint (`pairAddress`, `pairCreatedAt` ms).
- Produces: `data/copy_trade/wallets.json` — array of `{"address", "label", "score", "sources", "added_at", "notes"}` (spec format). Also pure helpers `gmgn_maker_counts(trades: list[dict]) -> dict[str, int]` and `assemble_candidates(gmgn_counts, early_counts) -> list[dict]` for tests.

Winner tokens are given BY HAND on the command line (spec: user + assistant pick 5-10 recent BSC winners from DexScreener, reviewed manually):
`python scripts/build_bsc_smart_wallets.py --winners 0xtok1 0xtok2 ... [--dry-run]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_bsc_smart_wallets.py
from scripts.build_bsc_smart_wallets import assemble_candidates, gmgn_maker_counts

W1, W2, W3 = "0x" + "1"*40, "0x" + "2"*40, "0x" + "3"*40


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_build_bsc_smart_wallets.py -v`
Expected: FAIL — module/functions not found

- [ ] **Step 3: Write the implementation**

```python
# scripts/build_bsc_smart_wallets.py
"""Build data/copy_trade/wallets.json — the self-built 50-wallet BSC smart-money list
(Part 1 of docs/superpowers/specs/2026-07-16-cluster-signal-filter-design.md).

Manual, LOCAL run (gmgn-cli is only configured on the dev machine, never the VPS):

    python scripts/build_bsc_smart_wallets.py --winners 0xtokA 0xtokB ... [--dry-run]

Two candidate sources, merged and scored by wallet_discovery:
  1. gmgn-cli recent smart-money trades (maker frequency = BSC activity signal)
  2. early buyers shared across >=2 of the hand-picked winner tokens (our own edge)
Prints the scored table for user review; --dry-run skips writing the file.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.copy_trade.rpc_pool import (  # noqa: E402
    DEFAULT_ENDPOINTS, TRANSFER_TOPIC, RpcPool,
)
from src.agent.copy_trade.wallet_discovery import (  # noqa: E402
    build_ranked_list, cross_winner_candidates, early_buyers, passes_filters,
    score_candidate, wallet_activity,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "copy_trade" / "wallets.json"
ZERO = "0x0000000000000000000000000000000000000000"
EARLY_WINDOW_BLOCKS = 4 * 60 * 60 // 3   # ~4h of BSC blocks after pair creation


def gmgn_maker_counts(trades: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in trades:
        addr = (t.get("maker") or "").lower()
        if addr:
            counts[addr] = counts.get(addr, 0) + 1
    return counts


def assemble_candidates(gmgn_counts: dict[str, int],
                        early_counts: dict[str, int]) -> list[dict]:
    out = []
    for addr in set(gmgn_counts) | set(early_counts):
        sources = ([s for s, hit in (("gmgn", addr in gmgn_counts),
                                     ("early_buyer", addr in early_counts)) if hit])
        out.append({
            "address": addr,
            "sources": sources,
            "score": score_candidate(wins_early=early_counts.get(addr, 0),
                                     gmgn_hits=gmgn_counts.get(addr, 0),
                                     in_both=len(sources) == 2),
        })
    return out


def fetch_gmgn_trades(limit: int = 500) -> list[dict]:
    gmgn_cli = shutil.which("gmgn-cli") or "gmgn-cli"
    proc = subprocess.run(
        [gmgn_cli, "track", "smartmoney", "--chain", "bsc",
         "--limit", str(limit), "--raw"],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print(f"gmgn-cli failed: {proc.stderr.strip()}", file=sys.stderr)
        return []
    return json.loads(proc.stdout).get("list", [])


def dexscreener_pair(token_address: str) -> dict | None:
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                     timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "bsc"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def block_at_timestamp(pool: RpcPool, ts: int) -> int:
    """Binary-search the first block at/after unix ts (public RPC has no direct API)."""
    lo, hi = 1, pool.latest_block()
    while lo < hi:
        mid = (lo + hi) // 2
        blk = pool.call("eth_getBlockByNumber", [hex(mid), False])
        if int(blk["timestamp"], 16) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def scan_winner(pool: RpcPool, token_address: str) -> list[str]:
    pair = dexscreener_pair(token_address)
    if pair is None or not pair.get("pairCreatedAt"):
        print(f"  !! no BSC pair found for {token_address} — skipping")
        return []
    created_ts = int(pair["pairCreatedAt"]) // 1000
    start = block_at_timestamp(pool, created_ts)
    logs = pool.get_logs_chunked(start, start + EARLY_WINDOW_BLOCKS,
                                 topics=[TRANSFER_TOPIC], address=token_address)
    exclude = {pair["pairAddress"].lower(), token_address.lower(), ZERO}
    buyers = early_buyers(logs, exclude=exclude)
    print(f"  {pair['baseToken']['symbol']}: {len(logs)} transfers, "
          f"{len(buyers)} early buyers")
    return buyers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--winners", nargs="+", required=True,
                    help="5-10 hand-picked recent BSC winner token addresses")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    bscscan_key = env.get("BSCSCAN_API_KEY", "")
    pool = RpcPool(DEFAULT_ENDPOINTS)
    now = int(time.time())

    print("== source 1: gmgn-cli recent smart-money trades ==")
    gmgn_counts = gmgn_maker_counts(fetch_gmgn_trades())
    print(f"  {len(gmgn_counts)} distinct makers")

    print("== source 2: early buyers across winner tokens ==")
    buyers_by_token = {t: scan_winner(pool, t) for t in args.winners}
    early_counts = cross_winner_candidates(buyers_by_token, min_tokens=2)
    print(f"  {len(early_counts)} wallets early in >=2 winners")

    candidates = assemble_candidates(gmgn_counts, early_counts)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    print(f"== filtering top candidates (contract/bot/cold checks, need {args.top}) ==")
    kept: list[dict] = []
    for c in candidates:
        if len(kept) >= args.top:
            break
        act = wallet_activity(bscscan_key, c["address"], now)
        if act is None:
            print(f"  skip {c['address'][:12]}… activity lookup failed")
            continue
        ok, reason = passes_filters(act, pool.get_code(c["address"]))
        if not ok:
            print(f"  drop {c['address'][:12]}… ({reason})")
            continue
        kept.append(c)
        time.sleep(0.25)   # BscScan free tier: 5 req/s

    ranked = build_ranked_list(kept, top_n=args.top)
    added_at = datetime.now(timezone.utc).isoformat()
    wallets = [{"address": c["address"], "label": f"BSC_SMART_{i+1:02d}",
                "score": round(c["score"], 2), "sources": c["sources"],
                "added_at": added_at, "notes": ""}
               for i, c in enumerate(ranked)]

    print(f"\n{'label':<14}{'score':>7}  sources          address")
    for w in wallets:
        print(f"{w['label']:<14}{w['score']:>7}  {','.join(w['sources']):<16} {w['address']}")

    if args.dry_run:
        print("\n--dry-run: not writing wallets.json")
        return
    OUT_PATH.write_text(json.dumps(wallets, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nWrote {len(wallets)} wallets to {OUT_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_build_bsc_smart_wallets.py -v`
Expected: 2 passed

- [ ] **Step 5: Smoke-run help + commit**

Run: `python scripts/build_bsc_smart_wallets.py --help`
Expected: usage text, exit 0.

```bash
git add scripts/build_bsc_smart_wallets.py tests/test_build_bsc_smart_wallets.py
git commit -m "feat(copy_trade): build_bsc_smart_wallets CLI - GMGN + early-buyer hybrid list"
```

> **CHECKPOINT (do not skip):** the real run needs winner tokens picked with the user
> and produces the list the user must approve before deploy. Do NOT run the real
> scan inside this task; it happens with the user after the build (see Task 13).

---

## Phase B — Part 2: RPC events, cluster gate, shadow-mode

### Task 5: PancakeSwap fee-on-transfer fix + received amount

**Files:**
- Modify: `src/agent/execution/pancakeswap.py`
- Test: `tests/test_pancakeswap_fot.py`

**Interfaces:**
- Consumes: existing `PancakeSwap`, `SwapResult`, `ROUTER_ABI`, `ERC20_ABI`.
- Produces (used by Tasks 10, 11):
  - `PancakeSwap.__init__(..., slippage_bps: int | None = None)` — per-instance override (copy-trade uses 1500 for taxed memes; Aegis default unchanged).
  - `_build_swap_tx` now calls `swapExactTokensForTokensSupportingFeeOnTransferTokens` (works for both taxed and untaxed tokens; the plain variant reverts on taxed sells — root cause of the 2026-07-05/06 stuck exits).
  - `SwapResult` gains `received_out_wei: int = 0` — actual balanceOf delta of token_out (live swaps only), so taxed-token fills are recorded truthfully.

**Changes:**
1. Append to `ROUTER_ABI`:

```python
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
```

2. `SwapResult` dataclass: add field `received_out_wei: int = 0`.
3. `__init__` signature: `def __init__(self, w3=None, account=None, dry_run: bool | None = None, slippage_bps: int | None = None)`; body: `self.slippage_bps = settings.slippage_bps if slippage_bps is None else slippage_bps`.
4. `_build_swap_tx`: replace `self.router.functions.swapExactTokensForTokens(` with `self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(` (args identical).
5. In `swap()` live path, measure the real fill:

```python
        out_tok = get_token(q.token_out)
        out_erc20 = self.w3.eth.contract(address=out_tok.address, abi=ERC20_ABI)
        bal_before = out_erc20.functions.balanceOf(self.account.address).call()
        self._approve(token_in, q.amount_in_wei)
        tx = self._build_swap_tx(q)
        tx_hash = self._sign_and_send(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        hash_str = _to_hex(tx_hash)
        if getattr(receipt, "status", 1) != 1:
            log.warning("swap_reverted", token_in=token_in, token_out=token_out, tx_hash=hash_str)
            raise RuntimeError(f"PancakeSwap swap reverted on-chain (status 0): {hash_str}")
        received = out_erc20.functions.balanceOf(self.account.address).call() - bal_before
        log.info("swap_sent", token_in=token_in, token_out=token_out, tx_hash=hash_str)
        return SwapResult(q.token_in, q.token_out, q.amount_in_wei,
                          q.expected_out_wei, q.min_out_wei, simulated=False,
                          tx_hash=hash_str, received_out_wei=received)
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pancakeswap_fot.py
"""Fee-on-transfer support: the SupportingFee router function must be used and the
real received amount recorded — taxed memes revert on the plain variant and deliver
less than the quote (root cause of the stuck 金狗/未来协议 exits, 2026-07-05)."""
from unittest.mock import MagicMock

from src.agent.execution.pancakeswap import PancakeSwap, SwapResult


def _fake_dex(monkeypatch=None, slippage_bps=None):
    w3 = MagicMock()
    account = MagicMock()
    account.address = "0x" + "a" * 40
    dex = PancakeSwap(w3=w3, account=account, dry_run=True, slippage_bps=slippage_bps)
    return dex, w3


def test_slippage_override_wins_over_settings():
    dex, _ = _fake_dex(slippage_bps=1500)
    assert dex.slippage_bps == 1500


def test_default_slippage_still_from_settings():
    from src.agent.config import settings
    dex, _ = _fake_dex(slippage_bps=None)
    assert dex.slippage_bps == settings.slippage_bps


def test_build_swap_tx_uses_supporting_fee_variant():
    dex, w3 = _fake_dex()
    q = MagicMock(amount_in_wei=1, min_out_wei=1, path=["0xa", "0xb"])
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.gas_price = 1
    dex._build_swap_tx(q)
    assert dex.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens.called
    assert not dex.router.functions.swapExactTokensForTokens.called


def test_swap_result_has_received_out_wei_default():
    r = SwapResult("A", "B", 1, 2, 1, simulated=True)
    assert r.received_out_wei == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pancakeswap_fot.py -v`
Expected: FAIL — unexpected kwarg `slippage_bps`, missing field, wrong router fn.

- [ ] **Step 3: Apply the 5 changes above**

- [ ] **Step 4: Run new + existing execution tests**

Run: `python -m pytest tests/test_pancakeswap_fot.py tests/ -k "pancake or executor" -v`
Expected: all pass (existing suites must not regress).

- [ ] **Step 5: Commit**

```bash
git add src/agent/execution/pancakeswap.py tests/test_pancakeswap_fot.py
git commit -m "fix(execution): fee-on-transfer swaps via SupportingFee variant + real received amount"
```

---

### Task 6: `chain_events.py` — typed wallet events from getLogs

**Files:**
- Create: `src/agent/copy_trade/chain_events.py`
- Test: `tests/test_chain_events.py`

**Interfaces:**
- Consumes: `RpcPool` (Task 1) — `latest_block`, `get_logs_chunked`, `get_receipt`; topic constants.
- Produces (used by Task 11):
  - `@dataclass(frozen=True) WalletEvent`: `wallet: str` (lowercase), `token_address: str` (lowercase), `direction: str` ("in"|"out"), `amount_raw: int`, `tx_hash: str`, `block: int`.
  - `class ChainEventSource`: `__init__(pool: RpcPool, wallets: list[str], start_block: int, ignore_tokens: set[str] | None = None)`; `poll() -> list[WalletEvent]`; attribute `last_processed: int`.
  - Buy events ("in") only emitted when the tx receipt contains a V2/V3 `Swap` event (filters airdrops/plain transfers). "out" events emitted unconditionally (any token leaving a wallet counts as abandonment — the root fix for missed multi-hop sells).
  - `ignore_tokens`: lowercase token addresses never reported (USDT/WBNB/stables — a wallet receiving USDT is the other side of a sell, not a buy signal).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chain_events.py
from unittest.mock import MagicMock

from src.agent.copy_trade.chain_events import ChainEventSource, WalletEvent
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, V2_SWAP_TOPIC, addr_topic

W = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
TOKEN = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
USDT = "0x55d398326f99059ff775485246999027b3197955"


def _log(token, frm, to, block=101, tx="0x" + "f" * 64):
    return {"address": token, "blockNumber": hex(block), "transactionHash": tx,
            "data": hex(10**18),
            "topics": [TRANSFER_TOPIC, addr_topic(frm), addr_topic(to)]}


def _source(logs, receipt_has_swap=True, latest=150):
    pool = MagicMock()
    pool.latest_block.return_value = latest
    pool.get_logs_chunked.return_value = logs
    swap_log = {"topics": [V2_SWAP_TOPIC]} if receipt_has_swap else {"topics": ["0xdead"]}
    pool.get_receipt.return_value = {"logs": [swap_log]}
    return ChainEventSource(pool, wallets=[W], start_block=100,
                            ignore_tokens={USDT}), pool


def test_incoming_with_swap_event_is_buy():
    src, _ = _source([_log(TOKEN, OTHER, W)])
    events = src.poll()
    assert events == [WalletEvent(wallet=W, token_address=TOKEN, direction="in",
                                  amount_raw=10**18, tx_hash="0x" + "f" * 64, block=101)]


def test_incoming_without_swap_event_is_dropped():
    src, _ = _source([_log(TOKEN, OTHER, W)], receipt_has_swap=False)
    assert src.poll() == []


def test_outgoing_needs_no_swap_event():
    src, _ = _source([_log(TOKEN, W, OTHER)], receipt_has_swap=False)
    assert [e.direction for e in src.poll()] == ["out"]


def test_ignore_tokens_filtered():
    src, _ = _source([_log(USDT, OTHER, W)])
    assert src.poll() == []


def test_poll_advances_and_never_rescans():
    src, pool = _source([_log(TOKEN, OTHER, W)])
    src.poll()
    assert src.last_processed == 150
    pool.get_logs_chunked.return_value = []
    pool.latest_block.return_value = 160
    src.poll()
    args = pool.get_logs_chunked.call_args_list[-1]
    assert args.kwargs.get("from_block", args.args[0] if args.args else None) == 151


def test_no_new_blocks_returns_empty_without_scanning():
    src, pool = _source([], latest=100)
    src.last_processed = 100
    assert src.poll() == []


def test_wallet_to_wallet_between_tracked_emits_out_only_for_sender():
    src, _ = _source([_log(TOKEN, W, W)], receipt_has_swap=False)
    # degenerate self-transfer: must not crash, must not emit "in" without swap
    assert all(e.direction == "out" for e in src.poll())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chain_events.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/chain_events.py
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
            for lg in self._pool.get_logs_chunked(frm, to, topics=topics):
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
        token = lg["address"].lower()
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
                           tx_hash=tx_hash, block=int(lg["blockNumber"], 16))

    def _tx_has_swap(self, tx_hash: str) -> bool:
        if tx_hash in self._receipt_swap_cache:
            return self._receipt_swap_cache[tx_hash]
        receipt = self._pool.get_receipt(tx_hash) or {}
        has = any(l.get("topics", [""])[0] in (V2_SWAP_TOPIC, V3_SWAP_TOPIC)
                  for l in receipt.get("logs", []))
        self._receipt_swap_cache[tx_hash] = has
        if len(self._receipt_swap_cache) > 2000:   # ponytail: crude cap, fine for 50 wallets
            self._receipt_swap_cache.clear()
        return has
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chain_events.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/chain_events.py tests/test_chain_events.py
git commit -m "feat(copy_trade): ChainEventSource - RPC Transfer events with swap-confirmed buys"
```

---

### Task 7: `cluster_signal.py` — convergence tracker

**Files:**
- Create: `src/agent/copy_trade/cluster_signal.py`
- Test: `tests/test_cluster_signal.py`

**Interfaces:**
- Produces (used by Task 11):
  - `class ClusterBuySignalTracker`: `__init__(min_wallets: int = 3, window_minutes: int = 15)`;
    `record(token_address: str, wallet: str, ts: float, price_usd: float | None) -> dict | None`.
    Returns `None` below threshold; at threshold returns
    `{"wallets": [w1, w2, w3], "first_ts": float, "first_price_usd": float | None}`
    (wallets in first-buy order; `first_price_usd` = price snapshot at the EARLIEST
    in-window observation — powers the entry-lateness metric in the shadow report).
  - RAM-only by design (pre-threshold there is no money at risk); self-prunes observations older than the window on every call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cluster_signal.py
from src.agent.copy_trade.cluster_signal import ClusterBuySignalTracker

T = "0x" + "a" * 40
W1, W2, W3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


def test_below_threshold_returns_none():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    assert tr.record(T, W1, ts=0, price_usd=1.0) is None
    assert tr.record(T, W2, ts=60, price_usd=1.1) is None


def test_three_distinct_wallets_in_window_fires_with_first_price():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W2, ts=300, price_usd=1.5)
    got = tr.record(T, W3, ts=600, price_usd=2.0)
    assert got == {"wallets": [W1, W2, W3], "first_ts": 0, "first_price_usd": 1.0}


def test_same_wallet_repeat_buys_do_not_count_twice():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W1, ts=10, price_usd=1.0)
    assert tr.record(T, W2, ts=20, price_usd=1.0) is None


def test_observations_outside_window_are_pruned():
    tr = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    tr.record(T, W2, ts=60, price_usd=1.0)
    assert tr.record(T, W3, ts=16 * 60, price_usd=1.0) is None  # W1 aged out


def test_tokens_are_independent():
    tr = ClusterBuySignalTracker(min_wallets=2, window_minutes=15)
    tr.record(T, W1, ts=0, price_usd=1.0)
    assert tr.record("0x" + "b" * 40, W2, ts=1, price_usd=1.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cluster_signal.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/cluster_signal.py
"""Convergence gate (v2 spec §2): a single GMGN-tagged wallet buying is public info
everyone sees — no edge. Three DISTINCT tracked wallets converging on one token
inside 15 minutes is our self-built signal. RAM-only on purpose: pre-threshold
there is no money at risk, so losing the buffer on restart is acceptable (unlike
positions, which are always disk-persisted)."""
from __future__ import annotations

from ..monitor.logger import get_logger

log = get_logger(__name__)


class ClusterBuySignalTracker:
    def __init__(self, min_wallets: int = 3, window_minutes: int = 15) -> None:
        self._min = min_wallets
        self._window_s = window_minutes * 60
        # token -> wallet -> (first_ts_in_window, price_at_first_obs)
        self._obs: dict[str, dict[str, tuple[float, float | None]]] = {}

    def record(self, token_address: str, wallet: str, ts: float,
               price_usd: float | None) -> dict | None:
        token, wallet = token_address.lower(), wallet.lower()
        per_token = self._obs.setdefault(token, {})
        per_token = {w: v for w, v in per_token.items() if ts - v[0] <= self._window_s}
        if wallet not in per_token:
            per_token[wallet] = (ts, price_usd)
        self._obs[token] = per_token
        if len(per_token) < self._min:
            log.info("cluster_pending", token=token, wallets=len(per_token),
                     need=self._min)
            return None
        ordered = sorted(per_token.items(), key=lambda kv: kv[1][0])
        first_ts, first_price = ordered[0][1]
        return {"wallets": [w for w, _ in ordered],
                "first_ts": first_ts, "first_price_usd": first_price}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cluster_signal.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/cluster_signal.py tests/test_cluster_signal.py
git commit -m "feat(copy_trade): ClusterBuySignalTracker - 3-wallet/15-min convergence gate"
```

---

### Task 8: `positions.py` — cluster fields, token lookup, shadow separation

**Files:**
- Modify: `src/agent/copy_trade/positions.py`
- Test: `tests/test_copy_trade_positions.py` (append)

**Interfaces:**
- Consumes: existing `CopyPosition`, `PositionStore` (unchanged methods stay).
- Produces (used by Tasks 10, 11):
  - `CopyPosition` new fields, ALL with defaults (backward-compatible with any old JSON):
    `cluster_wallets: list[str] = field(default_factory=list)`,
    `exited_by: list[str] = field(default_factory=list)`,
    `entry_price_usd: float = 0.0`, `simulated: bool = False`.
  - `PositionStore.find_by_token(token_address: str) -> CopyPosition | None`
  - `PositionStore.close_by_token(token_address: str) -> CopyPosition | None`
  - `PositionStore.update(pos: CopyPosition) -> None` — persist after in-place mutation (e.g. `exited_by.append`); raises `ValueError` if pos not in store.

- [ ] **Step 1: Write the failing tests (append to tests/test_copy_trade_positions.py)**

```python
def _cluster_pos(token="0x" + "c" * 40, simulated=True):
    from src.agent.copy_trade.positions import CopyPosition
    return CopyPosition(token_symbol="GEM", token_address=token, token_decimals=18,
                        source_wallet="", usd_size=3.0, token_amount=100.0,
                        opened_at="2026-07-16T00:00:00+00:00",
                        cluster_wallets=["0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40],
                        entry_price_usd=0.03, simulated=simulated)


def test_new_fields_default_for_legacy_json(tmp_path):
    from src.agent.copy_trade.positions import CopyPosition, PositionStore
    p = tmp_path / "positions.json"
    p.write_text('[{"token_symbol": "OLD", "token_address": "0xabc", '
                 '"token_decimals": 18, "source_wallet": "0xdef", "usd_size": 1.5, '
                 '"token_amount": 10.0, "opened_at": "t"}]', encoding="utf-8")
    store = PositionStore(p)
    store.load()
    pos = store.all()[0]
    assert pos.cluster_wallets == [] and pos.exited_by == []
    assert pos.entry_price_usd == 0.0 and pos.simulated is False


def test_find_by_token_and_close_by_token(tmp_path):
    from src.agent.copy_trade.positions import PositionStore
    store = PositionStore(tmp_path / "p.json")
    store.load()
    pos = _cluster_pos()
    store.open_position(pos)
    assert store.find_by_token(pos.token_address.upper()) is pos   # case-insensitive
    assert store.find_by_token("0x" + "d" * 40) is None
    closed = store.close_by_token(pos.token_address)
    assert closed is pos and store.all() == []


def test_update_persists_exited_by(tmp_path):
    from src.agent.copy_trade.positions import PositionStore
    path = tmp_path / "p.json"
    store = PositionStore(path)
    store.load()
    pos = _cluster_pos()
    store.open_position(pos)
    pos.exited_by.append(pos.cluster_wallets[0])
    store.update(pos)
    reloaded = PositionStore(path)
    reloaded.load()
    assert reloaded.all()[0].exited_by == [pos.cluster_wallets[0]]


def test_update_unknown_position_raises(tmp_path):
    from src.agent.copy_trade.positions import PositionStore
    import pytest
    store = PositionStore(tmp_path / "p.json")
    store.load()
    with pytest.raises(ValueError):
        store.update(_cluster_pos())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_copy_trade_positions.py -v`
Expected: new tests FAIL (unexpected kwargs / missing methods); old tests pass.

- [ ] **Step 3: Implement**

In `positions.py`: add `field` to the dataclasses import, extend `CopyPosition`:

```python
from dataclasses import asdict, dataclass, field


@dataclass
class CopyPosition:
    token_symbol: str
    token_address: str
    token_decimals: int
    source_wallet: str          # legacy 1:1 field; "" for cluster positions
    usd_size: float
    token_amount: float
    opened_at: str
    # v2 cluster fields — defaults keep any pre-v2 positions.json loadable
    cluster_wallets: list[str] = field(default_factory=list)
    exited_by: list[str] = field(default_factory=list)
    entry_price_usd: float = 0.0
    simulated: bool = False
```

Append to `PositionStore`:

```python
    def find_by_token(self, token_address: str) -> CopyPosition | None:
        for p in self._positions:
            if p.token_address.lower() == token_address.lower():
                return p
        return None

    def close_by_token(self, token_address: str) -> CopyPosition | None:
        pos = self.find_by_token(token_address)
        if pos is None:
            return None
        self._positions.remove(pos)
        self._save()
        return pos

    def update(self, pos: CopyPosition) -> None:
        if pos not in self._positions:
            raise ValueError("position not in store")
        self._save()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_copy_trade_positions.py -v`
Expected: all pass (old + 4 new)

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/positions.py tests/test_copy_trade_positions.py
git commit -m "feat(copy_trade): cluster/exit/simulated fields + token-keyed lookup on PositionStore"
```

---

### Task 9: `prices.py` — DexScreener price + GoPlus taxes

**Files:**
- Create: `src/agent/copy_trade/prices.py`
- Test: `tests/test_prices.py`

**Interfaces:**
- Produces (used by Tasks 10, 11, 12):
  - `get_price_usd(token_address: str) -> float | None` — best-liquidity BSC pair price from `https://api.dexscreener.com/latest/dex/tokens/{addr}`; `None` on any failure (callers must fail safe, never guess a price).
  - `get_taxes(token_address: str) -> tuple[float, float] | None` — `(buy_tax, sell_tax)` as fractions from `https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={addr}`; `None` on failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prices.py
from src.agent.copy_trade import prices

TOKEN = "0x" + "a" * 40


class FakeResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


def test_get_price_picks_highest_liquidity_bsc_pair(monkeypatch):
    payload = {"pairs": [
        {"chainId": "bsc", "priceUsd": "2.0", "liquidity": {"usd": 100}},
        {"chainId": "bsc", "priceUsd": "3.0", "liquidity": {"usd": 900}},
        {"chainId": "ethereum", "priceUsd": "9.9", "liquidity": {"usd": 99999}},
    ]}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_price_usd(TOKEN) == 3.0


def test_get_price_none_on_failure(monkeypatch):
    def boom(url, timeout=None):
        raise ConnectionError()
    monkeypatch.setattr(prices.requests, "get", boom)
    assert prices.get_price_usd(TOKEN) is None


def test_get_taxes_parses_goplus(monkeypatch):
    payload = {"result": {TOKEN: {"buy_tax": "0.04", "sell_tax": "0.05"}}}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_taxes(TOKEN) == (0.04, 0.05)


def test_get_taxes_none_on_missing(monkeypatch):
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp({"result": {}}))
    assert prices.get_taxes(TOKEN) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prices.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/prices.py
"""Price/tax lookups for the valve and shadow fills. Free keyless APIs; every
function returns None on failure — callers hold state and alert rather than guess
(spec: 'lỗi thì giữ nguyên trạng thái và alert, không đoán giá')."""
from __future__ import annotations

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

_DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="


def get_price_usd(token_address: str) -> float | None:
    try:
        r = requests.get(_DEXSCREENER + token_address, timeout=15)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "bsc" and p.get("priceUsd")]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        return float(best["priceUsd"])
    except Exception as e:  # noqa: BLE001
        log.warning("dexscreener_price_failed", token=token_address,
                    error=type(e).__name__)
        return None


def get_taxes(token_address: str) -> tuple[float, float] | None:
    try:
        r = requests.get(_GOPLUS + token_address, timeout=15)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return None
        return float(info.get("buy_tax") or 0), float(info.get("sell_tax") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("goplus_taxes_failed", token=token_address, error=type(e).__name__)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_prices.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/prices.py tests/test_prices.py
git commit -m "feat(copy_trade): DexScreener price + GoPlus tax lookups (fail-safe None)"
```

---

### Task 10: `trade_engine.py` — shadow/live fills, cluster exits, valve, journal

**Files:**
- Create: `src/agent/copy_trade/trade_engine.py`
- Test: `tests/test_trade_engine.py`

**Interfaces:**
- Consumes: `CopyTradeBudget` (allocate/release/can_open_new), `PositionStore` + `CopyPosition` (Task 8), `prices.get_price_usd`/`get_taxes` (Task 9), `rank_backends` + executors dict + `passes_safety_check` + `register_discovered` (all existing — same usage as the old `executor.py`).
- Produces (used by Tasks 11, 12):
  - `class TradeEngine`: `__init__(budget, store, executors: dict | None, shadow_mode: bool, journal_path: Path, exit_wallets: int = 2, valve_drop_pct: float = 0.70, slice_usd: float = 3.0)`
  - `open_cluster_position(token_address, token_symbol, token_decimals, cluster: dict) -> bool` — safety gate → shadow paper fill OR live buy; dedup via `find_by_token` happens in the monitor BEFORE calling this.
  - `on_exit_signal(wallet: str, token_address: str) -> None` — spec §3 rules (non-cluster wallet ignored; >=exit_wallets → close).
  - `check_valve() -> None` — price <= entry*(1-valve_drop_pct) → close, reason "valve".
  - Every close appends one JSON line to `journal_path` (`data/copy_trade/closed_trades.jsonl`): `{token_address, token_symbol, simulated, usd_size, entry_price_usd, exit_price_usd, pnl_usd, pnl_pct, opened_at, closed_at, reason, cluster_wallets, exited_by, first_price_usd, fees_model_usd}`.
  - **Shadow invariant (the most important test in the plan):** with `shadow_mode=True` the engine NEVER touches `executors` — it may be `None`.
  - Shadow fill model: `entry_price = price * (1 + buy_tax + 0.01)`; fees_model_usd = `0.20 + slice_usd * (buy_tax + sell_tax + 0.02)` (gas ~$0.10/leg + taxes + ~1% impact per leg); taxes default `(0.05, 0.05)` when `get_taxes` returns `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trade_engine.py
import json
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.positions import PositionStore
from src.agent.copy_trade.trade_engine import TradeEngine

T = "0x" + "a" * 40
W1, W2, W3, OUTSIDER = ("0x" + c * 40 for c in "1234")
CLUSTER = {"wallets": [W1, W2, W3], "first_ts": 0.0, "first_price_usd": 1.0}


def _engine(tmp_path, shadow=True, executors=None):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=executors,
                      shadow_mode=shadow, journal_path=tmp_path / "closed.jsonl")
    return eng, budget, store


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_shadow_open_never_touches_executors(_g, _p, _t, tmp_path):
    executors = MagicMock()
    eng, budget, store = _engine(tmp_path, shadow=True, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    pos = store.find_by_token(T)
    assert pos.simulated is True
    assert pos.entry_price_usd == 2.0 * (1 + 0.04 + 0.01)
    assert pos.cluster_wallets == [W1, W2, W3]
    assert budget.available_usd == 16.14 - 3.0
    executors.assert_not_called()
    assert not executors.method_calls        # zero interaction, ever


@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(False, None))
def test_safety_gate_blocks_and_releases_budget(_s, tmp_path):
    eng, budget, store = _engine(tmp_path)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14
    assert store.all() == []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_exit_needs_two_cluster_wallets_outsiders_ignored(_g, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.on_exit_signal(OUTSIDER, T)
    eng.on_exit_signal(W1, T)
    assert store.find_by_token(T) is not None            # 1 of 3 — still holding
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W1, T)                            # duplicate — still 1
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W2, T)                            # 2 of 3 — close
    assert store.find_by_token(T) is None
    assert budget.available_usd == 16.14                 # slice released


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_closes_at_70pct_drawdown(_s, price_mock, _t, tmp_path):
    price_mock.return_value = 10.0
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18,
                              {"wallets": [W1, W2, W3], "first_ts": 0.0,
                               "first_price_usd": 10.0})
    entry = store.find_by_token(T).entry_price_usd
    price_mock.return_value = entry * 0.31               # -69% — hold
    eng.check_valve()
    assert store.find_by_token(T) is not None
    price_mock.return_value = entry * 0.29               # -71% — dump
    eng.check_valve()
    assert store.find_by_token(T) is None
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "valve" and row["simulated"] is True
    assert row["pnl_pct"] < -0.5


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=None)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_holds_when_price_unavailable(_s, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    # open with a known price first
    with patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=5.0):
        eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.check_valve()                                    # price None — do nothing
    assert store.find_by_token(T) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_trade_engine.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# src/agent/copy_trade/trade_engine.py
"""Open/exit/valve logic for cluster-gated copy trading — one engine, two fill modes.

shadow_mode=True  → paper fills at DexScreener price + a fee model; positions go to
                    the SHADOW store; `executors` is never touched (may be None).
shadow_mode=False → real fills through the existing best-execution stack, identical
                    to the old executor.py flow (safety gate, ranked backends,
                    full exit failover).
Every close (either mode) appends one JSON line to the closed-trades journal — the
shadow report and the go-live decision are built from that file."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..execution.binance_web3 import passes_safety_check
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .positions import CopyPosition, PositionStore
from .prices import get_price_usd, get_taxes

log = get_logger(__name__)

DEFAULT_TAXES = (0.05, 0.05)     # conservative when GoPlus has no data
GAS_USD_PER_LEG = 0.10
IMPACT_PER_LEG = 0.01


class TradeEngine:
    def __init__(self, budget: CopyTradeBudget, store: PositionStore,
                 executors: dict | None, shadow_mode: bool, journal_path: Path,
                 exit_wallets: int = 2, valve_drop_pct: float = 0.70,
                 slice_usd: float = 3.0) -> None:
        self._budget = budget
        self._store = store
        self._executors = executors
        self._shadow = shadow_mode
        self._journal_path = journal_path
        self._exit_wallets = exit_wallets
        self._valve_drop = valve_drop_pct
        self._slice = slice_usd

    # ---------- open ----------

    def open_cluster_position(self, token_address: str, token_symbol: str,
                              token_decimals: int, cluster: dict) -> bool:
        if not self._budget.can_open_new():
            log.info("cluster_buy_skipped_budget", token=token_symbol)
            return False
        usd_size = self._budget.allocate()
        amount_wei = str(int(usd_size * 10 ** 18))   # USDT: 18 decimals on BSC
        ok, decimals = passes_safety_check(settings.usdt_address, token_address,
                                           amount_wei)
        if not ok:
            self._budget.release(usd_size)
            log.warning("cluster_buy_skipped_safety", token=token_symbol)
            return False
        resolved_decimals = decimals or token_decimals
        try:
            if self._shadow:
                pos = self._paper_fill(token_address, token_symbol,
                                       resolved_decimals, usd_size, cluster)
            else:
                pos = self._live_fill(token_address, token_symbol,
                                      resolved_decimals, usd_size, cluster)
        except Exception:
            self._budget.release(usd_size)   # never leak a slice on failure
            raise
        if pos is None:
            self._budget.release(usd_size)
            return False
        self._store.open_position(pos)
        log.info("cluster_position_opened", token=token_symbol,
                 simulated=pos.simulated, entry=pos.entry_price_usd)
        return True

    def _paper_fill(self, token_address, token_symbol, decimals, usd_size,
                    cluster) -> CopyPosition | None:
        price = get_price_usd(token_address)
        if price is None or price <= 0:
            log.warning("shadow_fill_no_price", token=token_symbol)
            return None
        buy_tax, _ = get_taxes(token_address) or DEFAULT_TAXES
        entry = price * (1 + buy_tax + IMPACT_PER_LEG)
        return CopyPosition(
            token_symbol=token_symbol, token_address=token_address.lower(),
            token_decimals=decimals, source_wallet="", usd_size=usd_size,
            token_amount=usd_size / entry,
            opened_at=datetime.now(timezone.utc).isoformat(),
            cluster_wallets=cluster["wallets"], entry_price_usd=entry,
            simulated=True)

    def _live_fill(self, token_address, token_symbol, decimals, usd_size,
                   cluster) -> CopyPosition | None:
        register_discovered(token_symbol, token_address, decimals)
        ranked = rank_backends(self._executors, "USDT", token_symbol, usd_size)
        if not ranked:
            log.warning("cluster_buy_no_route", token=token_symbol)
            return None
        result = self._executors[ranked[0]].swap("USDT", token_symbol, usd_size)
        received_wei = getattr(result, "received_out_wei", 0) or \
            getattr(result, "expected_out_wei", 0)
        token_amount = received_wei / (10 ** decimals)
        if token_amount <= 0:
            log.warning("cluster_buy_zero_fill", token=token_symbol)
            return None
        return CopyPosition(
            token_symbol=token_symbol, token_address=token_address.lower(),
            token_decimals=decimals, source_wallet="", usd_size=usd_size,
            token_amount=token_amount,
            opened_at=datetime.now(timezone.utc).isoformat(),
            cluster_wallets=cluster["wallets"],
            entry_price_usd=usd_size / token_amount, simulated=False)

    # ---------- exits ----------

    def on_exit_signal(self, wallet: str, token_address: str) -> None:
        pos = self._store.find_by_token(token_address)
        if pos is None:
            return
        w = wallet.lower()
        if w not in (cw.lower() for cw in pos.cluster_wallets):
            log.debug("exit_signal_outside_cluster", token=pos.token_symbol)
            return
        if w not in (e.lower() for e in pos.exited_by):
            pos.exited_by.append(w)
            self._store.update(pos)
            log.info("cluster_exit_vote", token=pos.token_symbol,
                     votes=len(pos.exited_by), need=self._exit_wallets)
        if len(pos.exited_by) >= self._exit_wallets:
            self._close(pos, reason="cluster_sell")

    def check_valve(self) -> None:
        for pos in self._store.all():
            price = get_price_usd(pos.token_address)
            if price is None or pos.entry_price_usd <= 0:
                continue   # no price → hold state, never guess (spec)
            if price <= pos.entry_price_usd * (1 - self._valve_drop):
                log.warning("valve_triggered", token=pos.token_symbol,
                            entry=pos.entry_price_usd, price=price)
                self._close(pos, reason="valve")

    def _close(self, pos: CopyPosition, reason: str) -> None:
        exit_price = get_price_usd(pos.token_address) or 0.0
        if not pos.simulated:
            if not self._sell_live(pos):
                return   # keep position open; a later signal/valve tick retries
        _, sell_tax = get_taxes(pos.token_address) or DEFAULT_TAXES
        effective_exit = exit_price * (1 - sell_tax - IMPACT_PER_LEG)
        pnl_usd = (effective_exit - pos.entry_price_usd) * pos.token_amount
        buy_tax, _ = get_taxes(pos.token_address) or DEFAULT_TAXES
        fees_model = 2 * GAS_USD_PER_LEG + pos.usd_size * (buy_tax + sell_tax
                                                           + 2 * IMPACT_PER_LEG)
        self._store.close_by_token(pos.token_address)
        self._budget.release(pos.usd_size)
        self._journal({
            "token_address": pos.token_address, "token_symbol": pos.token_symbol,
            "simulated": pos.simulated, "usd_size": pos.usd_size,
            "entry_price_usd": pos.entry_price_usd, "exit_price_usd": effective_exit,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round((effective_exit / pos.entry_price_usd - 1), 4)
            if pos.entry_price_usd else None,
            "opened_at": pos.opened_at,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "cluster_wallets": pos.cluster_wallets,
            "exited_by": pos.exited_by, "fees_model_usd": round(fees_model, 4)})
        log.info("cluster_position_closed", token=pos.token_symbol, reason=reason,
                 simulated=pos.simulated, pnl_usd=round(pnl_usd, 4))

    def _sell_live(self, pos: CopyPosition) -> bool:
        """Full-failover live sell (mirrors the old executor.py exit path)."""
        ranked = rank_backends(self._executors, pos.token_symbol, "USDT",
                               pos.token_amount)
        for backend in ranked:
            try:
                self._executors[backend].swap(pos.token_symbol, "USDT",
                                              pos.token_amount)
                return True
            except Exception as e:  # noqa: BLE001 — try every backend
                log.warning("live_sell_failed", token=pos.token_symbol,
                            backend=backend, error=str(e))
        log.error("live_sell_all_backends_failed", token=pos.token_symbol)
        return False

    def _journal(self, row: dict) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_trade_engine.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/trade_engine.py tests/test_trade_engine.py
git commit -m "feat(copy_trade): TradeEngine - shadow/live fills, 2-of-cluster exit, -70% valve, journal"
```

---

### Task 11: `monitor.py` rewrite — scan loop v2 + config migration + dead-code removal

**Files:**
- Rewrite: `src/agent/copy_trade/monitor.py` (keep CLI shape: `--status`, `--scan`)
- Delete: `src/agent/copy_trade/swap_parser.py`, `src/agent/copy_trade/executor.py`, `tests/test_copy_trade_swap_parser.py`, `tests/test_copy_trade_executor.py`, `tests/fixtures/copy_trade_swap_samples.json`
- Modify: `data/copy_trade/config.json`
- Test: `tests/test_copy_trade_monitor.py` (rewrite)

**Interfaces:**
- Consumes: everything from Tasks 1, 6, 7, 8, 9, 10; `EmailNotifier.send_alert(subject, content)` (ctor raises `ValueError` when SMTP unconfigured); `settings.dry_run`/`settings.agent_private_key`; executors `OneInch/OpenOcean/PancakeSwap` (Pancake with `slippage_bps` from config).
- Produces: `run_scan(once: bool = False)`, `show_status()`, `main()`; helper `process_events(events, tracker, engine, store, notifier, token_meta_fn) -> None` kept module-level and pure-ish for tests.

**New `data/copy_trade/config.json`** (replace `copy_settings`, DELETE `auto_execute` and `target_wallets` — wallets now come from `wallets.json`):

```json
{
  "copy_settings": {
    "shadow_mode": true,
    "slice_usd": 3.0,
    "total_budget_usd": 16.14,
    "min_wallets": 3,
    "exit_wallets": 2,
    "window_minutes": 15,
    "valve_drop_pct": 0.70,
    "poll_interval_seconds": 45,
    "exec_slippage_bps": 1500,
    "rpc_endpoints": [
      "https://bsc-dataseed.binance.org",
      "https://bsc-dataseed1.defibit.io",
      "https://rpc.ankr.com/bsc"
    ],
    "ignore_tokens": [
      "0x55d398326f99059ff775485246999027b3197955",
      "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
      "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
      "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    ]
  }
}
```

(ignore_tokens = USDT, WBNB, USDC, BUSD on BSC.)

**run_scan v2 flow:**
1. Load config + `wallets.json` (list of `{"address", ...}`); abort with a clear message if `wallets.json` missing.
2. Build `RpcPool(cfg["rpc_endpoints"])`, `ChainEventSource(pool, wallet_addresses, start_block=pool.latest_block(), ignore_tokens=set(cfg["ignore_tokens"]))` — ALWAYS current latest block (backlog guard), `state.json` keeps only observability data (`last_scan_at`, `last_processed_block`).
3. Build budget (`total_budget_usd`/`slice_usd`), store: `shadow_positions.json` when `shadow_mode` else `positions.json`; reconcile like today (`register_discovered` + `budget.allocate()` per loaded position).
4. Executors only when `shadow_mode` is false (live): same construction as the old `_build_runtime` with `PancakeSwap(account=account, dry_run=settings.dry_run, slippage_bps=cfg["exec_slippage_bps"])`; in shadow mode pass `executors=None`.
5. `TradeEngine(budget, store, executors, shadow_mode, journal_path=ROOT/"data/copy_trade/closed_trades.jsonl", exit_wallets, valve_drop_pct, slice_usd)`.
6. Loop every `poll_interval_seconds`:
   - `events = source.poll()` inside try/except; `consecutive_failures += 1` on exception, reset on success; email once at 5 consecutive failures ("copy-trade data source down"), remember alerted-state so it emails once per outage.
   - `process_events(...)`: for each event — `"in"`: skip if `store.find_by_token(token)`; fetch `price = get_price_usd(token)`; `cluster = tracker.record(token, ev.wallet, time.time(), price)`; if cluster → resolve `symbol`/`decimals` via `token_meta_fn(token)` (eth_call `symbol()`/`decimals()` through the pool, fallback symbol = `token[:8]`, decimals 18) → `engine.open_cluster_position(...)` → email `[SHADOW] CLUSTER BUY <sym>` (or live subject) with wallets + tx links. `"out"`: `engine.on_exit_signal(ev.wallet, ev.token_address)` (engine emails via notifier callback — simplest: monitor checks store size before/after and emails on close; implement as: `closed = store.find_by_token(t) is None and was_open` pattern).
   - `engine.check_valve()` once per loop.
   - Sub-threshold cluster signals: log only, NO email (spec).
   - Persist `state.json` observability fields.
7. `show_status()`: wallets count from wallets.json, open positions (real + shadow separately), budget remaining, last scan, shadow_mode flag.

- [ ] **Step 1: Rewrite tests/test_copy_trade_monitor.py**

Delete the old Moralis-based tests entirely. New tests (all mocks, no network):

```python
# tests/test_copy_trade_monitor.py
"""Integration-ish tests for the v2 scan pipeline: events → cluster → engine.
The safety-critical assertions: 3 distinct-wallet buys open exactly ONE position;
a 4th buy on the same token opens nothing; shadow mode performs zero real calls."""
import time
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.chain_events import WalletEvent
from src.agent.copy_trade.cluster_signal import ClusterBuySignalTracker
from src.agent.copy_trade.monitor import process_events
from src.agent.copy_trade.positions import PositionStore
from src.agent.copy_trade.trade_engine import TradeEngine

T = "0x" + "a" * 40
W1, W2, W3, W4 = ("0x" + c * 40 for c in "1234")


def _ev(wallet, direction="in", token=T, block=1):
    return WalletEvent(wallet=wallet, token_address=token, direction=direction,
                       amount_raw=10 ** 18, tx_hash="0x" + "f" * 64, block=block)


def _pipeline(tmp_path):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    engine = TradeEngine(budget=budget, store=store, executors=None,
                         shadow_mode=True,
                         journal_path=tmp_path / "closed.jsonl")
    tracker = ClusterBuySignalTracker(min_wallets=3, window_minutes=15)
    return tracker, engine, store


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.02, 0.02))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_three_buys_open_exactly_one_shadow_position(_s, _mp, _ep, _t, tmp_path):
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1), _ev(W2)], tracker, engine, store, None, meta)
    assert store.all() == []                       # 2 of 3 — no trade
    process_events([_ev(W3)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1 and store.all()[0].simulated is True
    process_events([_ev(W4)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1                   # dup-token guard


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_out_events_route_to_exit_logic(_s, _mp, _ep, _t, tmp_path):
    tracker, engine, store = _pipeline(tmp_path)
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1), _ev(W2), _ev(W3)], tracker, engine, store, None, meta)
    assert len(store.all()) == 1
    process_events([_ev(W1, "out"), _ev(W2, "out")],
                   tracker, engine, store, None, meta)
    assert store.all() == []                       # 2-of-cluster exit fired


def test_wallets_json_required(tmp_path, monkeypatch):
    import src.agent.copy_trade.monitor as mon
    monkeypatch.setattr(mon, "WALLETS_PATH", tmp_path / "missing.json")
    try:
        mon._load_wallets()
        assert False, "should raise"
    except SystemExit:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_copy_trade_monitor.py -v`
Expected: FAIL — `process_events`/`_load_wallets`/`WALLETS_PATH` don't exist yet.

- [ ] **Step 3: Rewrite monitor.py**

Full new module — key parts (write the complete file; reuse `_load_json`, `_save_json`, `_ts`, `_log` helpers from the current file):

```python
# src/agent/copy_trade/monitor.py  (docstring updated to describe the v2 pipeline)
"""Copy-Trade Monitor v2 — cluster-gated, RPC-sourced, shadow-mode-first.

    python -m src.agent.copy_trade.monitor            # scan loop
    python -m src.agent.copy_trade.monitor --status
    python -m src.agent.copy_trade.monitor --scan     # one pass

Pipeline per scan: ChainEventSource.poll() → buy events feed the
ClusterBuySignalTracker (>=3 distinct wallets / 15 min); a firing cluster opens ONE
position via TradeEngine (paper when shadow_mode, real otherwise); out events feed
the 2-of-cluster exit rule; a -70% price valve runs every pass. Wallets come from
data/copy_trade/wallets.json (built by scripts/build_bsc_smart_wallets.py)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ..config import settings
from ..data.token_list import register_discovered
from ..email_notifier import EmailNotifier
from ..execution.oneinch import OneInch
from ..execution.openocean import OpenOcean
from ..execution.pancakeswap import PancakeSwap
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .chain_events import ChainEventSource, WalletEvent
from .cluster_signal import ClusterBuySignalTracker
from .positions import PositionStore
from .prices import get_price_usd
from .rpc_pool import RpcPool
from .trade_engine import TradeEngine

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"
WALLETS_PATH = ROOT / "data" / "copy_trade" / "wallets.json"
STATE_PATH = ROOT / "data" / "copy_trade" / "state.json"
POSITIONS_PATH = ROOT / "data" / "copy_trade" / "positions.json"
SHADOW_PATH = ROOT / "data" / "copy_trade" / "shadow_positions.json"
JOURNAL_PATH = ROOT / "data" / "copy_trade" / "closed_trades.jsonl"
FAILURE_ALERT_THRESHOLD = 5


def _load_wallets() -> list[str]:
    if not WALLETS_PATH.exists():
        print(f"FATAL: {WALLETS_PATH} missing — run scripts/build_bsc_smart_wallets.py first")
        raise SystemExit(1)
    return [w["address"] for w in json.loads(WALLETS_PATH.read_text(encoding="utf-8"))]


def _token_meta(pool: RpcPool, token_address: str) -> tuple[str, int]:
    """symbol()/decimals() via eth_call; graceful fallback for weird tokens."""
    def call(sig: str) -> str | None:
        try:
            return pool.call("eth_call", [{"to": token_address, "data": sig}, "latest"])
        except Exception:  # noqa: BLE001
            return None
    sym_raw = call("0x95d89b41")        # symbol()
    dec_raw = call("0x313ce567")        # decimals()
    symbol = token_address[:8]
    if sym_raw and len(sym_raw) > 130:
        try:
            n = int(sym_raw[66:130], 16)
            symbol = bytes.fromhex(sym_raw[130:130 + n * 2]).decode(
                "utf-8", errors="replace") or symbol
        except Exception:  # noqa: BLE001
            pass
    decimals = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else 18
    return symbol, decimals


def process_events(events: list[WalletEvent], tracker: ClusterBuySignalTracker,
                   engine: TradeEngine, store: PositionStore,
                   notifier: EmailNotifier | None,
                   token_meta_fn) -> None:
    for ev in events:
        if ev.direction == "out":
            was_open = store.find_by_token(ev.token_address) is not None
            engine.on_exit_signal(ev.wallet, ev.token_address)
            if was_open and store.find_by_token(ev.token_address) is None:
                _notify(notifier, f"[COPY-TRADE] CLOSED {ev.token_address[:10]}…",
                        f"closed by cluster exit rule; wallet {ev.wallet}\n"
                        f"tx https://bscscan.com/tx/{ev.tx_hash}")
            continue
        # direction == "in"
        if store.find_by_token(ev.token_address) is not None:
            continue   # already holding — never double-buy one token (spec §3)
        price = get_price_usd(ev.token_address)
        cluster = tracker.record(ev.token_address, ev.wallet, time.time(), price)
        if cluster is None:
            continue   # sub-threshold: log only, no email (spec §2)
        symbol, decimals = token_meta_fn(ev.token_address)
        opened = engine.open_cluster_position(ev.token_address, symbol, decimals,
                                              cluster)
        if opened:
            _notify(notifier,
                    f"[COPY-TRADE{' SHADOW' if engine._shadow else ''}] CLUSTER BUY {symbol}",
                    f"token {ev.token_address}\nwallets: {', '.join(cluster['wallets'])}\n"
                    f"first buy price: {cluster['first_price_usd']}\n"
                    f"trigger price: {price}\n"
                    f"tx https://bscscan.com/tx/{ev.tx_hash}")


def _notify(notifier, subject: str, body: str) -> None:
    if notifier is None:
        return
    try:
        notifier.send_alert(subject, body)
    except Exception:  # noqa: BLE001 — email must never kill the loop
        log.warning("notify_failed", subject=subject)


def _build_runtime(cfg: dict):
    shadow = cfg.get("shadow_mode", True)
    budget = CopyTradeBudget(total_usd=cfg.get("total_budget_usd", 16.14),
                             slice_usd=cfg.get("slice_usd", 3.0))
    store = PositionStore(SHADOW_PATH if shadow else POSITIONS_PATH)
    store.load()
    for p in store.all():   # reconcile after restart (C2+C3, now incl. v2 fields)
        register_discovered(p.token_symbol, p.token_address, p.token_decimals)
        if budget.can_open_new():
            budget.allocate()
    executors = None
    if not shadow:
        account = None
        if not settings.dry_run:
            from eth_account import Account
            account = Account.from_key(settings.agent_private_key)
        executors = {
            "1inch": OneInch(account=account, dry_run=settings.dry_run),
            "openocean": OpenOcean(account=account, dry_run=settings.dry_run),
            "pancake": PancakeSwap(account=account, dry_run=settings.dry_run,
                                   slippage_bps=cfg.get("exec_slippage_bps", 1500)),
        }
    engine = TradeEngine(budget=budget, store=store, executors=executors,
                         shadow_mode=shadow, journal_path=JOURNAL_PATH,
                         exit_wallets=cfg.get("exit_wallets", 2),
                         valve_drop_pct=cfg.get("valve_drop_pct", 0.70),
                         slice_usd=cfg.get("slice_usd", 3.0))
    return budget, store, engine


def run_scan(once: bool = False) -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["copy_settings"]
    wallets = _load_wallets()
    pool = RpcPool(cfg["rpc_endpoints"])
    source = ChainEventSource(pool, wallets, start_block=pool.latest_block(),
                              ignore_tokens=set(cfg.get("ignore_tokens", [])))
    budget, store, engine = _build_runtime(cfg)
    tracker = ClusterBuySignalTracker(min_wallets=cfg.get("min_wallets", 3),
                                      window_minutes=cfg.get("window_minutes", 15))
    try:
        notifier = EmailNotifier()
    except ValueError:
        notifier = None
    interval = cfg.get("poll_interval_seconds", 45)
    consecutive_failures, outage_alerted = 0, False
    mode = "SHADOW" if cfg.get("shadow_mode", True) else "LIVE"
    log.info("copy_trade_monitor_v2_started", wallets=len(wallets), mode=mode,
             start_block=source.last_processed)

    while True:
        try:
            events = source.poll()
            consecutive_failures, outage_alerted = 0, False
        except Exception as e:  # noqa: BLE001
            consecutive_failures += 1
            log.error("event_poll_failed", error=str(e), streak=consecutive_failures)
            if consecutive_failures >= FAILURE_ALERT_THRESHOLD and not outage_alerted:
                _notify(notifier, "[COPY-TRADE] data source DOWN",
                        f"{consecutive_failures} consecutive poll failures — "
                        f"monitor is blind until RPC recovers. Last error: {e}")
                outage_alerted = True
            events = []
        process_events(events, tracker, engine, store, notifier,
                       lambda a: _token_meta(pool, a))
        engine.check_valve()
        STATE_PATH.write_text(json.dumps({
            "last_scan_at": datetime.now(timezone.utc).isoformat(),
            "last_processed_block": source.last_processed}), encoding="utf-8")
        if once:
            break
        time.sleep(interval)


def show_status() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["copy_settings"]
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    for label, path in (("REAL", POSITIONS_PATH), ("SHADOW", SHADOW_PATH)):
        store = PositionStore(path)
        store.load()
        print(f"  {label} positions: {len(store.all())}")
        for p in store.all():
            print(f"    {p.token_symbol} ${p.usd_size} entry={p.entry_price_usd} "
                  f"exits={len(p.exited_by)}/{len(p.cluster_wallets)}")
    print(f"  shadow_mode: {cfg.get('shadow_mode')}")
    print(f"  last scan:   {state.get('last_scan_at', 'never')}")
    print(f"  last block:  {state.get('last_processed_block', '-')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy-Trade Monitor v2 (cluster+shadow)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if args.status:
        show_status()
    elif args.scan:
        run_scan(once=True)
    else:
        run_scan(once=False)


if __name__ == "__main__":
    main()
```

Then: `git rm src/agent/copy_trade/swap_parser.py src/agent/copy_trade/executor.py tests/test_copy_trade_swap_parser.py tests/test_copy_trade_executor.py tests/fixtures/copy_trade_swap_samples.json` and update `data/copy_trade/config.json` to the JSON above.

- [ ] **Step 4: Run the full copy-trade suite**

Run: `python -m pytest tests/ -k "copy_trade or cluster or chain_events or rpc_pool or trade_engine or prices or wallet_discovery or pancakeswap" -v`
Expected: all pass; deleted test files gone; `python -m src.agent.copy_trade.monitor --status` runs without crashing (prints status, works even with no wallets.json since status doesn't need it).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(copy_trade)!: monitor v2 - RPC events + cluster gate + shadow mode; drop Moralis path"
```

---

### Task 12: `scripts/shadow_report.py` — the go-live decision report

**Files:**
- Create: `scripts/shadow_report.py`
- Test: `tests/test_shadow_report.py`

**Interfaces:**
- Consumes: `closed_trades.jsonl` rows (Task 10 journal schema), open shadow positions via `PositionStore(SHADOW_PATH)`.
- Produces: `summarize(rows: list[dict]) -> dict` — `{"events", "closed", "wins", "win_rate", "total_pnl_usd", "median_pnl_pct", "avg_fees_usd"}`; CLI prints the table plus open positions and the reminder threshold (≥10 events → user decision time).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shadow_report.py
from scripts.shadow_report import summarize


def _row(pnl_usd, pnl_pct, fees=0.5, simulated=True):
    return {"simulated": simulated, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
            "fees_model_usd": fees}


def test_summarize_stats():
    rows = [_row(1.0, 0.5), _row(-0.5, -0.2), _row(2.0, 1.0)]
    s = summarize(rows)
    assert s["closed"] == 3 and s["wins"] == 2
    assert abs(s["win_rate"] - 2 / 3) < 1e-9
    assert s["total_pnl_usd"] == 2.5
    assert s["median_pnl_pct"] == 0.5


def test_summarize_only_counts_simulated():
    rows = [_row(1.0, 0.5), _row(9.0, 9.0, simulated=False)]
    assert summarize(rows)["closed"] == 1


def test_summarize_empty():
    s = summarize([])
    assert s["closed"] == 0 and s["win_rate"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shadow_report.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the implementation**

```python
# scripts/shadow_report.py
"""Shadow-mode PnL report — the input to the user's go-live decision (v2 spec:
>=10 cluster events, then the human decides; the bot never flips itself live).

Run: python scripts/shadow_report.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.copy_trade.positions import PositionStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "data" / "copy_trade" / "closed_trades.jsonl"
SHADOW = ROOT / "data" / "copy_trade" / "shadow_positions.json"
GO_LIVE_MIN_EVENTS = 10


def summarize(rows: list[dict]) -> dict:
    sim = [r for r in rows if r.get("simulated")]
    if not sim:
        return {"events": 0, "closed": 0, "wins": 0, "win_rate": None,
                "total_pnl_usd": 0.0, "median_pnl_pct": None, "avg_fees_usd": None}
    pnls = [r["pnl_usd"] for r in sim]
    pcts = [r["pnl_pct"] for r in sim if r.get("pnl_pct") is not None]
    wins = sum(1 for p in pnls if p > 0)
    return {"events": len(sim), "closed": len(sim), "wins": wins,
            "win_rate": wins / len(sim),
            "total_pnl_usd": round(sum(pnls), 4),
            "median_pnl_pct": statistics.median(pcts) if pcts else None,
            "avg_fees_usd": round(statistics.mean(
                r.get("fees_model_usd", 0) for r in sim), 4)}


def main() -> None:
    rows = []
    if JOURNAL.exists():
        rows = [json.loads(l) for l in
                JOURNAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    store = PositionStore(SHADOW)
    store.load()
    s = summarize(rows)
    total_events = s["closed"] + len(store.all())

    print("=" * 60)
    print("  SHADOW-MODE REPORT")
    print("=" * 60)
    print(f"  cluster events total : {total_events} "
          f"(closed {s['closed']}, open {len(store.all())})")
    if s["closed"]:
        print(f"  win rate             : {s['win_rate']:.0%}")
        print(f"  total paper PnL      : ${s['total_pnl_usd']}")
        print(f"  median PnL %         : {s['median_pnl_pct']:+.1%}")
        print(f"  avg modeled fees     : ${s['avg_fees_usd']}")
    for r in [r for r in rows if r.get("simulated")]:
        print(f"    {r['token_symbol']:<14} {r['pnl_pct']:+8.1%}  "
              f"${r['pnl_usd']:+7.2f}  exit={r['reason']}")
    for p in store.all():
        print(f"    {p.token_symbol:<14} OPEN  entry=${p.entry_price_usd:.6g}  "
              f"exits={len(p.exited_by)}/{len(p.cluster_wallets)}")
    print()
    if total_events >= GO_LIVE_MIN_EVENTS:
        print(f"  >= {GO_LIVE_MIN_EVENTS} events reached — time for the go-live decision.")
    else:
        print(f"  {GO_LIVE_MIN_EVENTS - total_events} more events until the "
              f"go-live review.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests + smoke**

Run: `python -m pytest tests/test_shadow_report.py -v` → 3 passed.
Run: `python scripts/shadow_report.py` → prints an empty report, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/shadow_report.py tests/test_shadow_report.py
git commit -m "feat(copy_trade): shadow_report - paper PnL summary for the go-live decision"
```

---

### Task 13: Merge, build the real wallet list, deploy shadow-mode to the VPS

This task is operational — do it WITH the user in the loop.

- [ ] **Step 1: Full test suite + branch review**

Run: `python -m pytest tests/ -v` — everything green.
Then invoke the `requesting-code-review` / whole-branch review flow (mandatory per the spec: this class of review caught 3 Critical bugs on the last branch, focus on restart-reconciliation of the new fields and the shadow-never-trades invariant). Fix findings, commit.

- [ ] **Step 2: Merge via finishing-a-development-branch**

Use the superpowers:finishing-a-development-branch skill (merge to `main`, clean up worktree).

- [ ] **Step 3: Build the real wallet list (LOCAL, with the user)**

Pick 5-10 recent BSC winner tokens with the user (DexScreener gainers, user approves the token picks), then:
`python scripts/build_bsc_smart_wallets.py --winners <addr...>`
Show the printed table to the user. **STOP for explicit approval of the 50-wallet list.** Iterate thresholds if the list looks wrong (e.g. too few early-buyer candidates → add winners).

- [ ] **Step 4: Deploy to the VPS**

```
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62
# on the VPS:
cd /home/agent/Track1-trade-onchain && sudo -u agent git pull
# copy wallets.json from local (scp), verify config.json has the new copy_settings
systemctl restart copy-trade && sleep 10
tail -30 logs/copy_trade.log
```

Verification checklist (all must hold before walking away):
- log shows `copy_trade_monitor_v2_started` with `mode=SHADOW`, `wallets=50`, a fresh `start_block`;
- `python -m src.agent.copy_trade.monitor --status` on the VPS shows `shadow_mode: True`, 0 real positions;
- wallet nonce on-chain does NOT change over the next hour (no real tx — `eth_getTransactionCount` for `0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa`);
- `state.json` `last_processed_block` advances between two checks ~1 min apart;
- no Moralis URLs appear in the new log output.

- [ ] **Step 5: Update the memory handoff**

Update `copy-trade-gem-hunter-handoff.md`: v2 deployed in shadow mode, wallets.json count, go-live gate = shadow_report ≥10 events + user decision.

---

## Self-Review Notes

- Spec §1 (RPC source, swap-confirmed buys, out-any-means exits, backlog guard, source-down alert) → Tasks 1, 6, 11. Spec §2 (tracker, no sub-threshold email) → Tasks 7, 11. Spec §3 (new fields, find_by_token, 2-of-cluster, valve, FoT sell fix, balance-delta fills) → Tasks 5, 8, 10. Spec §4 (shadow file separation, fee model, [SHADOW] emails, ≥10-events gate) → Tasks 10, 11, 12. Spec §5 ($3/16.14/5 slots) → config in Task 11. Part 1 (hybrid sources, filters, wallets.json format, manual local run) → Tasks 2, 3, 4. Deploy/verification → Task 13.
- Deliberate simplifications: shadow PnL uses DexScreener spot + a fixed fee model (`ponytail:` the model is validated against real fills only at go-live); ChainEventSource loses events during a restart gap (spec accepts: valve is the catastrophe backstop); BscScan txlist capped at 200 rows for tx_per_day estimation.
- Type-consistency check done: `cluster` dict shape (`wallets`/`first_ts`/`first_price_usd`) is identical in Tasks 7, 10, 11; `WalletEvent` fields identical in Tasks 6, 11; `SwapResult.received_out_wei` consumed in Task 10 via `getattr` fallback.
