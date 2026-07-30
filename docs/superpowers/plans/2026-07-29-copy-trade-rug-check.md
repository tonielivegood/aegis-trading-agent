# Copy-Trade Rug-Check Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block real-money copy_trade buys of tokens whose contract has a live rug
vector (mintable supply, fake-renounced ownership, hidden owner, owner-editable
balances, pausable transfers, modifiable slippage, or an upgradeable proxy) — using
GoPlus data the codebase already has a client for but currently discards.

**Architecture:** New standalone module `passes_rug_check(token_address) -> tuple[bool, str]`
in `src/agent/copy_trade/rug_check.py`, calling GoPlus `token_security` directly
(same endpoint pattern as `prices.get_taxes`, different fields). Wired into
`TradeEngine.open_cluster_position` as one more gate, same shape as the existing
`passes_safety_check` gate right above it. Deliberately NOT touching
`passes_safety_check` itself (shared with Aegis's separate 147-token universe —
see spec for why a shared hard block would break it).

**Tech Stack:** Python, `requests`, existing `call_with_hard_timeout` wrapper
(`src/agent/copy_trade/net_timeout.py`), `pytest` + `unittest.mock.patch`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-29-copy-trade-rug-check-design.md` — read
  it before starting, it has the full reasoning for every decision below.
- Fail closed: any GoPlus error, timeout, or missing record → block the buy.
  Never default to "pass" on missing data.
- The 7 flags gated (exact GoPlus field names): `is_mintable`,
  `can_take_back_ownership`, `hidden_owner`, `owner_change_balance`,
  `transfer_pausable`, `slippage_modifiable`, `is_proxy`. GoPlus returns each as
  the **string** `"0"` or `"1"` (verified live 2026-07-29 against
  `0x40d46ecd7a40bef5311077aab840dcd23a123564`), not an int or bool — compare as
  strings.
- Do NOT gate on `is_honeypot` (already covered by `passes_safety_check` from a
  different source), `creator_percent`/`owner_percent` (already covered by
  `phase2_score`'s `whale_risk` gate), or LP-lock fields (null for most brand-new
  tokens — verified live, would false-block).
- Do NOT modify `passes_safety_check` or any Aegis/W3W code
  (`src/agent/execution/binance_web3.py`, `src/agent/agent_loop.py`).
- Follow this repo's existing conventions exactly: `call_with_hard_timeout` for
  every network call, `log.warning(...)` on failure with `error=type(e).__name__`,
  structlog-style `get_logger(__name__)`.

---

## Task 1: `passes_rug_check()` — standalone GoPlus rug-flag gate

**Files:**
- Create: `src/agent/copy_trade/rug_check.py`
- Test: `tests/test_rug_check.py`

**Interfaces:**
- Produces: `passes_rug_check(token_address: str) -> tuple[bool, str]` — `True, ""`
  if none of the 7 flags are set; `False, <reason>` otherwise, where `<reason>` is
  one of: `mintable`, `fake_renounce`, `hidden_owner`, `owner_can_edit_balance`,
  `transfer_pausable`, `slippage_modifiable`, `upgradeable_proxy`,
  `no_goplus_record`, `goplus_error`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rug_check.py`:

```python
from unittest.mock import patch

import pytest

from src.agent.copy_trade.rug_check import passes_rug_check

TOKEN = "0x" + "a" * 40

_CLEAN = {
    "is_mintable": "0", "can_take_back_ownership": "0", "hidden_owner": "0",
    "owner_change_balance": "0", "transfer_pausable": "0",
    "slippage_modifiable": "0", "is_proxy": "0",
}


class FakeResp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self._status_ok:
            raise Exception("bad status")


def _goplus_payload(info: dict) -> dict:
    return {"result": {TOKEN.lower(): info}}


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_all_flags_clean_passes(mock_get):
    mock_get.return_value = FakeResp(_goplus_payload(_CLEAN))
    assert passes_rug_check(TOKEN) == (True, "")


@pytest.mark.parametrize("field,reason", [
    ("is_mintable", "mintable"),
    ("can_take_back_ownership", "fake_renounce"),
    ("hidden_owner", "hidden_owner"),
    ("owner_change_balance", "owner_can_edit_balance"),
    ("transfer_pausable", "transfer_pausable"),
    ("slippage_modifiable", "slippage_modifiable"),
    ("is_proxy", "upgradeable_proxy"),
])
@patch("src.agent.copy_trade.rug_check.requests.get")
def test_each_flag_blocks_with_its_own_reason(mock_get, field, reason):
    info = dict(_CLEAN)
    info[field] = "1"
    mock_get.return_value = FakeResp(_goplus_payload(info))
    assert passes_rug_check(TOKEN) == (False, reason)


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_no_record_for_token_blocks(mock_get):
    mock_get.return_value = FakeResp({"result": {}})
    assert passes_rug_check(TOKEN) == (False, "no_goplus_record")


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_network_error_blocks(mock_get):
    mock_get.side_effect = Exception("boom")
    assert passes_rug_check(TOKEN) == (False, "goplus_error")


@patch("src.agent.copy_trade.rug_check.requests.get")
def test_bad_http_status_blocks(mock_get):
    mock_get.return_value = FakeResp({}, status_ok=False)
    assert passes_rug_check(TOKEN) == (False, "goplus_error")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rug_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.agent.copy_trade.rug_check'`

