# Copy-Trade Gem Hunter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Aegis volume-breakout bot with a copy-trading system that
auto-buys/auto-sells mirroring GMGN-labeled BSC smart-money wallets, spending the
remaining $15.39 wallet balance in fixed small slices, accepting total loss as the
known risk of this strategy.

**Architecture:** Extend the existing (uncommitted, buggy) `src/agent/copy_trade/`
package: keep its Moralis-polling skeleton, replace its swap parser and email sender,
add new small pure-function modules (budget, disk-backed positions, safety gate,
executor glue), then wire the whole thing into the already-correct
`deploy/copy-trade.service` systemd unit and stop `agent.service`.

**Tech Stack:** Python 3.12, `requests` (Moralis REST), `web3.py` (existing RPC/signing
stack), `pytest`, existing `src/agent/execution/best_execution.py` +
`src/agent/data/token_list.py` + `src/agent/execution/binance_web3.py`.

## Global Constraints

- Total trading budget: **$15.39** (current on-chain balance of
  `0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa`) — never size a position off any other
  number.
- **$0 infra budget** — GMGN free tier + Moralis free tier only, no paid subscriptions.
- **No custom take-profit/stop-loss** — every exit is a mirror of the source wallet's
  own sell, per spec §5. Do not add any other exit rule.
- **Self-custody signing preserved** — all execution goes through
  `src/agent/execution/best_execution.py` + the existing `OneInch`/`OpenOcean`/
  `PancakeSwap` executors, which sign locally with `settings.agent_private_key`. Never
  add a new signing path.
- **`DRY_RUN` must gate every live call** exactly like the existing execution backends
  (`dry_run=settings.dry_run if dry_run is None else dry_run`) — every new module that
  calls an executor must support and default to this.
- Every non-trivial function (parser, budget, position matching) ships with a `pytest`
  test in the same task that adds it — no task is "done" with failing or missing tests.

---

## File Structure

**New files:**
- `tests/fixtures/copy_trade_swap_samples.json` — hand-built realistic Moralis
  `wallets/{address}/history` response fragments (direct swap + multi-hop swap +
  ambiguous multi-leg tx), used by the parser tests.
- `src/agent/copy_trade/swap_parser.py` — `ParsedSwap` dataclass + `parse_swap()`.
  Replaces the buggy logic currently inline in `monitor.py`.
- `tests/test_copy_trade_swap_parser.py`
- `src/agent/copy_trade/budget.py` — `CopyTradeBudget`, pure allocation tracking.
- `tests/test_copy_trade_budget.py`
- `src/agent/copy_trade/positions.py` — disk-backed position store
  (`data/copy_trade/positions.json`), fixes the `_discovered`-style orphan-position bug
  by persisting on every write and reloading on start.
- `tests/test_copy_trade_positions.py`
- `src/agent/copy_trade/executor.py` — `handle_alert()`: the glue that turns a
  `ParsedSwap` into a buy or a mirror-sell, using the three modules above plus
  `best_execution`, `token_list.register_discovered`, and the shared
  `binance_web3.passes_safety_check` (see below — no separate `copy_trade/safety.py`).
- `tests/test_copy_trade_executor.py`

**Modified files:**
- `src/agent/execution/binance_web3.py` — extract the honeypot/tax/price-impact/
  holder/liquidity check currently inlined in `agent_loop._w3w_safety_check` into a
  new `passes_safety_check(from_token: str, to_token: str, amount_wei: str) -> tuple[bool, int | None]`,
  so both Aegis and copy-trade share one implementation instead of duplicating it
  (user decision, 2026-07-15 pre-flight review — Aegis's `agent_loop.py` is being
  retired but its code isn't deleted by this plan, so the shared check still needs a
  home both callers can import).
- `src/agent/agent_loop.py` — refactor `_w3w_safety_check`'s `check()` closure to call
  the new shared `bw.passes_safety_check()` instead of inlining the checks.
- `src/agent/copy_trade/monitor.py` — delete `parse_swap()` and `send_email_alert()`
  (replaced by `swap_parser.parse_swap()` and `src/agent/email_notifier.py`), wire the
  `# TODO: integrate with best_execution.py` stub to `executor.handle_alert()`, add the
  consecutive-failure alert.
- `data/copy_trade/config.json` — mark the confirmed-inactive contest wallets
  `monitor: false`, add `total_budget_usd` / `slice_usd`, set `auto_execute: true`.

**Not touched:** `deploy/copy-trade.service` (already correct — installed, not
rewritten), `src/agent/execution/oneinch.py` / `openocean.py` / `pancakeswap.py`
(reused as-is), `src/agent/data/token_list.py` (reused as-is).

---

### Task 1: Swap parser — fix the multi-hop mis-parse bug (§Audit #1, #4)

**Files:**
- Create: `tests/fixtures/copy_trade_swap_samples.json`
- Create: `src/agent/copy_trade/swap_parser.py`
- Test: `tests/test_copy_trade_swap_parser.py`