- [ ] **Step 3: Write the implementation**

Create `src/agent/copy_trade/rug_check.py`:

```python
"""Rug-vector gate for copy_trade real-money buys.

Separate from the shared `passes_safety_check` (execution/binance_web3.py),
which also serves Aegis's established 147-token universe
(src/agent/data/eligible_tokens.json). A hard block on GoPlus's mint/ownership/
proxy flags would be wrong there: Aegis's own ETH holding
(0x2170ed0880ac9a755fd29b2688956bd959f933f8) trips is_mintable=1 for legitimate
Binance-bridge custody reasons (verified live 2026-07-29). This module only
gates copy_trade's own new-memecoin buy path — see
docs/superpowers/specs/2026-07-29-copy-trade-rug-check-design.md."""
from __future__ import annotations

import requests

from .net_timeout import call_with_hard_timeout
from ..monitor.logger import get_logger

log = get_logger(__name__)

_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="

# Verified live against PIG (0x40d46ecd7a40bef5311077aab840dcd23a123564) and
# Aegis's own ETH holding 2026-07-29 — GoPlus reports each of these reliably as
# the string "0"/"1", even for a 1-day-old contract. Deliberately excludes:
#   - is_honeypot: already covered by passes_safety_check, different source
#   - creator_percent/owner_percent: already covered by phase2_score's whale_risk
#   - LP-lock fields: null for most brand-new tokens, would false-block
_RUG_FLAGS = {
    "is_mintable": "mintable",
    "can_take_back_ownership": "fake_renounce",
    "hidden_owner": "hidden_owner",
    "owner_change_balance": "owner_can_edit_balance",
    "transfer_pausable": "transfer_pausable",
    "slippage_modifiable": "slippage_modifiable",
    "is_proxy": "upgradeable_proxy",
}


def passes_rug_check(token_address: str) -> tuple[bool, str]:
    """(True, "") if none of the GoPlus mint/ownership/proxy rug flags are set.
    Fails closed: any network error or missing record blocks the buy."""
    try:
        r = call_with_hard_timeout(requests.get, _GOPLUS + token_address,
                                   timeout=15, hard_timeout=25)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return False, "no_goplus_record"
    except Exception as e:  # noqa: BLE001 — fail closed: no data = no buy
        log.warning("rug_check_failed", token=token_address, error=type(e).__name__)
        return False, "goplus_error"
    for field, reason in _RUG_FLAGS.items():
        if str(info.get(field, "0")) == "1":
            return False, reason
    return True, ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rug_check.py -v`
Expected: PASS — 11 tests (1 clean + 7 parametrized flags + no-record + network-error + bad-status)

- [ ] **Step 5: Commit**

```bash
git add src/agent/copy_trade/rug_check.py tests/test_rug_check.py
git commit -m "feat(copy_trade): add GoPlus mint/ownership/proxy rug-check gate

Standalone from passes_safety_check (shared with Aegis's established
token universe, which would false-block on is_mintable — see spec)."
```

---

## Task 2: Wire the gate into the real buy path

**Files:**
- Modify: `src/agent/copy_trade/trade_engine.py:1-25` (imports), `:140-146` (gate)
- Modify: `tests/conftest.py`
- Modify: `tests/test_trade_engine.py`

**Interfaces:**
- Consumes: `passes_rug_check(token_address: str) -> tuple[bool, str]` from Task 1.

- [ ] **Step 1: Write the failing test**

In `tests/test_trade_engine.py`, add this test directly after
`test_safety_gate_blocks_and_releases_budget` (currently ends at line 61):

```python
@patch("src.agent.copy_trade.trade_engine.passes_rug_check",
       return_value=(False, "mintable"))
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_rug_gate_blocks_and_releases_budget(_s, _r, tmp_path):
    eng, budget, store = _engine(tmp_path)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14
    assert store.all() == []
```

(Decorator order: `@patch` decorators apply bottom-up, so the bottom-most
decorator — `passes_safety_check` here — becomes the first mock arg after
`self`/positional test args, matching every other test in this file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trade_engine.py::test_rug_gate_blocks_and_releases_budget -v`
Expected: FAIL — either `ImportError`/`AttributeError` (no `passes_rug_check` name
to patch yet in `trade_engine`), or the test opens a real position because the
gate doesn't exist yet.

- [ ] **Step 3: Add the autouse default fixture so the other 69 existing tests don't break**

Every other test in `test_trade_engine.py` (and any other test that reaches
`open_cluster_position`) patches `passes_safety_check` but has never heard of
`passes_rug_check` — without a default, they'd all start making real GoPlus network
calls. `tests/conftest.py` already solves this exact problem for the notifier
(`_no_real_telegram`); follow the same pattern.

Add to `tests/conftest.py` (after the existing `_no_real_telegram` fixture):

```python
@pytest.fixture(autouse=True)
def _default_rug_check_passes(request, monkeypatch):
    """Safety net: tests written before passes_rug_check existed don't know
    about it and would otherwise hit the real GoPlus API inside
    open_cluster_position. Default every test to a passing rug check; tests
    that need to assert on rug-blocking re-patch with their own return value
    (a test-level @patch always wins over this fixture for that test).

    test_rug_check.py is exempt — it tests passes_rug_check itself via
    requests.get at the network boundary, never through this default.
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_rug_check":
        return
    monkeypatch.setattr("src.agent.copy_trade.trade_engine.passes_rug_check",
                        lambda *a, **k: (True, ""))
```

- [ ] **Step 4: Wire the gate into `open_cluster_position`**

In `src/agent/copy_trade/trade_engine.py`, find the import line (around line 20):

```python
from ..execution.binance_web3 import passes_safety_check
```

Add directly below it:

```python
from .rug_check import passes_rug_check
```

Then find this block (currently lines 140-146):

```python
        ok, decimals = passes_safety_check(settings.usdt_address, token_address,
                                           amount_wei)
        if not ok:
            self._budget.release(usd_size)
            self._log_signal(token, token_symbol, cluster, "skipped_safety", "")
            log.warning("cluster_buy_skipped_safety", token=token_symbol)
            return False
        resolved_decimals = decimals or token_decimals
```

Replace it with:

```python
        ok, decimals = passes_safety_check(settings.usdt_address, token_address,
                                           amount_wei)
        if not ok:
            self._budget.release(usd_size)
            self._log_signal(token, token_symbol, cluster, "skipped_safety", "")
            log.warning("cluster_buy_skipped_safety", token=token_symbol)
            return False
        rug_ok, rug_reason = passes_rug_check(token_address)
        if not rug_ok:
            self._budget.release(usd_size)
            self._log_signal(token, token_symbol, cluster, "skipped_rug", rug_reason)
            log.warning("cluster_buy_skipped_rug", token=token_symbol,
                       reason=rug_reason)
            return False
        resolved_decimals = decimals or token_decimals
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `python -m pytest tests/test_trade_engine.py::test_rug_gate_blocks_and_releases_budget -v`
Expected: PASS

- [ ] **Step 6: Run the FULL test suite to confirm nothing else broke**

Run: `python -m pytest -q`
Expected: all tests pass (was 820 passed, 2 skipped before this plan — expect
820 + 11 (Task 1) + 1 (this test) = 832 passed, 2 skipped). If any pre-existing
`test_trade_engine.py` test fails, it means the autouse fixture in Step 3 isn't
taking effect — check the fixture's `monkeypatch.setattr` target path matches
exactly `src.agent.copy_trade.trade_engine.passes_rug_check` (must patch where
it's imported TO, not where it's defined — same rule Python mocking always
follows, and the same pattern `passes_safety_check` already uses in every
existing test in this file).

- [ ] **Step 7: Commit**

```bash
git add src/agent/copy_trade/trade_engine.py tests/conftest.py tests/test_trade_engine.py
git commit -m "feat(copy_trade): wire rug-check gate into the real buy path

Runs right after passes_safety_check in open_cluster_position, same shape
(release budget, log skipped_rug, block). Added an autouse conftest
fixture defaulting the gate to pass, mirroring the existing
_no_real_telegram pattern, so the ~70 pre-existing tests that never knew
about this gate don't start hitting the real GoPlus API."
```

---

## Self-review notes

- **Spec coverage:** Phần 1 (kiến trúc tách riêng) → Task 1 + Task 2 Step 4.
  Phần 2 (7 cờ) → Task 1's `_RUG_FLAGS`. Phần 3 (fail closed) → Task 1's
  `no_goplus_record`/`goplus_error` paths, tested explicitly. Phần 4 (kiểm thử) →
  Task 1's 11 tests match the plan (all-clean, 7×flag, no-record — bad-status and
  network-error were folded into one `goplus_error` reason per the spec's error
  handling section, both tested separately for coverage of both failure shapes).
- **Type consistency:** `passes_rug_check` signature `(token_address: str) -> tuple[bool, str]`
  is identical between Task 1's definition and Task 2's call site and test mocks.
- **No placeholders:** every step has complete, runnable code — no "add error
  handling" or "similar to Task N" shortcuts.