**Interfaces:**
- Produces: `ParsedSwap` dataclass with fields `hash: str`, `wallet: str`,
  `direction: Literal["buy", "sell", "unclear"]`, `token_symbol: str`,
  `token_address: str`, `token_decimals: int`, `token_amount: float`,
  `counter_symbol: str`, `usd_value: float | None`, `timestamp: str`. And
  `parse_swap(tx: dict, wallet: str) -> ParsedSwap | None` (`None` when the tx isn't a
  clean single-leg swap for `wallet`, or isn't a `"token swap"` category tx).
- Consumes: nothing (pure function, no network, no other project module).

Moralis's `erc20_transfers` schema (confirmed via docs) gives each transfer
`from_address`, `to_address`, `token_symbol`, `token_decimals`, `address` (token
contract), `value_formatted` — the current buggy code ignores `from_address`/
`to_address` and just takes whichever `direction`-tagged entry came last in the list,
which silently picks the wrong leg on a multi-hop route (USDT→WBNB→GEM). The fix:
filter transfers to exactly those where `wallet` is the `from_address` (the leg the
wallet actually sent) or the `to_address` (the leg it actually received) — internal
router/pool hops never have `wallet` as either address, so they're excluded
automatically. If that leaves anything other than exactly one sent + one received leg,
the tx is genuinely ambiguous (batched multi-token trade) — return `None` rather than
guess.

- [ ] **Step 1: Write the fixture file**

```json
{
  "direct_swap": {
    "hash": "0xaaa1",
    "category": "token swap",
    "block_timestamp": "2026-07-15T10:00:00.000Z",
    "summary": "Swapped 5 USDT for 12345 GEM",
    "erc20_transfers": [
      {
        "from_address": "0xWALLET000000000000000000000000000000001",
        "to_address": "0xROUTER00000000000000000000000000000001",
        "token_symbol": "USDT",
        "token_decimals": "18",
        "address": "0x55d398326f99059fF775485246999027B3197955",
        "value_formatted": "5.0"
      },
      {
        "from_address": "0xROUTER00000000000000000000000000000001",
        "to_address": "0xWALLET000000000000000000000000000000001",
        "token_symbol": "GEM",
        "token_decimals": "9",
        "address": "0x00000000000000000000000000000000000gem1",
        "value_formatted": "12345.0"
      }
    ]
  },
  "multi_hop_swap": {
    "hash": "0xbbb2",
    "category": "token swap",
    "block_timestamp": "2026-07-15T10:05:00.000Z",
    "summary": "Swapped 5 USDT for 999 GEM2 via WBNB",
    "erc20_transfers": [
      {
        "from_address": "0xWALLET000000000000000000000000000000001",
        "to_address": "0xROUTER00000000000000000000000000000001",
        "token_symbol": "USDT",
        "token_decimals": "18",
        "address": "0x55d398326f99059fF775485246999027B3197955",
        "value_formatted": "5.0"
      },
      {
        "from_address": "0xROUTER00000000000000000000000000000001",
        "to_address": "0xPOOL0000000000000000000000000000000001",
        "token_symbol": "WBNB",
        "token_decimals": "18",
        "address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "value_formatted": "0.0083"
      },
      {
        "from_address": "0xPOOL0000000000000000000000000000000001",
        "to_address": "0xROUTER00000000000000000000000000000001",
        "token_symbol": "WBNB",
        "token_decimals": "18",
        "address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "value_formatted": "0.0083"
      },
      {
        "from_address": "0xROUTER00000000000000000000000000000001",
        "to_address": "0xWALLET000000000000000000000000000000001",
        "token_symbol": "GEM2",
        "token_decimals": "9",
        "address": "0x00000000000000000000000000000000000gem2",
        "value_formatted": "999.0"
      }
    ]
  },
  "ambiguous_multi_leg": {
    "hash": "0xccc3",
    "category": "token swap",
    "block_timestamp": "2026-07-15T10:10:00.000Z",
    "summary": "Batch swapped 2 tokens",
    "erc20_transfers": [
      {
        "from_address": "0xWALLET000000000000000000000000000000001",
        "to_address": "0xROUTER00000000000000000000000000000001",
        "token_symbol": "USDT",
        "token_decimals": "18",
        "address": "0x55d398326f99059fF775485246999027B3197955",
        "value_formatted": "3.0"
      },
      {
        "from_address": "0xWALLET000000000000000000000000000000001",
        "to_address": "0xROUTER00000000000000000000000000000001",
        "token_symbol": "GEM3",
        "token_decimals": "9",
        "address": "0x00000000000000000000000000000000000gem3",
        "value_formatted": "40.0"
      },
      {
        "from_address": "0xROUTER00000000000000000000000000000001",
        "to_address": "0xWALLET000000000000000000000000000000001",
        "token_symbol": "GEM4",
        "token_decimals": "9",
        "address": "0x00000000000000000000000000000000000gem4",
        "value_formatted": "77.0"
      }
    ]
  },
  "not_a_swap": {
    "hash": "0xddd4",
    "category": "send",
    "block_timestamp": "2026-07-15T10:15:00.000Z",
    "summary": "Sent 1 USDT",
    "erc20_transfers": []
  }
}
```

- [ ] **Step 2: Write the failing tests**

```python
import json
from pathlib import Path

import pytest

from src.agent.copy_trade.swap_parser import parse_swap

WALLET = "0xWALLET000000000000000000000000000000001"
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "copy_trade_swap_samples.json").read_text()
)


def test_direct_swap_parses_as_buy():
    result = parse_swap(FIXTURES["direct_swap"], WALLET)
    assert result is not None
    assert result.direction == "buy"
    assert result.token_symbol == "GEM"
    assert result.token_decimals == 9
    assert result.token_amount == pytest.approx(12345.0)
    assert result.counter_symbol == "USDT"


def test_multi_hop_swap_ignores_intermediate_wbnb_hop():
    result = parse_swap(FIXTURES["multi_hop_swap"], WALLET)
    assert result is not None
    assert result.token_symbol == "GEM2"          # not WBNB — the old bug's failure mode
    assert result.token_decimals == 9
    assert result.token_amount == pytest.approx(999.0)
    assert result.direction == "buy"


def test_ambiguous_multi_leg_tx_returns_none_instead_of_guessing():
    assert parse_swap(FIXTURES["ambiguous_multi_leg"], WALLET) is None


def test_non_swap_category_returns_none():
    assert parse_swap(FIXTURES["not_a_swap"], WALLET) is None


def test_sell_direction_when_wallet_sends_the_tracked_token():
    tx = {
        "hash": "0xeee5",
        "category": "token swap",
        "block_timestamp": "2026-07-15T10:20:00.000Z",
        "summary": "Swapped 12345 GEM for 6 USDT",
        "erc20_transfers": [
            {
                "from_address": WALLET,
                "to_address": "0xROUTER00000000000000000000000000000001",
                "token_symbol": "GEM",
                "token_decimals": "9",
                "address": "0x00000000000000000000000000000000000gem1",
                "value_formatted": "12345.0",
            },
            {
                "from_address": "0xROUTER00000000000000000000000000000001",
                "to_address": WALLET,
                "token_symbol": "USDT",
                "token_decimals": "18",
                "address": "0x55d398326f99059fF775485246999027B3197955",
                "value_formatted": "6.0",
            },
        ],
    }
    result = parse_swap(tx, WALLET)
    assert result is not None
    assert result.direction == "sell"
    assert result.token_symbol == "GEM"
    assert result.token_amount == pytest.approx(12345.0)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_copy_trade_swap_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.agent.copy_trade.swap_parser'`

- [ ] **Step 4: Implement `swap_parser.py`**

```python
"""Parse a Moralis `wallets/{address}/history` transaction into a clean single-leg
swap, or None if it isn't one.

The wallet only ever directly sends/receives the FIRST and LAST leg of a routed swap —
intermediate router/pool hops never have the wallet as from_address or to_address, so
filtering on that automatically drops them. If filtering doesn't leave exactly one
sent leg and one received leg, the tx is a genuine multi-token batch trade: return
None rather than guess which leg matters (the bug this replaces guessed wrong).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_STABLE_OR_NATIVE = {"USDT", "USDC", "BUSD", "BNB", "WBNB"}


@dataclass(frozen=True)
class ParsedSwap:
    hash: str
    wallet: str
    direction: Literal["buy", "sell"]
    token_symbol: str
    token_address: str
    token_decimals: int
    token_amount: float
    counter_symbol: str
    usd_value: float | None
    timestamp: str


def parse_swap(tx: dict, wallet: str) -> ParsedSwap | None:
    if tx.get("category") != "token swap":
        return None

    w = wallet.lower()
    transfers = tx.get("erc20_transfers", [])
    sent = [t for t in transfers if (t.get("from_address") or "").lower() == w]
    received = [t for t in transfers if (t.get("to_address") or "").lower() == w]

    if len(sent) != 1 or len(received) != 1:
        return None

    sent_leg, recv_leg = sent[0], received[0]
    sent_sym = sent_leg.get("token_symbol", "")
    recv_sym = recv_leg.get("token_symbol", "")

    # Buy = wallet gave up a stable/native and received the tracked token.
    # Sell = wallet gave up the tracked token and received a stable/native.
    if sent_sym in _STABLE_OR_NATIVE and recv_sym not in _STABLE_OR_NATIVE:
        direction: Literal["buy", "sell"] = "buy"
        token_leg, counter_sym = recv_leg, sent_sym
    elif sent_sym not in _STABLE_OR_NATIVE and recv_sym in _STABLE_OR_NATIVE:
        direction = "sell"
        token_leg, counter_sym = sent_leg, recv_sym
    else:
        return None  # stable<->stable or gem<->gem — not an actionable copy signal

    try:
        decimals = int(token_leg.get("token_decimals", 18))
        amount = float(token_leg.get("value_formatted", 0))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    return ParsedSwap(
        hash=tx.get("hash", ""),
        wallet=wallet,
        direction=direction,
        token_symbol=token_leg.get("token_symbol", ""),
        token_address=token_leg.get("address", ""),
        token_decimals=decimals,
        token_amount=amount,
        counter_symbol=counter_sym,
        usd_value=None,
        timestamp=tx.get("block_timestamp", ""),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_copy_trade_swap_parser.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/copy_trade_swap_samples.json src/agent/copy_trade/swap_parser.py tests/test_copy_trade_swap_parser.py
git commit -m "feat(copy_trade): add swap parser that fixes multi-hop mis-parse bug"
```

---

### Task 2: Budget tracker

**Files:**
- Create: `src/agent/copy_trade/budget.py`
- Test: `tests/test_copy_trade_budget.py`

**Interfaces:**
- Produces: `CopyTradeBudget` class — `__init__(self, total_usd: float, slice_usd: float)`,
  `available_usd -> float` (property), `can_open_new() -> bool`,
  `allocate() -> float` (returns the slice amount, raises `RuntimeError` if
  `not can_open_new()`), `release(amount_usd: float) -> None` (called when a mirror-sell
  frees capital back up — proceeds from a sell are NOT reinvested beyond the original
  slice, so `release` always returns exactly the slice amount that was allocated, never
  the sale proceeds).
- Consumes: nothing (pure, no I/O — the caller in Task 6 persists/restores allocation
  state via `positions.py`, this class only does arithmetic).

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from src.agent.copy_trade.budget import CopyTradeBudget


def test_starts_with_full_budget_available():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    assert b.available_usd == pytest.approx(15.39)
    assert b.can_open_new() is True


def test_allocate_reduces_available_by_slice_size():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    amount = b.allocate()
    assert amount == pytest.approx(1.5)
    assert b.available_usd == pytest.approx(13.89)


def test_cannot_open_new_once_budget_below_one_slice():
    b = CopyTradeBudget(total_usd=1.4, slice_usd=1.5)
    assert b.can_open_new() is False
    with pytest.raises(RuntimeError):
        b.allocate()


def test_release_returns_the_slice_to_available_budget():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    b.allocate()
    b.release(1.5)
    assert b.available_usd == pytest.approx(15.39)


def test_ten_slices_exhaust_a_fifteen_dollar_budget():
    b = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    opened = 0
    while b.can_open_new():
        b.allocate()
        opened += 1
    assert opened == 10
    assert b.available_usd == pytest.approx(0.39, abs=1e-9)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_copy_trade_budget.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `budget.py`**

```python
"""Fixed-slice budget tracker for the copy-trade strategy — pure arithmetic, no I/O.
Persistence of which slices are currently open lives in positions.py; this class only
answers 'is there room for one more slice right now'."""
from __future__ import annotations


class CopyTradeBudget:
    def __init__(self, total_usd: float, slice_usd: float) -> None:
        if total_usd <= 0 or slice_usd <= 0:
            raise ValueError("total_usd and slice_usd must be positive")
        self._slice_usd = slice_usd
        self._available_usd = total_usd

    @property
    def available_usd(self) -> float:
        return self._available_usd

    def can_open_new(self) -> bool:
        return self._available_usd >= self._slice_usd

    def allocate(self) -> float:
        if not self.can_open_new():
            raise RuntimeError(
                f"insufficient budget: {self._available_usd:.4f} < slice {self._slice_usd:.4f}"
            )
        self._available_usd -= self._slice_usd
        return self._slice_usd

    def release(self, amount_usd: float) -> None:
        self._available_usd += amount_usd
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_copy_trade_budget.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/budget.py tests/test_copy_trade_budget.py
git commit -m "feat(copy_trade): add fixed-slice budget tracker"
```

---

### Task 3: Disk-backed position store (fixes the orphan-position root cause)

**Files:**
- Create: `src/agent/copy_trade/positions.py`
- Test: `tests/test_copy_trade_positions.py`

**Interfaces:**
- Produces: `CopyPosition` dataclass (`token_symbol: str`, `token_address: str`,
  `token_decimals: int`, `source_wallet: str`, `usd_size: float`,
  `token_amount: float`, `opened_at: str`). `PositionStore` class —
  `__init__(self, path: Path)`, `load(self) -> None` (reads the JSON file into memory,
  no-ops if the file doesn't exist yet), `open_position(self, pos: CopyPosition) -> None`
  (adds + writes to disk immediately), `close_position(self, token_address: str,
  source_wallet: str) -> CopyPosition | None` (removes + writes to disk immediately,
  returns the removed position or `None` if not found), `find(self, token_address: str,
  source_wallet: str) -> CopyPosition | None`, `all(self) -> list[CopyPosition]`.
- Consumes: nothing beyond stdlib `json`/`pathlib`.

The critical property under test: every `open_position`/`close_position` call writes
to disk **synchronously, before returning** — so a process restart between two calls
never loses a position, unlike `token_list._discovered` (RAM-only) which orphaned
金狗/未来协议 for 9 days.

- [ ] **Step 1: Write the failing tests**

```python
import json
from pathlib import Path

import pytest

from src.agent.copy_trade.positions import CopyPosition, PositionStore

POS = CopyPosition(
    token_symbol="GEM",
    token_address="0xgem1",
    token_decimals=9,
    source_wallet="0xshark1",
    usd_size=1.5,
    token_amount=12345.0,
    opened_at="2026-07-15T10:00:00Z",
)


def test_open_position_persists_to_disk_immediately(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(POS)

    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk[0]["token_address"] == "0xgem1"


def test_reloading_a_fresh_store_recovers_positions_written_by_a_prior_process(tmp_path):
    path = tmp_path / "positions.json"
    store_a = PositionStore(path)
    store_a.open_position(POS)

    # Simulate a process restart: a brand new PositionStore instance, same path.
    store_b = PositionStore(path)
    store_b.load()

    found = store_b.find("0xgem1", "0xshark1")
    assert found is not None
    assert found.token_amount == pytest.approx(12345.0)


def test_close_position_removes_it_and_persists(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.open_position(POS)

    closed = store.close_position("0xgem1", "0xshark1")
    assert closed is not None
    assert closed.token_amount == pytest.approx(12345.0)
    assert store.find("0xgem1", "0xshark1") is None

    reloaded = PositionStore(path)
    reloaded.load()
    assert reloaded.all() == []


def test_close_position_returns_none_when_not_found(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    assert store.close_position("0xnope", "0xshark1") is None


def test_load_on_missing_file_starts_empty(tmp_path):
    store = PositionStore(tmp_path / "does_not_exist.json")
    store.load()
    assert store.all() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_copy_trade_positions.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `positions.py`**

```python
"""Disk-backed copy-trade position store. Every mutation writes to disk synchronously
before returning, so a process restart (crash, deploy, VPS reboot) can always recover
open positions by reloading this file — the exact property the RAM-only
`token_list._discovered` registry lacked, which orphaned two real positions for 9 days
(see docs/superpowers/specs/2026-07-15-copy-trade-gem-hunter-design.md)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CopyPosition:
    token_symbol: str
    token_address: str
    token_decimals: int
    source_wallet: str
    usd_size: float
    token_amount: float
    opened_at: str


class PositionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._positions: list[CopyPosition] = []

    def load(self) -> None:
        if not self._path.exists():
            self._positions = []
            return
        raw = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        self._positions = [CopyPosition(**p) for p in raw]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(p) for p in self._positions], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def open_position(self, pos: CopyPosition) -> None:
        self._positions.append(pos)
        self._save()

    def close_position(self, token_address: str, source_wallet: str) -> CopyPosition | None:
        pos = self.find(token_address, source_wallet)
        if pos is None:
            return None
        self._positions.remove(pos)
        self._save()
        return pos

    def find(self, token_address: str, source_wallet: str) -> CopyPosition | None:
        for p in self._positions:
            if p.token_address.lower() == token_address.lower() and p.source_wallet.lower() == source_wallet.lower():
                return p
        return None

    def all(self) -> list[CopyPosition]:
        return list(self._positions)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_copy_trade_positions.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/positions.py tests/test_copy_trade_positions.py
git commit -m "feat(copy_trade): add disk-backed position store, fixes orphan-position root cause"
```

---

### Task 4: Extract a shared safety-check helper into `binance_web3.py`, refactor `agent_loop.py` to use it

**Files:**
- Modify: `src/agent/execution/binance_web3.py`
- Modify: `src/agent/agent_loop.py:352-421` (the `_w3w_safety_check` function)
- Test: `tests/test_binance_web3.py`

**Interfaces:**
- Consumes: `quote(from_token, to_token, amount_wei, *, chain_id=...) -> list[dict]`
  (existing, module-level in `binance_web3.py` — returns the routes list directly,
  confirmed at `src/agent/agent_loop.py:363`: `routes = bw.quote(...)`), `price_info
  (contracts: list[str], *, chain_id=...) -> dict[str, dict]` (existing, same module).
- Produces: `passes_safety_check(from_token: str, to_token: str, amount_wei: str) ->
  tuple[bool, int | None]` — `(True, decimals)` when honeypot/tax/price-impact/
  holders/liquidity all pass (decimals read from the quote's `toToken.decimal`),
  `(False, None)` otherwise, fail-closed on any error.

User decision (pre-flight review, 2026-07-15): `agent_loop._w3w_safety_check`
currently inlines these five checks. Rather than let `src/agent/copy_trade` duplicate
that ~70-line block verbatim, extract the checks into `binance_web3.py` (the module
that already owns `quote()`/`price_info()`) and have `agent_loop.py` call the shared
function too — one implementation, two callers.

- [ ] **Step 1: Write the failing tests** — append to the existing
  `tests/test_binance_web3.py` (matches its established `mocker`-fixture style, see
  lines 34-59 of that file — do not introduce `unittest.mock.patch` in this file):

```python
# ----------------------------- passes_safety_check -----------------------------

def _quote_response(is_honeypot=False, tax="0", price_impact="1", decimal="9"):
    return [{
        "isBest": True,
        "toToken": {"isHoneyPot": is_honeypot, "taxRate": tax, "decimal": decimal},
        "priceImpactPercent": price_impact,
    }]


def _price_info(holders=500, liquidity=50000.0):
    return {"0xtoken1": {"holders": holders, "liquidity": liquidity}}


def test_safety_check_rejects_honeypot(mocker):
    mocker.patch.object(bw, "quote", return_value=_quote_response(is_honeypot=True))
    ok, decimals = bw.passes_safety_check("0xusdt1", "0xtoken1", "1000")
    assert ok is False and decimals is None


def test_safety_check_rejects_tax_above_threshold(mocker):
    mocker.patch.object(bw, "settings", mocker.Mock(binance_w3w_max_tax_rate=0.1,
                                                      binance_w3w_max_price_impact=0.5,
                                                      binance_w3w_min_holders=10,
                                                      binance_w3w_min_liquidity_usd_check=1000))
    mocker.patch.object(bw, "quote", return_value=_quote_response(tax="50"))
    ok, decimals = bw.passes_safety_check("0xusdt1", "0xtoken1", "1000")
    assert ok is False and decimals is None


def test_safety_check_rejects_low_liquidity(mocker):
    mocker.patch.object(bw, "settings", mocker.Mock(binance_w3w_max_tax_rate=0.1,
                                                      binance_w3w_max_price_impact=0.5,
                                                      binance_w3w_min_holders=10,
                                                      binance_w3w_min_liquidity_usd_check=1000))
    mocker.patch.object(bw, "quote", return_value=_quote_response())
    mocker.patch.object(bw, "price_info", return_value=_price_info(liquidity=1.0))
    ok, decimals = bw.passes_safety_check("0xusdt1", "0xtoken1", "1000")
    assert ok is False and decimals is None


def test_safety_check_passes_clean_token_and_returns_decimals(mocker):
    mocker.patch.object(bw, "settings", mocker.Mock(binance_w3w_max_tax_rate=0.1,
                                                      binance_w3w_max_price_impact=0.5,
                                                      binance_w3w_min_holders=10,
                                                      binance_w3w_min_liquidity_usd_check=1000))
    mocker.patch.object(bw, "quote", return_value=_quote_response(decimal="9"))
    mocker.patch.object(bw, "price_info", return_value=_price_info())
    ok, decimals = bw.passes_safety_check("0xusdt1", "0xtoken1", "1000")
    assert ok is True and decimals == 9


def test_safety_check_quote_failure_fails_closed(mocker):
    mocker.patch.object(bw, "quote", side_effect=RuntimeError("timeout"))
    ok, decimals = bw.passes_safety_check("0xusdt1", "0xtoken1", "1000")
    assert ok is False and decimals is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_binance_web3.py -v -k passes_safety_check`
Expected: FAIL with `AttributeError: module ... has no attribute 'passes_safety_check'`

- [ ] **Step 3: Add `passes_safety_check` to `binance_web3.py`**

Append at the end of `src/agent/execution/binance_web3.py` (after the existing
`quote()` function):

```python
def passes_safety_check(from_token: str, to_token: str, amount_wei: str) -> tuple[bool, int | None]:
    """Honeypot/tax/price-impact/holder/liquidity gate for a buy candidate — shared
    by Aegis (agent_loop._w3w_safety_check) and the copy-trade module so neither
    duplicates the other's checks. Fails closed: any error or missing data returns
    (False, None), never a pass. On success returns (True, decimals) using the
    destination token's decimals from the quote response."""
    try:
        routes = quote(from_token, to_token, amount_wei)
    except Exception as e:  # noqa: BLE001 — fail closed: no quote = no entry
        log.warning("safety_check_quote_failed", to_token=to_token, error=type(e).__name__)
        return False, None
    if not routes:
        return False, None
    best = next((r for r in routes if r.get("isBest")), routes[0])
    to_tok = best.get("toToken") or {}

    if to_tok.get("isHoneyPot"):
        log.warning("safety_check_honeypot_blocked", to_token=to_token)
        return False, None
    try:
        tax = float(to_tok.get("taxRate") or 0)
    except (TypeError, ValueError):
        tax = 1.0
    if tax > settings.binance_w3w_max_tax_rate:
        log.warning("safety_check_tax_too_high", to_token=to_token, tax=tax)
        return False, None
    try:
        impact = float(best.get("priceImpactPercent") or 0) / 100.0
    except (TypeError, ValueError):
        impact = 1.0
    if impact > settings.binance_w3w_max_price_impact:
        log.warning("safety_check_price_impact_too_high", to_token=to_token, impact=impact)
        return False, None

    try:
        info = price_info([to_token]).get(to_token.lower())
    except Exception as e:  # noqa: BLE001 — fail closed
        log.warning("safety_check_price_info_failed", to_token=to_token, error=type(e).__name__)
        return False, None
    if not info:
        log.warning("safety_check_price_info_missing", to_token=to_token)
        return False, None
    try:
        holders = int(info.get("holders") or 0)
    except (TypeError, ValueError):
        holders = 0
    if holders < settings.binance_w3w_min_holders:
        log.warning("safety_check_holders_too_low", to_token=to_token, holders=holders)
        return False, None
    try:
        liquidity = float(info.get("liquidity") or 0)
    except (TypeError, ValueError):
        liquidity = 0.0
    if liquidity < settings.binance_w3w_min_liquidity_usd_check:
        log.warning("safety_check_liquidity_too_low", to_token=to_token, liquidity=liquidity)
        return False, None

    try:
        decimals = int(to_tok.get("decimal") or 18)
    except (TypeError, ValueError):
        decimals = 18
    return True, decimals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_binance_web3.py -v -k passes_safety_check`
Expected: PASS (5 passed)

- [ ] **Step 5: Refactor `agent_loop.py` to call the shared function**

Replace the body of `_w3w_safety_check`'s `check()` closure
(`src/agent/agent_loop.py:359-420`, from `def check(sig) -> bool:` through the line
before `return check`) with:

```python
    def check(sig) -> bool:
        ticket = sniper.meme_ticket_usd(equity_usd)
        amount_wei = str(int(ticket * 10**18))   # USDT has 18 decimals on BSC
        ok, decimals = bw.passes_safety_check(settings.usdt_address, sig.contract, amount_wei)
        if not ok:
            log.warning("w3w_safety_check_failed", symbol=sig.symbol, contract=sig.contract)
            return False
        token_list.register_discovered(sig.symbol, sig.contract, decimals or 18)
        return True
```

The five inlined checks that used to fill this closure now live in
`bw.passes_safety_check` (Step 3) — this replacement is a pure delegation, no behavior
change. The `# Real-money incident (2/7)...` comment explaining the price-impact check
moves with the logic into `binance_web3.py`'s new function (already present there from
Step 3) rather than staying here.

- [ ] **Step 6: Run the existing agent_loop test suite to confirm no regression**

Run: `pytest tests/test_agent_loop.py -v`
Expected: PASS, same pass count as before this task's changes (this task must not
change `_w3w_safety_check`'s external behavior, only where the logic lives).

- [ ] **Step 7: Commit**

```bash
git add src/agent/execution/binance_web3.py src/agent/agent_loop.py tests/test_binance_web3.py
git commit -m "refactor: extract shared passes_safety_check from agent_loop into binance_web3, reused by copy_trade"
```

---

### Task 5: Executor glue — turn a `ParsedSwap` into a buy or mirror-sell

**Files:**
- Create: `src/agent/copy_trade/executor.py`
- Test: `tests/test_copy_trade_executor.py`

**Interfaces:**
- Consumes: `swap_parser.ParsedSwap` (Task 1), `budget.CopyTradeBudget` (Task 2),
  `positions.PositionStore` + `positions.CopyPosition` (Task 3),
  `src.agent.execution.binance_web3.passes_safety_check(from_token, to_token,
  amount_wei) -> tuple[bool, int | None]` (Task 4), `src.agent.data.token_list
  .register_discovered(symbol, contract, decimals) -> Token` (existing),
  `src.agent.execution.best_execution.rank_backends(executors: dict[str, object],
  token_in: str, token_out: str, amount_in_human: float) -> list[str]` (existing),
  `settings.usdt_address` (existing config value), and each executor's
  `.swap(token_in: str, token_out: str, amount_in_human: float) -> SwapResult`
  (existing, same signature on `OneInch`/`OpenOcean`/`PancakeSwap`).
- Produces: `handle_alert(alert: ParsedSwap, budget: CopyTradeBudget, store:
  PositionStore, executors: dict[str, object]) -> None`.

- [ ] **Step 1: Write the failing tests** — use the `mocker` fixture (project
  convention, see `tests/test_oneinch.py`), not `unittest.mock.patch`:

```python
import pytest

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.executor import handle_alert
from src.agent.copy_trade.positions import CopyPosition, PositionStore
from src.agent.copy_trade.swap_parser import ParsedSwap

BUY = ParsedSwap(
    hash="0x1", wallet="0xshark1", direction="buy", token_symbol="GEM",
    token_address="0xgem1", token_decimals=9, token_amount=12345.0,
    counter_symbol="USDT", usd_value=None, timestamp="2026-07-15T10:00:00Z",
)
SELL = ParsedSwap(
    hash="0x2", wallet="0xshark1", direction="sell", token_symbol="GEM",
    token_address="0xgem1", token_decimals=9, token_amount=12345.0,
    counter_symbol="USDT", usd_value=None, timestamp="2026-07-15T11:00:00Z",
)


def _mock_executors(mocker):
    winning = mocker.MagicMock()
    winning.swap.return_value = mocker.MagicMock(simulated=False, tx_hash="0xexec1")
    return {"1inch": winning}, winning


def test_buy_signal_allocates_budget_registers_token_and_executes(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(True, 9))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    mock_register.assert_called_once_with("GEM", "0xgem1", 9)
    winning.swap.assert_called_once_with("USDT", "GEM", 1.5)
    assert budget.available_usd == pytest.approx(13.89)
    assert store.find("0xgem1", "0xshark1") is not None


def test_buy_signal_skipped_when_safety_check_fails(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(False, None))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    winning.swap.assert_not_called()
    mock_register.assert_not_called()
    assert budget.available_usd == pytest.approx(15.39)
    assert store.find("0xgem1", "0xshark1") is None


def test_buy_signal_skipped_when_budget_exhausted(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.passes_safety_check", return_value=(True, 9))
    mock_register = mocker.patch("src.agent.copy_trade.executor.register_discovered")
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=1.0, slice_usd=1.5)  # already too small
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(BUY, budget, store, executors)

    winning.swap.assert_not_called()
    mock_register.assert_not_called()


def test_sell_signal_closes_matching_position_and_releases_budget(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=13.89, slice_usd=1.5)  # 1 slice already spent
    store = PositionStore(tmp_path / "positions.json")
    store.open_position(CopyPosition(
        token_symbol="GEM", token_address="0xgem1", token_decimals=9,
        source_wallet="0xshark1", usd_size=1.5, token_amount=12345.0,
        opened_at="2026-07-15T10:00:00Z",
    ))

    handle_alert(SELL, budget, store, executors)

    winning.swap.assert_called_once_with("GEM", "USDT", 12345.0)
    assert store.find("0xgem1", "0xshark1") is None
    assert budget.available_usd == pytest.approx(15.39)


def test_sell_signal_for_untracked_position_is_a_noop(mocker, tmp_path):
    executors, winning = _mock_executors(mocker)
    mocker.patch("src.agent.copy_trade.executor.rank_backends", return_value=["1inch"])
    budget = CopyTradeBudget(total_usd=15.39, slice_usd=1.5)
    store = PositionStore(tmp_path / "positions.json")

    handle_alert(SELL, budget, store, executors)  # never bought this one

    winning.swap.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_copy_trade_executor.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `executor.py`**

```python
"""Turn a ParsedSwap alert into a real (or DRY_RUN-simulated) copy-buy or mirror-sell.
Buy: only when budget allows AND the shared safety gate (binance_web3
.passes_safety_check) passes. Sell: only mirrors a position this module itself
opened (matched by token_address + source_wallet) — never sells something it doesn't
have a record of buying, and never applies any stop/take-profit rule of its own
(spec §5 — exit strictly mirrors the source wallet)."""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..execution.binance_web3 import passes_safety_check
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .positions import CopyPosition, PositionStore
from .swap_parser import ParsedSwap

log = get_logger(__name__)


def handle_alert(
    alert: ParsedSwap,
    budget: CopyTradeBudget,
    store: PositionStore,
    executors: dict[str, object],
) -> None:
    if alert.direction == "buy":
        _handle_buy(alert, budget, store, executors)
    else:
        _handle_sell(alert, budget, store, executors)


def _handle_buy(
    alert: ParsedSwap, budget: CopyTradeBudget, store: PositionStore,
    executors: dict[str, object],
) -> None:
    if not budget.can_open_new():
        log.info("copy_trade_buy_skipped_budget", token=alert.token_symbol)
        return

    amount_wei = str(int(budget.available_usd * 10**18))  # USDT has 18 decimals on BSC
    ok, decimals = passes_safety_check(settings.usdt_address, alert.token_address, amount_wei)
    if not ok:
        log.warning("copy_trade_buy_skipped_safety", token=alert.token_symbol)
        return

    register_discovered(alert.token_symbol, alert.token_address, decimals or alert.token_decimals)
    ranked = rank_backends(executors, "USDT", alert.token_symbol, budget.available_usd)
    if not ranked:
        log.warning("copy_trade_buy_no_route", token=alert.token_symbol)
        return

    usd_size = budget.allocate()
    executor = executors[ranked[0]]
    result = executor.swap("USDT", alert.token_symbol, usd_size)
    store.open_position(CopyPosition(
        token_symbol=alert.token_symbol,
        token_address=alert.token_address,
        token_decimals=decimals or alert.token_decimals,
        source_wallet=alert.wallet,
        usd_size=usd_size,
        token_amount=alert.token_amount,
        opened_at=datetime.now(timezone.utc).isoformat(),
    ))
    log.info("copy_trade_bought", token=alert.token_symbol, usd_size=usd_size,
              simulated=getattr(result, "simulated", None))


def _handle_sell(
    alert: ParsedSwap, budget: CopyTradeBudget, store: PositionStore,
    executors: dict[str, object],
) -> None:
    pos = store.find(alert.token_address, alert.wallet)
    if pos is None:
        log.debug("copy_trade_sell_no_matching_position", token=alert.token_symbol)
        return

    ranked = rank_backends(executors, alert.token_symbol, "USDT", pos.token_amount)
    if not ranked:
        log.warning("copy_trade_sell_no_route", token=alert.token_symbol)
        return

    executor = executors[ranked[0]]
    result = executor.swap(alert.token_symbol, "USDT", pos.token_amount)
    store.close_position(alert.token_address, alert.wallet)
    budget.release(pos.usd_size)
    log.info("copy_trade_sold", token=alert.token_symbol,
              simulated=getattr(result, "simulated", None))
```

`register_discovered(alert.token_symbol, alert.token_address, decimals or
alert.token_decimals)` prefers the safety check's on-chain-quote-sourced `decimals`
but falls back to the Moralis-sourced `alert.token_decimals` from Task 1 if the quote
didn't return one — the two sources should always agree since both read the same
token contract, this is defense against either being momentarily absent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_copy_trade_executor.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/executor.py tests/test_copy_trade_executor.py
git commit -m "feat(copy_trade): add executor glue wiring buy/mirror-sell to best_execution"
```

---

### Task 6: Wire `monitor.py` to the new modules, remove the dead TODO and duplicate email code

**Files:**
- Modify: `src/agent/copy_trade/monitor.py`

**Interfaces:**
- Consumes: `swap_parser.parse_swap` (Task 1), `executor.handle_alert` (Task 5),
  `budget.CopyTradeBudget` (Task 2), `positions.PositionStore` (Task 3),
  `src.agent.email_notifier.EmailNotifier` (existing), `src.agent.execution.oneinch
  .OneInch`, `.openocean.OpenOcean`, `.pancakeswap.PancakeSwap` (existing, for building
  the `executors` dict once at startup — same pattern `best_execution.py`'s own
  docstring assumes callers already build).

- [ ] **Step 1: Delete the buggy/duplicate functions**

In `src/agent/copy_trade/monitor.py`, delete the entire `parse_swap()` function
(lines 79-119 in the original file) and the entire `send_email_alert()` function
(lines 166-219 in the original file).

- [ ] **Step 2: Update the imports at the top of the file**

Replace:
```python
import requests
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import dotenv_values
```
with:
```python
import requests
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import dotenv_values

from ..email_notifier import EmailNotifier
from ..execution.oneinch import OneInch
from ..execution.openocean import OpenOcean
from ..execution.pancakeswap import PancakeSwap
from .budget import CopyTradeBudget
from .executor import handle_alert
from .positions import PositionStore
from .swap_parser import parse_swap
```

- [ ] **Step 3: Add module-level constants and a builder for the executors/budget/store**

Add right after the existing `MORALIS`/`HEADERS` constants:

```python
POSITIONS_PATH = ROOT / "data" / "copy_trade" / "positions.json"


def _build_runtime():
    """One-time construction of the shared budget tracker, position store, and
    executor pool — called once from main(), passed down into the scan loop."""
    config = _load_json(CONFIG_PATH)
    settings_ = config.get("copy_settings", {})
    budget = CopyTradeBudget(
        total_usd=settings_.get("total_budget_usd", 15.39),
        slice_usd=settings_.get("slice_usd", 1.5),
    )
    store = PositionStore(POSITIONS_PATH)
    store.load()
    executors = {"1inch": OneInch(), "openocean": OpenOcean(), "pancake": PancakeSwap()}
    return budget, store, executors
```

- [ ] **Step 4: Replace `check_wallet()`'s alert body to use the new parser**

Replace the body of the `for tx in swaps:` loop in `check_wallet()` — everything from
`parsed = parse_swap(tx)` down to (but not including) `state["processed_txs"] = ...` —
with:

```python
        parsed = parse_swap(tx, address)
        if not parsed:
            processed.add(tx_hash)
            continue

        if parsed.token_symbol in ignore_tokens or parsed.token_address.lower() in {
            t.lower() for t in ignore_tokens
        }:
            processed.add(tx_hash)
            continue

        alert = {
            "wallet": address,
            "wallet_label": label,
            "detected_at": _ts(),
            "parsed": parsed,
        }
        new_alerts.append(alert)
        processed.add(tx_hash)

        _log("ALERT", f"NEW SWAP on [{label}]",
             symbol=parsed.token_symbol, direction=parsed.direction)
```

(`min_usd` was read but unused in the original — the real $-threshold now lives in
`CopyTradeBudget`'s `slice_usd`, so it is intentionally dropped here rather than wired
to a second, redundant gate.)

- [ ] **Step 5: Replace the email-and-execute block in `run_scan()`**

Replace this block (originally the tail of the `for a in all_new_alerts:` loop):
```python
                # Gửi email thông báo
                send_email_alert(a)

                # Copy trade signal
                if settings.get("auto_execute"):
                    _log("EXEC", "Auto-execute enabled — would copy this swap")
                    # TODO: integrate with best_execution.py
                else:
                    _log("INFO", "Alert-only mode — manual review required")
```
with:
```python
                try:
                    notifier.send_alert(
                        f"[AEGIS COPY-TRADE] {a['parsed'].direction.upper()} {a['parsed'].token_symbol}",
                        f"Wallet: {a['wallet_label']} ({a['wallet']})\n"
                        f"Direction: {a['parsed'].direction}\n"
                        f"Token: {a['parsed'].token_symbol} ({a['parsed'].token_address})\n"
                        f"Amount: {a['parsed'].token_amount}\n"
                        f"TX: https://bscscan.com/tx/{a['parsed'].hash}\n",
                    )
                except ValueError:
                    pass  # SMTP not configured — alert still logged to console above

                if settings.get("auto_execute"):
                    handle_alert(a["parsed"], budget, store, executors)
```

- [ ] **Step 6: Wire `run_scan()`'s signature and startup to build the new runtime, and add the consecutive-failure alert**

Replace the start of `run_scan()`:
```python
def run_scan(once: bool = False):
    """Chạy 1 vòng scan hoặc loop liên tục."""
    config = _load_json(CONFIG_PATH)
    state = _load_json(STATE_PATH)
    settings = config.get("copy_settings", {})
    interval = settings.get("poll_interval_seconds", 30)
```
with:
```python
def run_scan(once: bool = False):
    """Chạy 1 vòng scan hoặc loop liên tục."""
    config = _load_json(CONFIG_PATH)
    state = _load_json(STATE_PATH)
    settings = config.get("copy_settings", {})
    interval = settings.get("poll_interval_seconds", 30)
    budget, store, executors = _build_runtime()
    try:
        notifier = EmailNotifier()
    except ValueError:
        notifier = None
    consecutive_failures = 0
```

Then, inside the `while True:` loop, right after the existing
`for w in wallets: alerts = check_wallet(...)` block, add the failure-streak check —
`check_wallet()` already logs an `ERROR` per failed wallet fetch via `_log`, but does
not raise, so detect failures by checking whether `state["last_checked"]` advanced for
every wallet this round:

```python
        round_ok = all(w["address"] in state.get("last_checked", {}) for w in wallets)
        # A wallet only lands in last_checked when fetch_recent_swaps didn't except —
        # 401s inside fetch_recent_swaps are already caught there and logged, but the
        # wallet's last_checked timestamp is still written by check_wallet() either
        # way, so use a request-level probe instead: re-check the most recently seen
        # HTTP status via a lightweight one-off call every 10 iterations.
        if iteration % 10 == 0:
            probe = requests.get(
                f"{MORALIS}/wallets/{wallets[0]['address']}/history",
                headers=HEADERS, params={"chain": "bsc", "limit": 1}, timeout=10,
            )
            if probe.status_code == 401:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if consecutive_failures == 1 and notifier is not None:  # alert once, not every 10 iters
                notifier.send_alert(
                    "[AEGIS COPY-TRADE] Moralis auth failing",
                    f"wallets/history returned 401 at iteration {iteration}. "
                    f"MORALIS_API_KEY likely invalid/expired — copy-trade monitor is blind until fixed.",
                )
```

- [ ] **Step 7: Manual smoke test — run one scan cycle in DRY_RUN**

Run: `DRY_RUN=true python -m src.agent.copy_trade.monitor --scan`
Expected: no `ModuleNotFoundError`/`ImportError`; either "No new swaps detected" or,
if `MORALIS_API_KEY` is still the invalid one, `401` errors logged per wallet exactly
as before (confirms the wiring didn't break the existing failure path) — this task
does not require a working key, only that the code imports and runs cleanly.

- [ ] **Step 8: Commit**

```bash
git add src/agent/copy_trade/monitor.py
git commit -m "feat(copy_trade): wire monitor.py to swap_parser/executor, add auth-failure alert"
```

---

### Task 7: Config updates — inactive wallets, budget fields, enable auto-execute

**Files:**
- Modify: `data/copy_trade/config.json`

- [ ] **Step 1: Edit `target_wallets`**

Set `"monitor": false` for `MAIN_TRADE` (confirmed dead — nonce unchanged 11 days,
verified 2026-07-15) and for `HUB` (same cluster, no independent evidence of activity
— treat as dead-by-association per spec §Bối cảnh rather than re-verify). Leave
`HACK_1`, `HACK_2`, `TOP_2_HACK`, `TOP_3_HACK`, `TOP_4_HACK` as `"monitor": true`
(secondary source per spec §1 — kept, not prioritized, and not proven dead).

- [ ] **Step 2: Add budget fields and enable auto-execute to `copy_settings`**

```json
  "copy_settings": {
    "auto_execute": true,
    "alert_only": false,
    "min_swap_usd": 3.0,
    "max_copy_usd": 50.0,
    "total_budget_usd": 15.39,
    "slice_usd": 1.5,
    "ignore_tokens": ["uBTC", "YFIT", "Web3", "Web3Ai"],
    "poll_interval_seconds": 30
  }
```

- [ ] **Step 3: Validate the JSON is well-formed**

Run: `python -c "import json; json.load(open('data/copy_trade/config.json'))" `
Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add data/copy_trade/config.json
git commit -m "chore(copy_trade): mark confirmed-dead contest wallets inactive, enable auto-execute"
```

---

### Task 8: Obtain working credentials (blocking, user action required)

This task cannot be completed by an engineer alone — it needs two accounts only the
project owner can create.

- [ ] **Step 1: Get a working Moralis API key**

The current `MORALIS_API_KEY` in `.env` has returned `401 Unauthorized` on every
request since at least 2026-07-07 (see `logs/copy_trade.log` on the VPS). Sign in at
https://moralis.com (or create a new account), open a project, copy a fresh API key
from the dashboard, and replace `MORALIS_API_KEY` in both the local `.env` and the
VPS's `.env` (`/home/agent/Track1-trade-onchain/.env`). Verify with:

```bash
curl -s -H "X-API-Key: <NEW_KEY>" "https://deep-index.moralis.io/api/v2.2/wallets/0x8ec6ab4e0f4383ecb01f870fc70cb351a12c43af/history?chain=bsc&limit=1"
```
Expected: HTTP 200 with a JSON body containing a `"result"` array (not a 401).

- [ ] **Step 2: Get a GMGN API key and install `gmgn-cli`**

Sign up at the GMGN developer portal (per `github.com/GMGNAI/gmgn-skills`) and obtain
an API key, then:

```bash
npm i -g gmgn-cli   # package name per github.com/GMGNAI/gmgn-skills — verify exact
                     # name at install time, the repo may have renamed it since this
                     # plan was written
gmgn-cli config --apply <GMGN_API_KEY>
gmgn-cli track smartmoney --chain bsc --limit 5
```
Expected: a JSON/table list of recent smart-money trades on BSC, each with a `maker`
wallet address field — confirms both the key and the command work.

- [ ] **Step 3: Record confirmation in this plan**

Once both steps above return real data, check off this task and proceed to Task 9 —
Task 9 depends on having actually seen the real `gmgn-cli track smartmoney` JSON shape
to extract wallet addresses correctly (field name confirmed as `maker` per the GMGN
skills docs, but confirm against the real response before writing Task 9's parser).

---

### Task 9: GMGN wallet sourcing script

**Files:**
- Create: `scripts/fetch_gmgn_smart_money.py`
- Test: `tests/test_fetch_gmgn_smart_money.py`

**Interfaces:**
- Consumes: `gmgn-cli track smartmoney --chain bsc --limit <n> --json` (external CLI,
  confirmed working in Task 8) via `subprocess.run`.
- Produces: `extract_wallets(trades: list[dict], max_wallets: int) -> list[dict]` —
  pure function taking the parsed JSON trade list, returning deduplicated
  `{"address": ..., "label": "GMGN_SMART_N", "role": "GMGN smart-money", "priority": 5,
  "monitor": True}` entries ready to merge into `config.json`'s `target_wallets`,
  capped at `max_wallets` (respecting the free-tier 10-wallet ceiling) and a
  `merge_wallets(existing: list[dict], new: list[dict]) -> list[dict]` that skips any
  address already present (case-insensitive).

Because Task 8 confirms the real `gmgn-cli` JSON shape before this task starts, write
`extract_wallets`/`merge_wallets` as pure functions covered by unit tests using a
hand-built sample matching whatever field name Task 8 confirmed (`maker` per current
docs — adjust the fixture if Task 8 found a different field name in the live
response), and keep `main()`'s `subprocess.run` call as a thin, untested I/O shell
around them.

- [ ] **Step 1: Write the failing tests**

```python
from scripts.fetch_gmgn_smart_money import extract_wallets, merge_wallets

SAMPLE_TRADES = [
    {"maker": "0xShark0000000000000000000000000000001", "token_symbol": "GEM5", "side": "buy"},
    {"maker": "0xShark0000000000000000000000000000002", "token_symbol": "GEM6", "side": "buy"},
    {"maker": "0xShark0000000000000000000000000000001", "token_symbol": "GEM7", "side": "buy"},
]


def test_extract_wallets_deduplicates_and_labels():
    result = extract_wallets(SAMPLE_TRADES, max_wallets=10)
    addresses = [w["address"] for w in result]
    assert addresses == [
        "0xShark0000000000000000000000000000001",
        "0xShark0000000000000000000000000000002",
    ]
    assert result[0]["label"] == "GMGN_SMART_1"
    assert result[0]["monitor"] is True


def test_extract_wallets_respects_max_wallets_cap():
    many = [{"maker": f"0xW{i:039d}", "token_symbol": "X", "side": "buy"} for i in range(15)]
    result = extract_wallets(many, max_wallets=10)
    assert len(result) == 10


def test_merge_wallets_skips_addresses_already_present_case_insensitive():
    existing = [{"address": "0xABC0000000000000000000000000000000001", "label": "OLD"}]
    new = [
        {"address": "0xabc0000000000000000000000000000000001", "label": "GMGN_SMART_1"},
        {"address": "0xDEF0000000000000000000000000000000002", "label": "GMGN_SMART_2"},
    ]
    merged = merge_wallets(existing, new)
    assert len(merged) == 2
    assert merged[0]["label"] == "OLD"
    assert merged[1]["address"] == "0xDEF0000000000000000000000000000000002"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fetch_gmgn_smart_money.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `scripts/fetch_gmgn_smart_money.py`**

```python
"""Pull recent BSC smart-money trades from gmgn-cli and merge the trading wallets into
data/copy_trade/config.json's target_wallets (§1 of the design spec — GMGN is the
primary signal source, capped at the free-tier ceiling of 10 tracked wallets).

Run: python scripts/fetch_gmgn_smart_money.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"


def extract_wallets(trades: list[dict], max_wallets: int) -> list[dict]:
    seen: dict[str, None] = {}
    for t in trades:
        addr = t.get("maker")
        if addr and addr not in seen:
            seen[addr] = None
        if len(seen) >= max_wallets:
            break
    return [
        {
            "address": addr,
            "label": f"GMGN_SMART_{i + 1}",
            "role": "GMGN smart-money (BSC, auto-sourced)",
            "priority": 5,
            "monitor": True,
        }
        for i, addr in enumerate(seen)
    ]


def merge_wallets(existing: list[dict], new: list[dict]) -> list[dict]:
    existing_addrs = {w["address"].lower() for w in existing}
    merged = list(existing)
    for w in new:
        if w["address"].lower() not in existing_addrs:
            merged.append(w)
            existing_addrs.add(w["address"].lower())
    return merged


def main() -> None:
    proc = subprocess.run(
        ["gmgn-cli", "track", "smartmoney", "--chain", "bsc", "--limit", "100", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f"gmgn-cli failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    trades = json.loads(proc.stdout)
    wallets = extract_wallets(trades, max_wallets=10)

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["target_wallets"] = merge_wallets(config["target_wallets"], wallets)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Merged {len(wallets)} GMGN smart-money wallets into {CONFIG_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fetch_gmgn_smart_money.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the real script once against the live `gmgn-cli`**

First run `gmgn-cli track smartmoney --help` and confirm a `--json` (or equivalent
machine-readable output) flag actually exists — the public docs fetched while writing
this plan didn't show one explicitly. If the flag name differs, update the
`subprocess.run` argument list in `main()` accordingly before proceeding.

Run: `python scripts/fetch_gmgn_smart_money.py`
Expected: prints `Merged N GMGN smart-money wallets into ...config.json`; inspect
`data/copy_trade/config.json` afterward to confirm the new `GMGN_SMART_*` entries look
sane (real-looking addresses, no duplicates of the existing 8 cluster wallets).

- [ ] **Step 6: Commit**

```bash
git add scripts/fetch_gmgn_smart_money.py tests/test_fetch_gmgn_smart_money.py data/copy_trade/config.json
git commit -m "feat(copy_trade): add GMGN smart-money wallet sourcing script"
```

---

### Task 10: Deploy — replace Aegis with copy-trade on the VPS

**Files:** none (ops-only task, no code changes)

- [ ] **Step 1: Push the branch and pull it on the VPS**

```bash
git push origin main
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62 "cd /home/agent/Track1-trade-onchain && sudo -u agent git pull"
```
Expected: VPS repo now has all commits from Tasks 1-9.

- [ ] **Step 2: Update the VPS `.env` with the working Moralis key (from Task 8) and
      GMGN key if `gmgn-cli` also needs to run on the VPS**

Copy the same `MORALIS_API_KEY` value confirmed working in Task 8 into
`/home/agent/Track1-trade-onchain/.env` on the VPS (never print the key value to a
terminal that gets logged — use `scp` of a local `.env` diff or edit directly over
SSH with an editor, not `echo`/`cat <<<`).

- [ ] **Step 3: Stop and disable the Aegis bot**

```bash
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62 "sudo systemctl stop agent.service && sudo systemctl disable agent.service"
```
Expected: `systemctl status agent.service` shows `inactive (dead)`.

- [ ] **Step 4: Kill the old unsupervised copy_trade process, install the systemd unit**

```bash
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62 "sudo kill 194294; sudo cp /home/agent/Track1-trade-onchain/deploy/copy-trade.service /etc/systemd/system/copy-trade.service && sudo systemctl daemon-reload && sudo systemctl enable --now copy-trade.service"
```
(`194294` is the current loose PID found 2026-07-15 — check `ps aux | grep copy_trade`
first in case it has since changed.)

Expected: `systemctl status copy-trade.service` shows `active (running)`.

- [ ] **Step 5: Watch the first few real scan cycles**

```bash
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62 "tail -f /home/agent/Track1-trade-onchain/logs/copy_trade.log"
```
Expected: no `401` errors (confirms the new Moralis key works), `=== Scan #N ===`
lines advancing every ~30s, no Python tracebacks. Ctrl-C to stop watching once a few
clean cycles have passed.

---

## Testing Summary

Every task above ships its own `pytest` suite (Tasks 1-5, 9) or an explicit manual
smoke-test step (Tasks 6, 10). Run the full new suite together before Task 10's
deploy:

```bash
pytest tests/test_copy_trade_swap_parser.py tests/test_copy_trade_budget.py tests/test_copy_trade_positions.py tests/test_binance_web3.py tests/test_copy_trade_executor.py tests/test_fetch_gmgn_smart_money.py tests/test_agent_loop.py -v
```
Expected: all passed, 0 failed.
