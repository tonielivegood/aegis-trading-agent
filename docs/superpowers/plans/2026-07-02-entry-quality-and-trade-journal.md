# Entry-Quality Gates + Trade Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the entry-quality gap that let a near-zero-liquidity token (SPCX) into the
wallet, add operational hardening (entry-fail cooldown), and build a trade journal so the
user's soak-test pass bar (win-rate ≥ 40%, no trade breaches its own class's hard stop) can
actually be evaluated.

**Architecture:** Config-driven filters layered in front of the existing hot-token discovery
and just-in-time safety-check paths in `agent_loop.py`; a new standalone, pure `trade_journal`
module that `_execute()` calls on every real (non-simulated) fill; a before/after diff of the
position book in `_event_decision()` to recover exit PnL context without touching the 11
`book.close()` call sites inside `decide_exits()`.

**Tech Stack:** Python 3.11+, pytest + pytest-mock, existing `CooldownBook`/`PositionBook`
patterns, Binance W3W `hot_token()`/`price_info()`/`quote()` (already wired, `binance_web3.py`).

## Global Constraints

- Every new/changed function keeps this project's existing fail-safe philosophy: a network
  hiccup or malformed response REJECTS the candidate / skips the journal write, never raises
  into the tick, never silently admits something on missing data.
- New config settings follow the exact existing pattern in `config.py`: a `Settings` field with
  an inline default + comment, and a matching `_get(...)` call in `get_settings()` using the
  same string default.
- No change to exit rail values (TP/trail/stop), `beta_core` (disabled, out of scope), or trade
  cadence/caps (explicitly rejected by the user during brainstorming).
- Every task ends with `ruff check <changed files>` clean and the FULL test suite green
  (`python -m pytest -q` from `E:\Track1-trade-onchain`), not just the new tests.
- Commit after each task (see Global Constraints in the spec: this project always commits+pushes
  +deploys per logical change — deployment to the VPS happens once at the END of the whole plan,
  not per task, to avoid restarting the live bot repeatedly; note this explicitly in the final task).

---

### Task 1: Config settings

**Files:**
- Modify: `src/agent/config.py:247-255` (Settings class fields, insert after existing
  `binance_w3w_max_price_impact` block) and `src/agent/config.py:422-424` (`get_settings()`)

**Interfaces:**
- Produces: `settings.binance_w3w_min_liquidity_usd` (float), `settings.binance_w3w_min_volume_usd`
  (float), `settings.binance_w3w_max_top10_holding_pct` (float), `settings.binance_w3w_min_holders`
  (int), `settings.binance_w3w_min_liquidity_usd_check` (float), `settings.entry_fail_cooldown_seconds`
  (int) — all consumed by later tasks.

- [ ] **Step 1: Add the 6 new Settings fields**

In `src/agent/config.py`, find this existing block (currently lines 247-255):

```python
    binance_w3w_universe_enabled: bool = True
    binance_w3w_max_tax_rate: float = 0.10        # reject a candidate taxed above this (matches
                                                  # Binance's own query-token-audit "critical" bar)
    binance_w3w_max_price_impact: float = 0.15    # NEW (2/7, real-money incident): reject a candidate
                                                  # whose OWN quote shows selling our ticket size back
                                                  # would lose more than this to price impact — an
                                                  # honeypot/tax pass alone doesn't catch a pool too
                                                  # thin to exit (SPCX: not a honeypot, 0% tax, but an
                                                  # 86% price-impact trap on exit).
```

Replace it with (adds 6 new fields after the existing ones, keeps everything above unchanged):

```python
    binance_w3w_universe_enabled: bool = True
    binance_w3w_max_tax_rate: float = 0.10        # reject a candidate taxed above this (matches
                                                  # Binance's own query-token-audit "critical" bar)
    binance_w3w_max_price_impact: float = 0.15    # NEW (2/7, real-money incident): reject a candidate
                                                  # whose OWN quote shows selling our ticket size back
                                                  # would lose more than this to price impact — an
                                                  # honeypot/tax pass alone doesn't catch a pool too
                                                  # thin to exit (SPCX: not a honeypot, 0% tax, but an
                                                  # 86% price-impact trap on exit).
    # NEW (2/7, post-SPCX hardening): server-side hot-token discovery filters — Binance
    # filters these OUT before the candidate list ever reaches the bot, so a 10-holder,
    # ~$1-liquidity pool (SPCX) never even shows up as a candidate.
    binance_w3w_min_liquidity_usd: float = 20_000.0     # hot-token liquidityMin
    binance_w3w_min_volume_usd: float = 5_000.0         # hot-token volumeMin
    binance_w3w_max_top10_holding_pct: float = 30.0     # hot-token top10HoldingPercentMax
    # Second-layer JIT floor (in case liquidity moved between discovery and entry decision).
    binance_w3w_min_holders: int = 30
    binance_w3w_min_liquidity_usd_check: float = 10_000.0
    # Cooldown after EVERY backend fails an entry attempt — stops the bot hammering the
    # same dead candidate every tick (observed: 5+ minutes of retries on one symbol, 2/7).
    entry_fail_cooldown_seconds: int = 900
```

- [ ] **Step 2: Add the matching `get_settings()` wiring**

Find this existing block (currently lines 422-424):

```python
        binance_w3w_universe_enabled=_get_bool("BINANCE_W3W_UNIVERSE_ENABLED", "true"),
        binance_w3w_max_tax_rate=float(_get("BINANCE_W3W_MAX_TAX_RATE", "0.10")),
        binance_w3w_max_price_impact=float(_get("BINANCE_W3W_MAX_PRICE_IMPACT", "0.15")),
```

Replace it with:

```python
        binance_w3w_universe_enabled=_get_bool("BINANCE_W3W_UNIVERSE_ENABLED", "true"),
        binance_w3w_max_tax_rate=float(_get("BINANCE_W3W_MAX_TAX_RATE", "0.10")),
        binance_w3w_max_price_impact=float(_get("BINANCE_W3W_MAX_PRICE_IMPACT", "0.15")),
        binance_w3w_min_liquidity_usd=float(_get("BINANCE_W3W_MIN_LIQUIDITY_USD", "20000")),
        binance_w3w_min_volume_usd=float(_get("BINANCE_W3W_MIN_VOLUME_USD", "5000")),
        binance_w3w_max_top10_holding_pct=float(_get("BINANCE_W3W_MAX_TOP10_HOLDING_PCT", "30")),
        binance_w3w_min_holders=int(_get("BINANCE_W3W_MIN_HOLDERS", "30")),
        binance_w3w_min_liquidity_usd_check=float(_get("BINANCE_W3W_MIN_LIQUIDITY_USD_CHECK", "10000")),
        entry_fail_cooldown_seconds=int(_get("ENTRY_FAIL_COOLDOWN_SECONDS", "900")),
```

- [ ] **Step 3: Verify with a quick settings-load test**

Run: `python -c "from src.agent.config import settings; print(settings.binance_w3w_min_liquidity_usd, settings.entry_fail_cooldown_seconds)"`
Expected output: `20000.0 900`

- [ ] **Step 4: Run the existing config test suite**

Run: `python -m pytest -q tests/test_env_config.py`
Expected: all pass (these new fields have working defaults, nothing existing breaks).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/agent/config.py
git add src/agent/config.py
git commit -m "Add config settings for entry-quality gates + entry-fail cooldown (2/7 hardening)"
```

---

### Task 2: Server-side discovery filters (`_w3w_hot_token_items`)

**Files:**
- Modify: `src/agent/agent_loop.py:306-320` (`_w3w_hot_token_items`)
- Test: `tests/test_agent_loop.py` (extend the existing `_w3w_hot_token_items` test block, ~line 361-379)

**Interfaces:**
- Consumes: `settings.binance_w3w_min_liquidity_usd`, `settings.binance_w3w_min_volume_usd`,
  `settings.binance_w3w_max_top10_holding_pct` (Task 1); `binance_web3.hot_token()`'s existing
  `liquidity_min`, `volume_min`, `top10_holding_percent_max` kwargs (already implemented,
  unused until now).
- Produces: no change to `_w3w_hot_token_items()`'s return type (`list[dict] | None`).

- [ ] **Step 1: Write the failing test**

In `tests/test_agent_loop.py`, add right after `test_w3w_hot_token_items_passes_meme_breakout_min`
(the block ending around line 379):

```python
def test_w3w_hot_token_items_passes_liquidity_volume_holder_filters(mocker):
    mocker.patch.object(al.settings, "binance_w3w_universe_enabled", True)
    mocker.patch.object(al.settings, "binance_w3w_min_liquidity_usd", 20000.0)
    mocker.patch.object(al.settings, "binance_w3w_min_volume_usd", 5000.0)
    mocker.patch.object(al.settings, "binance_w3w_max_top10_holding_pct", 30.0)
    hot = mocker.patch("src.agent.execution.binance_web3.hot_token", return_value=[])
    al._w3w_hot_token_items()
    assert hot.call_args.kwargs["liquidity_min"] == 20000.0
    assert hot.call_args.kwargs["volume_min"] == 5000.0
    assert hot.call_args.kwargs["top10_holding_percent_max"] == 30.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest -q tests/test_agent_loop.py::test_w3w_hot_token_items_passes_liquidity_volume_holder_filters -v`
Expected: FAIL — `KeyError: 'liquidity_min'` (the kwarg isn't passed yet).

- [ ] **Step 3: Update `_w3w_hot_token_items()`**

In `src/agent/agent_loop.py`, replace the current function (lines 306-320):

```python
def _w3w_hot_token_items() -> list[dict] | None:
    """Fetch this tick's server-side-filtered meme candidates (Option B discovery).
    None (not []) on any failure or when the flag is off — the caller falls back to
    the legacy client-side scan; an empty list would instead mean "scanned, found
    nothing", which is a different and wrong signal to send on a network hiccup."""
    if not settings.binance_w3w_universe_enabled:
        return None
    from .aegis import token_class as tc
    from .execution import binance_web3 as bw
    mp = tc.params(tc.MEME)
    try:
        return bw.hot_token(price_change_percent_min=mp.breakout_min * 100)
    except Exception as e:  # noqa: BLE001 — a hiccup here must fall back, never break the tick
        log.warning("w3w_hot_token_failed", error=type(e).__name__)
        return None
```

with:

```python
def _w3w_hot_token_items() -> list[dict] | None:
    """Fetch this tick's server-side-filtered meme candidates (Option B discovery).
    None (not []) on any failure or when the flag is off — the caller falls back to
    the legacy client-side scan; an empty list would instead mean "scanned, found
    nothing", which is a different and wrong signal to send on a network hiccup.

    Liquidity/volume/top10-holding filters (2/7, post-SPCX hardening) are applied
    SERVER-SIDE by Binance — a near-zero-liquidity, 10-holder token never even
    reaches the candidate list, instead of relying only on the JIT safety check."""
    if not settings.binance_w3w_universe_enabled:
        return None
    from .aegis import token_class as tc
    from .execution import binance_web3 as bw
    mp = tc.params(tc.MEME)
    try:
        return bw.hot_token(
            price_change_percent_min=mp.breakout_min * 100,
            liquidity_min=settings.binance_w3w_min_liquidity_usd,
            volume_min=settings.binance_w3w_min_volume_usd,
            top10_holding_percent_max=settings.binance_w3w_max_top10_holding_pct)
    except Exception as e:  # noqa: BLE001 — a hiccup here must fall back, never break the tick
        log.warning("w3w_hot_token_failed", error=type(e).__name__)
        return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest -q tests/test_agent_loop.py::test_w3w_hot_token_items_passes_liquidity_volume_holder_filters -v`
Expected: PASS

- [ ] **Step 5: Run the full `_w3w_hot_token_items` test block + full suite**

Run: `python -m pytest -q tests/test_agent_loop.py -k hot_token_items`
Expected: all pass (the 2 pre-existing tests + the new one).

Run: `python -m pytest -q`
Expected: all pass (no regressions elsewhere).

- [ ] **Step 6: Lint + commit**

```bash
ruff check src/agent/agent_loop.py tests/test_agent_loop.py
git add src/agent/agent_loop.py tests/test_agent_loop.py
git commit -m "Pass liquidity/volume/top10-holding filters to hot-token discovery (server-side)"
```

---

### Task 3: Safety-check holders/liquidity floor (`_w3w_safety_check`)

**Files:**
- Modify: `src/agent/agent_loop.py:323-368` (`_w3w_safety_check`)
- Test: `tests/test_agent_loop.py` (extend the `_w3w_safety_check` test block)

**Interfaces:**
- Consumes: `settings.binance_w3w_min_holders`, `settings.binance_w3w_min_liquidity_usd_check`
  (Task 1); `binance_web3.price_info(contracts: list[str]) -> dict[str, dict]` (existing,
  keyed by lowercase contract address, entries have `"holders"` (int-like) and `"liquidity"`
  (string-like float) fields — confirmed live 2026-07-02 against the actual SPCX incident data).
- Produces: no change to `_w3w_safety_check(equity_usd)`'s return type (still returns a
  `Callable[[BreakoutSignal], bool]`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_agent_loop.py`, add right after `test_w3w_safety_check_allows_low_price_impact`
(the block that currently ends the price-impact tests, just before `test_w3w_safety_check_no_routes_fails_closed`):

```python
def test_w3w_safety_check_blocks_low_holders(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch.object(al.settings, "binance_w3w_min_holders", 30)
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "priceImpactPercent": "1.0",
         "toToken": {"isHoneyPot": False, "taxRate": "0", "decimal": "18"}},
    ])
    mocker.patch("src.agent.execution.binance_web3.price_info", return_value={
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaad": {"holders": 10, "liquidity": "50000"},
    })
    sig = BreakoutSignal(symbol="THINHOLD", contract="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaad",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_blocks_low_liquidity(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch.object(al.settings, "binance_w3w_min_liquidity_usd_check", 10000.0)
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "priceImpactPercent": "1.0",
         "toToken": {"isHoneyPot": False, "taxRate": "0", "decimal": "18"}},
    ])
    mocker.patch("src.agent.execution.binance_web3.price_info", return_value={
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbe": {"holders": 100, "liquidity": "0.83"},
    })
    sig = BreakoutSignal(symbol="SPCX2", contract="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbe",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_price_info_failure_fails_closed(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "priceImpactPercent": "1.0",
         "toToken": {"isHoneyPot": False, "taxRate": "0", "decimal": "18"}},
    ])
    mocker.patch("src.agent.execution.binance_web3.price_info", side_effect=RuntimeError("boom"))
    sig = BreakoutSignal(symbol="NETERR", contract="0xccccccccccccccccccccccccccccccccccccccf",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_passes_when_holders_and_liquidity_ok(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    from src.agent.data import token_list
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "priceImpactPercent": "1.0",
         "toToken": {"isHoneyPot": False, "taxRate": "0", "decimal": "18"}},
    ])
    mocker.patch("src.agent.execution.binance_web3.price_info", return_value={
        "0xddddddddddddddddddddddddddddddddddddddd0": {"holders": 500, "liquidity": "80000"},
    })
    sig = BreakoutSignal(symbol="GOODLIQ", contract="0xddddddddddddddddddddddddddddddddddddddd0",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    try:
        check = al._w3w_safety_check(40.0)
        assert check(sig) is True
    finally:
        token_list._discovered.pop("GOODLIQ", None)
        token_list._discovered_classes.pop("GOODLIQ", None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -q tests/test_agent_loop.py -k "low_holders or low_liquidity or price_info_failure or holders_and_liquidity_ok" -v`
Expected: `test_w3w_safety_check_blocks_low_holders` FAILS (check returns True, no holders gate
yet), `test_w3w_safety_check_blocks_low_liquidity` FAILS (same reason),
`test_w3w_safety_check_price_info_failure_fails_closed` FAILS (no `price_info` call happens at
all yet, so the mocked `side_effect` is never triggered and `check()` returns True),
`test_w3w_safety_check_passes_when_holders_and_liquidity_ok` PASSES already (no gate to fail).

- [ ] **Step 3: Update `_w3w_safety_check()`**

In `src/agent/agent_loop.py`, find this block inside `check()` (currently lines 349-361 of the
existing function body):

```python
        if tax > settings.binance_w3w_max_tax_rate:
            log.warning("w3w_tax_too_high", symbol=sig.symbol, tax=tax)
            return False
        # Real-money incident (2/7): not a honeypot, 0% tax, still an 86% price-impact
        # trap — the pool was too thin to exit even though buying in looked fine.
        try:
            impact = float(best.get("priceImpactPercent") or 0) / 100.0
        except (TypeError, ValueError):
            impact = 1.0
        if impact > settings.binance_w3w_max_price_impact:
            log.warning("w3w_price_impact_too_high", symbol=sig.symbol, impact=impact)
            return False
        try:
            decimals = int(to_tok.get("decimal") or 18)
```

Replace it with (adds the holders/liquidity check between the price-impact check and the
decimals/registration step):

```python
        if tax > settings.binance_w3w_max_tax_rate:
            log.warning("w3w_tax_too_high", symbol=sig.symbol, tax=tax)
            return False
        # Real-money incident (2/7): not a honeypot, 0% tax, still an 86% price-impact
        # trap — the pool was too thin to exit even though buying in looked fine.
        try:
            impact = float(best.get("priceImpactPercent") or 0) / 100.0
        except (TypeError, ValueError):
            impact = 1.0
        if impact > settings.binance_w3w_max_price_impact:
            log.warning("w3w_price_impact_too_high", symbol=sig.symbol, impact=impact)
            return False
        # Second-layer liquidity/holder floor (2/7): quote() doesn't carry holders/liquidity,
        # so a dedicated price_info() call is needed. Fail closed on any error — same policy
        # as every other check in this function.
        try:
            info = bw.price_info([sig.contract]).get(sig.contract.lower())
        except Exception as e:  # noqa: BLE001 — fail closed
            log.warning("w3w_price_info_failed", symbol=sig.symbol, error=type(e).__name__)
            return False
        if not info:
            log.warning("w3w_price_info_missing", symbol=sig.symbol)
            return False
        try:
            holders = int(info.get("holders") or 0)
        except (TypeError, ValueError):
            holders = 0
        if holders < settings.binance_w3w_min_holders:
            log.warning("w3w_holders_too_low", symbol=sig.symbol, holders=holders)
            return False
        try:
            liquidity = float(info.get("liquidity") or 0)
        except (TypeError, ValueError):
            liquidity = 0.0
        if liquidity < settings.binance_w3w_min_liquidity_usd_check:
            log.warning("w3w_liquidity_too_low", symbol=sig.symbol, liquidity=liquidity)
            return False
        try:
            decimals = int(to_tok.get("decimal") or 18)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_agent_loop.py -k "low_holders or low_liquidity or price_info_failure or holders_and_liquidity_ok" -v`
Expected: all 4 PASS.

- [ ] **Step 5: Run the full safety-check block + full suite**

Run: `python -m pytest -q tests/test_agent_loop.py -k safety_check`
Expected: all pass (the pre-existing 7 tests + the 4 new ones = 11 total).

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Lint + commit**

```bash
ruff check src/agent/agent_loop.py tests/test_agent_loop.py
git add src/agent/agent_loop.py tests/test_agent_loop.py
git commit -m "Add holders/liquidity floor to the JIT safety check (2nd defense layer)"
```

---

### Task 4: Entry-fail cooldown (record on failure + consult before entry)

**Files:**
- Modify: `src/agent/agent_loop.py` — add `ENTRY_FAIL_COOLDOWN_FILE` constant (near line 41),
  `_execute()` (currently lines 1047-1104), `_event_decision()` (currently lines 634-751)
- Modify: `src/agent/aegis/sniper.py` — `run()` signature (currently lines 64-76) and its
  cooldown-union logic (currently line 121)
- Test: `tests/test_agent_loop.py`, `tests/test_sniper.py`

**Interfaces:**
- Consumes: `settings.entry_fail_cooldown_seconds` (Task 1); existing `CooldownBook` class
  (`src/agent/aegis/cooldown.py`, unchanged) — `load(path)`, `record_exit(symbol, now)` (reused
  for entry-fails too — same "don't touch until N seconds pass" semantics), `cooling_down(now,
  cooldown_s)`, `prune(now, cooldown_s)`, `save(path)`.
- Produces: `sniper.run(..., entry_fail_cooldowns: CooldownBook | None = None,
  entry_fail_cooldown_s: float | None = None)` — new OPTIONAL kwargs, backward compatible
  (every existing call/test omits them and behaves exactly as before).

- [ ] **Step 1: Write the failing test for `sniper.run` consulting a second cooldown book**

In `tests/test_sniper.py`, add after `test_hot_token_entry_tracked_even_when_symbol_missing_from_prices`:

```python
def test_entry_fail_cooldown_blocks_reentry():
    from src.agent.aegis.cooldown import CooldownBook as CB
    fail_book = CB()
    fail_book.record_exit("MEOW", 1000.0)   # "MEOW" failed to enter 0s ago
    items = [_hot_item("MEOW", change=8.0, volume=9000.0, contract="0xmeow")]
    orders, _ = sniper.run(
        _state(), {"MEOW": 1.0}, book=PositionBook(), feed=FakeFeed({}), cooldowns=CooldownBook(),
        regime_flag=Regime.RISK_ON, universe=[], now=1000.0, floor_usd=6.0, allow=_allow,
        hot_token_items=items, entry_fail_cooldowns=fail_book, entry_fail_cooldown_s=900.0)
    assert orders == []


def test_entry_fail_cooldown_expires():
    from src.agent.aegis.cooldown import CooldownBook as CB
    fail_book = CB()
    fail_book.record_exit("MEOW", 0.0)      # failed a long time ago
    items = [_hot_item("MEOW", change=8.0, volume=9000.0, contract="0xmeow")]
    orders, _ = sniper.run(
        _state(), {"MEOW": 1.0}, book=PositionBook(), feed=FakeFeed({}), cooldowns=CooldownBook(),
        regime_flag=Regime.RISK_ON, universe=[], now=1000.0, floor_usd=6.0, allow=_allow,
        hot_token_items=items, entry_fail_cooldowns=fail_book, entry_fail_cooldown_s=900.0)
    assert len(orders) == 1 and orders[0].token_out == "MEOW"


def test_no_entry_fail_cooldown_param_behaves_as_before():
    items = [_hot_item("MEOW", change=8.0, volume=9000.0, contract="0xmeow")]
    orders, _ = sniper.run(
        _state(), {"MEOW": 1.0}, book=PositionBook(), feed=FakeFeed({}), cooldowns=CooldownBook(),
        regime_flag=Regime.RISK_ON, universe=[], now=1000.0, floor_usd=6.0, allow=_allow,
        hot_token_items=items)
    assert len(orders) == 1
```

- [ ] **Step 2: Run them to verify the first fails**

Run: `python -m pytest -q tests/test_sniper.py -k entry_fail_cooldown -v`
Expected: `test_entry_fail_cooldown_blocks_reentry` FAILS with `TypeError: run() got an
unexpected keyword argument 'entry_fail_cooldowns'`. The other two also fail the same way
(the kwarg doesn't exist yet).

- [ ] **Step 3: Update `sniper.run()`'s signature and cooldown-union logic**

In `src/agent/aegis/sniper.py`, replace the function signature (currently lines 64-76):

```python
def run(state: PortfolioState, prices: dict[str, float], *, book: PositionBook,
        feed, cooldowns: CooldownBook, regime_flag: rg.Regime | str,
        universe: list[str], now: float, floor_usd: float | None = None,
        settlement: str = STABLE, overpump_pct: float | None = None,
        cooldown_s: float | None = None,
        allow: Callable[[str], bool] | None = None,
        trending: frozenset[str] | set[str] = frozenset(),
        manage_classes: set[str] | frozenset[str] | None = None,
        max_meme_positions: int | None = None,
        meme_usd: float | None = None,
        hot_token_items: list[dict] | None = None,
        safety_check: Callable[[BreakoutSignal], bool] | None = None,
        ) -> tuple[list[TradeOrder], str]:
```

with:

```python
def run(state: PortfolioState, prices: dict[str, float], *, book: PositionBook,
        feed, cooldowns: CooldownBook, regime_flag: rg.Regime | str,
        universe: list[str], now: float, floor_usd: float | None = None,
        settlement: str = STABLE, overpump_pct: float | None = None,
        cooldown_s: float | None = None,
        allow: Callable[[str], bool] | None = None,
        trending: frozenset[str] | set[str] = frozenset(),
        manage_classes: set[str] | frozenset[str] | None = None,
        max_meme_positions: int | None = None,
        meme_usd: float | None = None,
        hot_token_items: list[dict] | None = None,
        safety_check: Callable[[BreakoutSignal], bool] | None = None,
        entry_fail_cooldowns: CooldownBook | None = None,
        entry_fail_cooldown_s: float | None = None,
        ) -> tuple[list[TradeOrder], str]:
```

Then find this line (currently line 121):

```python
        cooling = cooldowns.cooling_down(now=now, cooldown_s=cooldown_s)
```

Replace it with:

```python
        cooling = cooldowns.cooling_down(now=now, cooldown_s=cooldown_s)
        if entry_fail_cooldowns is not None and entry_fail_cooldown_s is not None:
            # A symbol that failed to enter recently is skipped too — stops the bot
            # hammering the same dead candidate every tick (2/7 hardening).
            cooling = cooling | entry_fail_cooldowns.cooling_down(
                now=now, cooldown_s=entry_fail_cooldown_s)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_sniper.py -k entry_fail_cooldown -v`
Expected: all 3 PASS.

Run: `python -m pytest -q tests/test_sniper.py`
Expected: all pass (no regressions in the other 18 sniper tests).

- [ ] **Step 5: Write the failing test for recording a failure in `_execute()`**

In `tests/test_agent_loop.py`, add after `test_twak_primary_does_not_failover`:

```python
def test_execute_records_entry_fail_cooldown(tmp_path, mocker):
    mocker.patch.object(al, "ENTRY_FAIL_COOLDOWN_FILE", tmp_path / "entry_fail_cooldown.json")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    mocker.patch.object(al.settings, "entry_fail_cooldown_seconds", 900)
    failing = _FakeDex(fail=True)
    _patch_backends(mocker, {"1inch": failing, "openocean": _FakeDex(fail=True),
                             "pancake": _FakeDex(fail=True)})
    al._execute([_entry_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    from src.agent.aegis.cooldown import CooldownBook
    book = CooldownBook.load(al.ENTRY_FAIL_COOLDOWN_FILE)
    assert "BAS" in book.last_exit


def test_execute_does_not_record_cooldown_for_failed_exit(tmp_path, mocker):
    mocker.patch.object(al, "ENTRY_FAIL_COOLDOWN_FILE", tmp_path / "entry_fail_cooldown.json")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    send = mocker.patch.object(al.notifier, "send")
    _patch_backends(mocker, {"1inch": _FakeDex(fail=True), "openocean": _FakeDex(fail=True),
                             "pancake": _FakeDex(fail=True)})
    al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    from src.agent.aegis.cooldown import CooldownBook
    book = CooldownBook.load(al.ENTRY_FAIL_COOLDOWN_FILE)
    assert book.last_exit == {}
    assert send.called   # the existing exit-failure alert still fires
```

Note: `now=0` is passed as a plain int in the existing test fixtures for `_execute` (see
`test_exit_fails_over_to_backup_backend` etc.) — `_execute()` never calls `.timestamp()` or
`.strftime()` on `now` for the FAILURE path (only the success/journal path in Task 6 needs a
real `datetime`), so `now=0` is safe here.

- [ ] **Step 6: Run them to verify they fail**

Run: `python -m pytest -q tests/test_agent_loop.py -k entry_fail_cooldown -v`
Expected: both FAIL — `AttributeError: module 'src.agent.agent_loop' has no attribute
'ENTRY_FAIL_COOLDOWN_FILE'`.

- [ ] **Step 7: Add the constant and wire recording into `_execute()`**

In `src/agent/agent_loop.py`, find line 41:

```python
COOLDOWN_FILE = RUNTIME / "aegis_cooldown.json"
```

Add right after it:

```python
COOLDOWN_FILE = RUNTIME / "aegis_cooldown.json"
ENTRY_FAIL_COOLDOWN_FILE = RUNTIME / "aegis_entry_fail_cooldown.json"
```

Now find the `else` branch inside `_execute()`'s order loop (currently lines 1099-1103):

```python
        else:                                               # every backend (incl. failover) failed
            results.append({"order": o.reason, "error": str(last_err),
                            "token_in": o.token_in, "token_out": o.token_out})
            if is_exit:                                     # a stuck EXIT is a real DD risk → page now
                _alert_exit_failure(o, str(last_err))
    return results
```

Replace it with:

```python
        else:                                               # every backend (incl. failover) failed
            results.append({"order": o.reason, "error": str(last_err),
                            "token_in": o.token_in, "token_out": o.token_out})
            if is_exit:                                     # a stuck EXIT is a real DD risk → page now
                _alert_exit_failure(o, str(last_err))
            else:
                _record_entry_fail_cooldown(o.token_out, now)
    return results


def _record_entry_fail_cooldown(symbol: str, now) -> None:
    """Every backend failed to buy `symbol` — cool it down so the bot doesn't hammer
    the same dead candidate every tick (2/7 hardening; observed 5+ min of retries)."""
    from .aegis.cooldown import CooldownBook
    now_ts = now.timestamp() if hasattr(now, "timestamp") else float(now)
    book = CooldownBook.load(ENTRY_FAIL_COOLDOWN_FILE)
    book.record_exit(symbol, now_ts)
    book.prune(now=now_ts, cooldown_s=settings.entry_fail_cooldown_seconds)
    book.save(ENTRY_FAIL_COOLDOWN_FILE)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_agent_loop.py -k entry_fail_cooldown -v`
Expected: both PASS.

- [ ] **Step 9: Wire `_event_decision()` to load and pass the entry-fail cooldown book**

In `src/agent/agent_loop.py`, find this line inside `_event_decision()` (currently around line 651):

```python
    cooldowns = CooldownBook.load(COOLDOWN_FILE)
```

Replace it with:

```python
    cooldowns = CooldownBook.load(COOLDOWN_FILE)
    entry_fail_cooldowns = CooldownBook.load(ENTRY_FAIL_COOLDOWN_FILE)
```

Then find the `sniper.run(...)` call (currently lines 741-746):

```python
    orders, mode = sniper.run(sniper_state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=meme_flag, universe=symbols, now=now_ts, trending=trending,
                              max_meme_positions=meme_cap, meme_usd=meme_usd,
                              manage_classes=sniper_classes, allow=entry_allow,
                              hot_token_items=hot_token_items,
                              safety_check=_w3w_safety_check(state.equity_usd) if hot_token_items is not None else None)
```

Replace it with:

```python
    orders, mode = sniper.run(sniper_state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=meme_flag, universe=symbols, now=now_ts, trending=trending,
                              max_meme_positions=meme_cap, meme_usd=meme_usd,
                              manage_classes=sniper_classes, allow=entry_allow,
                              hot_token_items=hot_token_items,
                              safety_check=_w3w_safety_check(state.equity_usd) if hot_token_items is not None else None,
                              entry_fail_cooldowns=entry_fail_cooldowns,
                              entry_fail_cooldown_s=settings.entry_fail_cooldown_seconds)
```

- [ ] **Step 10: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass, no regressions.

- [ ] **Step 11: Lint + commit**

```bash
ruff check src/agent/agent_loop.py src/agent/aegis/sniper.py tests/test_agent_loop.py tests/test_sniper.py
git add src/agent/agent_loop.py src/agent/aegis/sniper.py tests/test_agent_loop.py tests/test_sniper.py
git commit -m "Add entry-fail cooldown: record on every-backend-fails, consult before re-entry"
```

---

### Task 5: Trade journal module (new, standalone)

**Files:**
- Create: `src/agent/aegis/trade_journal.py`
- Test: `tests/test_trade_journal.py`

**Interfaces:**
- Produces: `record_entry(path, *, symbol, token_class, entry_price, usd_size, reason, backend,
  tx, time_iso) -> None`; `record_exit(path, *, symbol, token_class, entry_price, exit_price,
  usd_size, hold_minutes, reason, backend, tx, time_iso) -> None`; `read_all(path) ->
  list[dict]`; `report(path) -> dict` (keys: `n_trades`, `win_rate`, `avg_pnl_pct`,
  `worst_pnl_pct`, `total_pnl_usd`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trade_journal.py`:

```python
"""TDD for the append-only trade journal (win-rate / PnL evaluation data source)."""
from __future__ import annotations

import json

from src.agent.aegis import trade_journal as tj


def test_record_entry_writes_one_json_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_entry(path, symbol="SPCX", token_class="meme", entry_price=4.88e-06,
                    usd_size=5.10, reason="breakout vol 0.0x +14.3%", backend="1inch",
                    tx="0xabc", time_iso="2026-07-02T00:16:19Z")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "entry" and row["symbol"] == "SPCX" and row["usd_size"] == 5.10


def test_record_exit_computes_pnl(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_exit(path, symbol="FOO", token_class="meme", entry_price=1.0, exit_price=1.4,
                   usd_size=10.0, hold_minutes=42.0, reason="aegis exit: hard TP 1.4x",
                   backend="pancake", tx="0xdef", time_iso="2026-07-02T01:00:00Z")
    rows = tj.read_all(path)
    assert len(rows) == 1
    assert rows[0]["pnl_pct"] == 0.4 - 0  # exact: (1.4/1.0 - 1)
    assert rows[0]["pnl_usd"] == 4.0
    assert rows[0]["hold_minutes"] == 42.0


def test_record_exit_handles_zero_entry_price(tmp_path):
    path = tmp_path / "journal.jsonl"
    tj.record_exit(path, symbol="ZERO", token_class="meme", entry_price=0.0, exit_price=1.0,
                   usd_size=5.0, hold_minutes=1.0, reason="x", backend="pancake", tx=None,
                   time_iso="2026-07-02T00:00:00Z")
    rows = tj.read_all(path)
    assert rows[0]["pnl_pct"] == 0.0 and rows[0]["pnl_usd"] == 0.0


def test_read_all_returns_empty_list_for_missing_file(tmp_path):
    assert tj.read_all(tmp_path / "does_not_exist.jsonl") == []


def test_read_all_skips_malformed_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"event": "entry", "symbol": "OK"}\nnot json\n\n', encoding="utf-8")
    rows = tj.read_all(path)
    assert rows == [{"event": "entry", "symbol": "OK"}]


def test_report_computes_win_rate_and_pnl():
    path_rows = [
        {"event": "exit", "pnl_usd": 2.0, "pnl_pct": 0.20},
        {"event": "exit", "pnl_usd": -1.0, "pnl_pct": -0.05},
        {"event": "entry", "symbol": "IGNORED"},   # entries are not counted in win-rate
        {"event": "exit", "pnl_usd": 3.0, "pnl_pct": 0.30},
    ]
    import src.agent.aegis.trade_journal as tj_mod
    orig = tj_mod.read_all
    tj_mod.read_all = lambda path: path_rows
    try:
        rep = tj.report("unused-path")
    finally:
        tj_mod.read_all = orig
    assert rep["n_trades"] == 3
    assert rep["win_rate"] == 2 / 3
    assert rep["total_pnl_usd"] == 4.0
    assert rep["worst_pnl_pct"] == -0.05


def test_report_empty_journal():
    import src.agent.aegis.trade_journal as tj_mod
    orig = tj_mod.read_all
    tj_mod.read_all = lambda path: []
    try:
        rep = tj.report("unused-path")
    finally:
        tj_mod.read_all = orig
    assert rep == {"n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
                   "worst_pnl_pct": None, "total_pnl_usd": 0.0}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -q tests/test_trade_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.agent.aegis.trade_journal'`.

- [ ] **Step 3: Write the module**

Create `src/agent/aegis/trade_journal.py`:

```python
"""Append-only trade journal — one JSON line per executed (real, non-simulated) fill.

Purpose: give the soak-test pass bar (win-rate, per-trade stop discipline) an actual
data source. Pure I/O helpers only — no chain/network, no strategy logic. Every
write is fail-safe from the CALLER's perspective (this module raises on a genuine
disk error, but the caller in agent_loop.py wraps every call so a journal-write
hiccup never breaks a trading tick).
"""
from __future__ import annotations

import json
from pathlib import Path


def record_entry(path: Path, *, symbol: str, token_class: str, entry_price: float,
                 usd_size: float, reason: str, backend: str, tx: str | None,
                 time_iso: str) -> None:
    _append(path, {
        "event": "entry", "time": time_iso, "symbol": symbol, "token_class": token_class,
        "entry_price": entry_price, "usd_size": usd_size, "reason": reason,
        "backend": backend, "tx": tx,
    })


def record_exit(path: Path, *, symbol: str, token_class: str, entry_price: float,
                exit_price: float, usd_size: float, hold_minutes: float, reason: str,
                backend: str, tx: str | None, time_iso: str) -> None:
    pnl_pct = (exit_price / entry_price - 1.0) if entry_price > 0 else 0.0
    pnl_usd = usd_size * pnl_pct
    _append(path, {
        "event": "exit", "time": time_iso, "symbol": symbol, "token_class": token_class,
        "entry_price": entry_price, "exit_price": exit_price, "usd_size": usd_size,
        "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "hold_minutes": hold_minutes,
        "reason": reason, "backend": backend, "tx": tx,
    })


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_all(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def report(path: Path) -> dict:
    """Win-rate / PnL summary over every recorded EXIT (entries aren't counted —
    a trade's outcome is only known once it closes)."""
    rows = [r for r in read_all(path) if r.get("event") == "exit"]
    n = len(rows)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
                "worst_pnl_pct": None, "total_pnl_usd": 0.0}
    wins = [r for r in rows if r.get("pnl_usd", 0.0) > 0]
    pnl_pcts = [r.get("pnl_pct", 0.0) for r in rows]
    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "avg_pnl_pct": sum(pnl_pcts) / n,
        "worst_pnl_pct": min(pnl_pcts),
        "total_pnl_usd": sum(r.get("pnl_usd", 0.0) for r in rows),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_trade_journal.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/agent/aegis/trade_journal.py tests/test_trade_journal.py
git add src/agent/aegis/trade_journal.py tests/test_trade_journal.py
git commit -m "Add standalone trade_journal module (entry/exit/PnL, JSON Lines)"
```

---

### Task 6: Wire the trade journal into the live tick

**Files:**
- Modify: `src/agent/agent_loop.py` — add `TRADE_JOURNAL_FILE` constant (near line 41),
  `_event_decision()` (closed-position diff, currently lines 634-751), `tick()` (currently
  lines 822-941, the `_event_decision()` call and `_execute()` call), `_execute()` (currently
  lines 1047-1104, journal-write calls on success)

**Interfaces:**
- Consumes: `trade_journal.record_entry`/`record_exit` (Task 5); `OpenPosition` (existing,
  `src/agent/aegis/positions.py` — fields `symbol`, `contract`, `entry_price`, `usd_size`,
  `entry_time`, `peak_price`, `entry_baseline_vol`, `token_class`); `token_list.token_class(sym)`
  (existing).
- Produces: `_event_decision(...)` now returns a 4-tuple `(orders, label, scan_rows, closed)`
  where `closed: dict[str, OpenPosition]`; `_execute(orders, prices, dry_run, trade_counter,
  now, closed=None)` — new optional 6th positional-or-keyword param, defaults to `{}` so
  `flatten_to_cash()`'s existing call (no journal context available there) is unaffected.

- [ ] **Step 1: Write the failing test for the closed-position diff in `_event_decision`**

`_event_decision` has no direct unit tests today (it's exercised through `tick()`). Rather than
adding a heavy integration test here, verify this piece through the `_execute()`-level tests in
Step 5 below, which exercise the actual journal-write behavior end to end. Skip ahead to Step 2.

- [ ] **Step 2: Add the `TRADE_JOURNAL_FILE` constant**

In `src/agent/agent_loop.py`, find (after Task 4's edit):

```python
COOLDOWN_FILE = RUNTIME / "aegis_cooldown.json"
ENTRY_FAIL_COOLDOWN_FILE = RUNTIME / "aegis_entry_fail_cooldown.json"
```

Add right after it:

```python
COOLDOWN_FILE = RUNTIME / "aegis_cooldown.json"
ENTRY_FAIL_COOLDOWN_FILE = RUNTIME / "aegis_entry_fail_cooldown.json"
TRADE_JOURNAL_FILE = RUNTIME / "trade_journal.jsonl"
```

- [ ] **Step 3: Add the before/after position snapshot + diff in `_event_decision()`**

In `src/agent/agent_loop.py`, find this line inside `_event_decision()` (currently around line 650):

```python
    book = PositionBook.load(POSITIONS_FILE)
```

Replace it with:

```python
    book = PositionBook.load(POSITIONS_FILE)
    positions_before = dict(book.positions)   # snapshot for the closed-position diff below
```

Then find the return statement at the end of the function (currently line 751):

```python
    return beta_orders + orders, label, _scan_rows(feed.last_snapshots)
```

Replace it with:

```python
    # Every position present before this tick and gone now was closed THIS tick, by
    # whichever of the 11 exit branches in decide_exits() fired — a before/after diff
    # avoids touching any of those call sites just to capture PnL context (2/7).
    closed = {sym: pos for sym, pos in positions_before.items() if sym not in book.positions}
    return beta_orders + orders, label, _scan_rows(feed.last_snapshots), closed
```

- [ ] **Step 4: Update `tick()` to unpack the 4th value and pass it to `_execute()`**

In `src/agent/agent_loop.py`, find this block inside `tick()` (currently lines 892-898):

```python
    elif event_mode:
        # Layer B primary (event radar) with Layer A (eligible basket) fallback.
        # daily_halt blocks new entries at the SOURCE (no phantom book positions).
        orders, mode, scan_rows = _event_decision(
            state, prices, symbols, block_entries=daily_halt,
            our_return=pnl.cumulative_return(_baseline_equity(equity), equity),
            current_dd=drawdown.current_drawdown())
```

Replace it with:

```python
    elif event_mode:
        # Layer B primary (event radar) with Layer A (eligible basket) fallback.
        # daily_halt blocks new entries at the SOURCE (no phantom book positions).
        orders, mode, scan_rows, closed_positions = _event_decision(
            state, prices, symbols, block_entries=daily_halt,
            our_return=pnl.cumulative_return(_baseline_equity(equity), equity),
            current_dd=drawdown.current_drawdown())
```

Leave the baseline (`else`, non-event-mode) branch completely untouched — it doesn't use the
position book, so it never needs `closed_positions`.

Now find the top of this same `if/elif/else` block, right before `action.derisk` (currently
lines 886-891):

```python
    scan_rows: list[dict] = []
    if action.derisk:
        orders = rebalance_strategy.derisk_orders(state)
        mode = "derisk"
        if event_mode:
            _clear_position_book()          # flatten simulated event positions too
```

Replace it with (declares ONE default, before the if/elif/else — the event-mode branch's
`orders, mode, scan_rows, closed_positions = ...` line from earlier in this step overwrites it
when that branch runs; the `derisk` and baseline branches leave it at `{}`, which is correct —
neither of them produces exits with recoverable entry-price context):

```python
    scan_rows: list[dict] = []
    closed_positions: dict = {}
    if action.derisk:
        orders = rebalance_strategy.derisk_orders(state)
        mode = "derisk"
        if event_mode:
            _clear_position_book()          # flatten simulated event positions too
```

Finally, find the `_execute()` call (currently line 932):

```python
    results = _execute(orders, prices, dry_run, trade_counter, now)
```

Replace it with:

```python
    results = _execute(orders, prices, dry_run, trade_counter, now, closed=closed_positions)
```

- [ ] **Step 5: Write the failing tests for journal writes in `_execute()`**

In `tests/test_agent_loop.py`, add after `test_execute_does_not_record_cooldown_for_failed_exit`:

```python
def test_execute_journals_successful_entry(tmp_path, mocker):
    mocker.patch.object(al, "TRADE_JOURNAL_FILE", tmp_path / "journal.jsonl")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    primary = _FakeDex(tx="0xentry")
    _patch_backends(mocker, {"1inch": primary, "openocean": _FakeDex(), "pancake": _FakeDex()})
    now = al.utcnow()
    al._execute([_entry_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=now)
    from src.agent.aegis import trade_journal
    rows = trade_journal.read_all(al.TRADE_JOURNAL_FILE)
    assert len(rows) == 1
    assert rows[0]["event"] == "entry" and rows[0]["symbol"] == "BAS"
    assert rows[0]["usd_size"] == 5.0 and rows[0]["tx"] == "0xentry"


def test_execute_journals_successful_exit_with_pnl(tmp_path, mocker):
    from src.agent.aegis.positions import OpenPosition
    mocker.patch.object(al, "TRADE_JOURNAL_FILE", tmp_path / "journal.jsonl")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    primary = _FakeDex(tx="0xexit")
    _patch_backends(mocker, {"1inch": primary, "openocean": _FakeDex(), "pancake": _FakeDex()})
    now = al.utcnow()
    closed = {"BAS": OpenPosition(symbol="BAS", contract="0xbas", entry_price=0.02,
                                  usd_size=4.0, entry_time=now.timestamp() - 600,
                                  token_class="meme")}
    al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(),
               now=now, closed=closed)
    from src.agent.aegis import trade_journal
    rows = trade_journal.read_all(al.TRADE_JOURNAL_FILE)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "exit" and row["symbol"] == "BAS"
    assert row["entry_price"] == 0.02 and row["exit_price"] == 0.03   # _PRICES["BAS"] = 0.03
    assert row["pnl_pct"] == pytest.approx(0.5)          # (0.03/0.02 - 1)
    assert row["pnl_usd"] == pytest.approx(2.0)           # 4.0 * 0.5
    assert row["hold_minutes"] == pytest.approx(10.0, abs=0.1)


def test_execute_skips_exit_journal_when_no_closed_context(tmp_path, mocker):
    # flatten_to_cash() has no `closed` map — must not crash, must not fabricate PnL.
    mocker.patch.object(al, "TRADE_JOURNAL_FILE", tmp_path / "journal.jsonl")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    _patch_backends(mocker, {"1inch": _FakeDex(tx="0xnoctx"), "openocean": _FakeDex(),
                             "pancake": _FakeDex()})
    now = al.utcnow()
    al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=now)
    from src.agent.aegis import trade_journal
    assert trade_journal.read_all(al.TRADE_JOURNAL_FILE) == []


def test_execute_does_not_journal_simulated_swaps(tmp_path, mocker):
    mocker.patch.object(al, "TRADE_JOURNAL_FILE", tmp_path / "journal.jsonl")
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    _patch_backends(mocker, {"1inch": _FakeDex(simulated=True, tx="0xsim"),
                             "openocean": _FakeDex(), "pancake": _FakeDex()})
    now = al.utcnow()
    al._execute([_entry_order()], _PRICES, dry_run=True, trade_counter=mocker.Mock(), now=now)
    from src.agent.aegis import trade_journal
    assert trade_journal.read_all(al.TRADE_JOURNAL_FILE) == []
```

`pytest` needs to already be imported at the top of `tests/test_agent_loop.py` — it was added
in an earlier session's `test_rehydrate_discovered_from_book_restores_held_position` work
(`import pytest` near the top of the file). Confirm this import is present; if not, add
`import pytest` alongside the existing imports.

- [ ] **Step 6: Run them to verify they fail**

Run: `python -m pytest -q tests/test_agent_loop.py -k "journals or skips_exit_journal or does_not_journal_simulated" -v`
Expected: all FAIL — no journal writes happen yet (`_execute()` doesn't call `trade_journal` at all).

- [ ] **Step 7: Wire the journal writes into `_execute()`**

In `src/agent/agent_loop.py`, replace the current `_execute()` function signature and success
branch. Find (currently lines 1047-1096, the part up to and including the successful-swap `break`):

```python
def _execute(orders, prices, dry_run, trade_counter, now) -> list[dict]:
    if not orders:
        return []
    configured = settings.execution_backend
    executors: dict[str, object] = {}                       # built lazily, reused this tick

    def _dex(backend):
        if backend not in executors:
            executors[backend] = _make_executor_for(backend, dry_run)
        return executors[backend]

    results = []
    for o in orders:
        amount_in = _amount_in_tokens(o, prices)
        if amount_in <= 0:
            continue
        is_exit = o.token_out in STABLECOINS              # selling to stable = closing a position
        if configured == "twak":
            backends = ["twak"]                            # separate wallet → never crosses over
        else:
            # Flexible venue selection (2/7, user call): quote every aggregator LIVE
            # for THIS token/size and prefer whichever has the best liquidity right
            # now, instead of a fixed configured primary. Falls back to the configured
            # backend if every live quote fails (network hiccup, no route yet).
            from .execution import best_execution
            ranked = best_execution.rank_backends(
                {b: _dex(b) for b in _ROUTABLE_BACKENDS}, o.token_in, o.token_out, amount_in)
            best = ranked[0] if ranked else configured
            if is_exit:
                # EXIT is non-negotiable: fail over through the rest of the ranking,
                # then any backend the live ranking couldn't quote, as a last resort.
                rest = ranked[1:] + [b for b in _ROUTABLE_BACKENDS if b != best and b not in ranked]
                backends = [best, *rest]
            else:
                backends = [best]        # ENTRY: best-liquidity venue only, no failover (price discipline)
        last_err = None
        for attempt, backend in enumerate(backends):
            try:
                r = _dex(backend).swap(o.token_in, o.token_out, amount_in)
                if not r.simulated:
                    trade_counter.record_trade(now)
                row = {"order": o.reason, "simulated": r.simulated, "tx": r.tx_hash,
                       "token_in": o.token_in, "token_out": o.token_out,
                       "amount_usd": o.amount_in_usd, "backend": backend}
                if attempt > 0:                            # the top choice had failed → record the save
                    row["failover_backend"] = backend
                    log.warning("swap_failover_ok", reason=o.reason, backend=backend)
                results.append(row)
                break
```

with:

```python
def _execute(orders, prices, dry_run, trade_counter, now, closed: dict | None = None) -> list[dict]:
    if not orders:
        return []
    closed = closed or {}
    configured = settings.execution_backend
    executors: dict[str, object] = {}                       # built lazily, reused this tick

    def _dex(backend):
        if backend not in executors:
            executors[backend] = _make_executor_for(backend, dry_run)
        return executors[backend]

    results = []
    for o in orders:
        amount_in = _amount_in_tokens(o, prices)
        if amount_in <= 0:
            continue
        is_exit = o.token_out in STABLECOINS              # selling to stable = closing a position
        if configured == "twak":
            backends = ["twak"]                            # separate wallet → never crosses over
        else:
            # Flexible venue selection (2/7, user call): quote every aggregator LIVE
            # for THIS token/size and prefer whichever has the best liquidity right
            # now, instead of a fixed configured primary. Falls back to the configured
            # backend if every live quote fails (network hiccup, no route yet).
            from .execution import best_execution
            ranked = best_execution.rank_backends(
                {b: _dex(b) for b in _ROUTABLE_BACKENDS}, o.token_in, o.token_out, amount_in)
            best = ranked[0] if ranked else configured
            if is_exit:
                # EXIT is non-negotiable: fail over through the rest of the ranking,
                # then any backend the live ranking couldn't quote, as a last resort.
                rest = ranked[1:] + [b for b in _ROUTABLE_BACKENDS if b != best and b not in ranked]
                backends = [best, *rest]
            else:
                backends = [best]        # ENTRY: best-liquidity venue only, no failover (price discipline)
        last_err = None
        for attempt, backend in enumerate(backends):
            try:
                r = _dex(backend).swap(o.token_in, o.token_out, amount_in)
                if not r.simulated:
                    trade_counter.record_trade(now)
                    _journal_trade(o, is_exit, closed, prices, backend, r.tx_hash, now)
                row = {"order": o.reason, "simulated": r.simulated, "tx": r.tx_hash,
                       "token_in": o.token_in, "token_out": o.token_out,
                       "amount_usd": o.amount_in_usd, "backend": backend}
                if attempt > 0:                            # the top choice had failed → record the save
                    row["failover_backend"] = backend
                    log.warning("swap_failover_ok", reason=o.reason, backend=backend)
                results.append(row)
                break
```

Leave the rest of `_execute()` (the `except`/`else` blocks) unchanged from Task 4's edit.

Now add the `_journal_trade` helper right after `_execute()` (before `_alert_exit_failure` /
`_record_entry_fail_cooldown`, wherever those currently sit):

```python
def _journal_trade(o, is_exit: bool, closed: dict, prices: dict, backend: str,
                   tx: str | None, now) -> None:
    """Best-effort trade-journal write for a REAL (already-confirmed-live) fill.
    Never raises into the tick — a journal-write hiccup must not affect trading."""
    try:
        from .aegis import trade_journal
        time_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(now, "strftime") else ""
        if is_exit:
            pos = closed.get(o.token_in)
            if pos is None:      # no entry context available (e.g. flatten_to_cash) — skip
                return
            exit_price = prices.get(o.token_in, 0.0)
            hold_minutes = (now.timestamp() - pos.entry_time) / 60.0 if hasattr(now, "timestamp") else 0.0
            trade_journal.record_exit(
                TRADE_JOURNAL_FILE, symbol=o.token_in, token_class=pos.token_class,
                entry_price=pos.entry_price, exit_price=exit_price, usd_size=pos.usd_size,
                hold_minutes=hold_minutes, reason=o.reason, backend=backend, tx=tx,
                time_iso=time_iso)
        else:
            entry_price = prices.get(o.token_out, 0.0)
            trade_journal.record_entry(
                TRADE_JOURNAL_FILE, symbol=o.token_out, token_class=token_list.token_class(o.token_out),
                entry_price=entry_price, usd_size=o.amount_in_usd, reason=o.reason,
                backend=backend, tx=tx, time_iso=time_iso)
    except Exception as e:  # noqa: BLE001 — a journal-write hiccup must never break the tick
        log.warning("trade_journal_write_failed", symbol=o.token_out if not is_exit else o.token_in,
                   error=type(e).__name__)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_agent_loop.py -k "journals or skips_exit_journal or does_not_journal_simulated" -v`
Expected: all 4 PASS.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass — this is the highest-blast-radius task in the plan (touches `tick()`,
`_event_decision()`, `_execute()`), so a clean full-suite run here matters most.

- [ ] **Step 10: Lint + commit**

```bash
ruff check src/agent/agent_loop.py tests/test_agent_loop.py
git add src/agent/agent_loop.py tests/test_agent_loop.py
git commit -m "Wire trade journal into the live tick (closed-position diff, entry/exit writes)"
```

---

### Task 7: Real volume gate for hot-token candidates

**Files:**
- Modify: `src/agent/aegis/volume_breakout.py:116-144` (`hot_token_signals`)
- Modify: `src/agent/aegis/sniper.py` (the `hot_token_signals(...)` call, currently lines 111-114)
- Modify: `src/agent/agent_loop.py` — new `_w3w_hot_token_volume_data()` helper, wire into
  `_event_decision()` and the `sniper.run()` call
- Test: `tests/test_volume_breakout.py`, `tests/test_sniper.py`, `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `binance_web3.price_info(contracts: list[str]) -> dict[str, dict]` (existing,
  entries have `volume5M`, `volume1H` string-like float fields — confirmed live 2026-07-02).
- Produces: `hot_token_signals(items, *, breakout_min, breakout_max, allow=None, vol_mult=0.0,
  price_info_by_contract=None) -> list[BreakoutSignal]` — 2 new OPTIONAL kwargs, backward
  compatible (every existing call/test omits them → `vol_mult=0.0` disables the new gate
  entirely, identical to today's `vol_multiple=0.0` hardcode).

- [ ] **Step 1: Write the failing tests for `hot_token_signals`' real volume gate**

In `tests/test_volume_breakout.py`, add after `test_hot_token_signals_allow_filter`:

```python
def test_hot_token_signals_computes_real_vol_multiple(mocker):
    items = [_hot_item("MYX", change=8.0, volume=5000.0, liquidity=20000.0, contract="0xmyx")]
    # volume1H=1200 -> 5m-equivalent baseline = 1200/12 = 100; volume5M=600 -> 6x baseline.
    price_info = {"0xmyx": {"volume5M": "600", "volume1H": "1200"}}
    sigs = hot_token_signals(items, breakout_min=0.06, breakout_max=0.20,
                             vol_mult=4.0, price_info_by_contract=price_info)
    assert len(sigs) == 1
    assert sigs[0].vol_multiple == pytest.approx(6.0)


def test_hot_token_signals_rejects_below_real_volume_bar():
    items = [_hot_item("WEAKVOL", change=8.0, volume=5000.0, liquidity=20000.0, contract="0xweakvol")]
    # volume1H=1200 -> baseline 100; volume5M=200 -> only 2x, below the 4x meme bar.
    price_info = {"0xweakvol": {"volume5M": "200", "volume1H": "1200"}}
    sigs = hot_token_signals(items, breakout_min=0.06, breakout_max=0.20,
                             vol_mult=4.0, price_info_by_contract=price_info)
    assert sigs == []


def test_hot_token_signals_rejects_zero_baseline_volume():
    # This is exactly the SPCX shape: real activity happened, but the rolling 1h window
    # shows nothing right now (fail-safe: no real baseline, never fire).
    items = [_hot_item("DEADVOL", change=14.3, volume=1997.0, liquidity=1.0, contract="0xdeadvol")]
    price_info = {"0xdeadvol": {"volume5M": "0", "volume1H": "0"}}
    sigs = hot_token_signals(items, breakout_min=0.06, breakout_max=0.20,
                             vol_mult=4.0, price_info_by_contract=price_info)
    assert sigs == []


def test_hot_token_signals_missing_price_info_entry_rejected_when_gate_active():
    items = [_hot_item("NODATA", change=8.0, volume=5000.0, liquidity=20000.0, contract="0xnodata")]
    sigs = hot_token_signals(items, breakout_min=0.06, breakout_max=0.20,
                             vol_mult=4.0, price_info_by_contract={})   # no entry for 0xnodata
    assert sigs == []


def test_hot_token_signals_no_gate_when_vol_mult_zero_matches_old_behavior():
    # Backward compatibility: omitting the new kwargs (or vol_mult=0.0) behaves exactly
    # like before this task — every existing caller/test relies on this.
    items = [_hot_item("MYX", change=8.0, volume=5000.0, liquidity=20000.0, contract="0xmyx")]
    sigs = hot_token_signals(items, breakout_min=0.06, breakout_max=0.20)
    assert len(sigs) == 1 and sigs[0].vol_multiple == 0.0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -q tests/test_volume_breakout.py -k "real_vol_multiple or below_real_volume_bar or zero_baseline_volume or missing_price_info_entry" -v`
Expected: all FAIL — `TypeError: hot_token_signals() got an unexpected keyword argument
'vol_mult'` (the new kwargs don't exist yet).

- [ ] **Step 3: Update `hot_token_signals()`**

In `src/agent/aegis/volume_breakout.py`, replace the current function (lines 116-144):

```python
def hot_token_signals(items: list[dict], *, breakout_min: float, breakout_max: float,
                      allow: Callable[[str], bool] | None = None) -> list[BreakoutSignal]:
    """Convert Binance's server-side-filtered hot-token results (Option B discovery)
    into ranked BreakoutSignals — a PURE conversion, the caller already fetched
    `items` via `binance_web3.hot_token(...)`. Wash-trading/mint/freeze exclusion and
    the price-change FLOOR already happened server-side; this only applies the
    chase-cap ceiling (hot-token has no upper price-change filter) and ranks by
    the $ volume hot-token reports (there is no baseline-multiple concept here)."""
    out: list[BreakoutSignal] = []
    for it in items:
        contract = (it.get("tokenContractAddress") or "").strip().lower()
        if not contract or (allow and not allow(contract)):
            continue
        try:
            change = float(it.get("change")) / 100.0
            volume = float(it.get("volume"))
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if change <= 0 or change < breakout_min or change > breakout_max:
            continue
        out.append(BreakoutSignal(
            symbol=it.get("tokenSymbol") or contract, contract=contract,
            vol_multiple=0.0, breakout_pct=change, recent_pump_pct=0.0, slippage_est=0.0,
            price_now=price, baseline_vol=volume,
            reasons=(f"hot-token +{change * 100:.1f}% vol=${volume:,.0f}",),
        ))
    out.sort(key=lambda s: s.baseline_vol, reverse=True)
    return out
```

with:

```python
def hot_token_signals(items: list[dict], *, breakout_min: float, breakout_max: float,
                      allow: Callable[[str], bool] | None = None,
                      vol_mult: float = 0.0,
                      price_info_by_contract: dict[str, dict] | None = None) -> list[BreakoutSignal]:
    """Convert Binance's server-side-filtered hot-token results (Option B discovery)
    into ranked BreakoutSignals — a PURE conversion, the caller already fetched
    `items` via `binance_web3.hot_token(...)`. Wash-trading/mint/freeze exclusion and
    the price-change FLOOR already happened server-side; this applies the chase-cap
    ceiling (hot-token has no upper price-change filter) and ranks by the $ volume
    hot-token reports.

    Real volume confirmation (2/7, post-SPCX hardening): if `price_info_by_contract`
    is supplied (a batch `binance_web3.price_info()` result keyed by lowercase
    contract) AND `vol_mult > 0`, a candidate must show `volume5M >= vol_mult x
    baseline` to pass — baseline is approximated as `volume1H / 12` (a rough "average
    5-minute bucket over the last hour"; this is contaminated by the spike itself
    since price_info's rolling window includes the current moment, so it under-states
    a true pre-spike baseline — still a real improvement over the previous hardcoded
    vol_multiple=0.0, which applied NO volume confirmation at all). Omitting these
    kwargs (the default) reproduces the OLD behavior exactly, for backward compat."""
    gate_active = vol_mult > 0 and price_info_by_contract is not None
    out: list[BreakoutSignal] = []
    for it in items:
        contract = (it.get("tokenContractAddress") or "").strip().lower()
        if not contract or (allow and not allow(contract)):
            continue
        try:
            change = float(it.get("change")) / 100.0
            volume = float(it.get("volume"))
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if change <= 0 or change < breakout_min or change > breakout_max:
            continue
        vol_multiple = 0.0
        if gate_active:
            info = price_info_by_contract.get(contract)
            if not info:
                continue                                  # no real volume data -> never fire
            try:
                vol_5m = float(info.get("volume5M") or 0)
                baseline = float(info.get("volume1H") or 0) / 12.0
            except (TypeError, ValueError):
                continue
            if baseline <= 0 or vol_5m < vol_mult * baseline:
                continue                                  # fails the REAL volume-confirmation bar
            vol_multiple = vol_5m / baseline
        out.append(BreakoutSignal(
            symbol=it.get("tokenSymbol") or contract, contract=contract,
            vol_multiple=vol_multiple, breakout_pct=change, recent_pump_pct=0.0, slippage_est=0.0,
            price_now=price, baseline_vol=volume,
            reasons=(f"hot-token +{change * 100:.1f}% vol=${volume:,.0f}",),
        ))
    out.sort(key=lambda s: s.baseline_vol, reverse=True)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_volume_breakout.py -k "real_vol_multiple or below_real_volume_bar or zero_baseline_volume or missing_price_info_entry or no_gate_when_vol_mult_zero" -v`
Expected: all 5 PASS.

- [ ] **Step 5: Run the full `hot_token_signals` block + full volume_breakout suite**

Run: `python -m pytest -q tests/test_volume_breakout.py`
Expected: all pass (existing 7 hot-token tests untouched + 5 new ones).

- [ ] **Step 6: Write the failing test for `sniper.run` passing volume data through**

In `tests/test_sniper.py`, add after `test_no_entry_fail_cooldown_param_behaves_as_before`:

```python
def test_hot_token_volume_gate_rejects_weak_candidate():
    items = [_hot_item("WEAK", change=8.0, volume=9000.0, contract="0xweak")]
    weak_volume = {"0xweak": {"volume5M": "50", "volume1H": "1200"}}   # baseline 100, only 0.5x
    orders, _ = sniper.run(
        _state(), {"WEAK": 1.0}, book=PositionBook(), feed=FakeFeed({}), cooldowns=CooldownBook(),
        regime_flag=Regime.RISK_ON, universe=[], now=1000.0, floor_usd=6.0, allow=_allow,
        hot_token_items=items, hot_token_volume=weak_volume)
    assert orders == []


def test_hot_token_volume_gate_admits_strong_candidate():
    items = [_hot_item("STRONG", change=8.0, volume=9000.0, contract="0xstrong")]
    strong_volume = {"0xstrong": {"volume5M": "600", "volume1H": "1200"}}   # baseline 100, 6x
    orders, _ = sniper.run(
        _state(), {"STRONG": 1.0}, book=PositionBook(), feed=FakeFeed({}), cooldowns=CooldownBook(),
        regime_flag=Regime.RISK_ON, universe=[], now=1000.0, floor_usd=6.0, allow=_allow,
        hot_token_items=items, hot_token_volume=strong_volume)
    assert len(orders) == 1 and orders[0].token_out == "STRONG"
```

- [ ] **Step 7: Run them to verify they fail**

Run: `python -m pytest -q tests/test_sniper.py -k hot_token_volume_gate -v`
Expected: both FAIL with `TypeError: run() got an unexpected keyword argument
'hot_token_volume'` (the kwarg doesn't exist on `sniper.run()` yet).

- [ ] **Step 8: Wire `sniper.run()` to accept and pass `hot_token_volume`**

In `src/agent/aegis/sniper.py`, add a new optional parameter to `run()`'s signature. Find
(after Task 4's edit, the signature now ends with `entry_fail_cooldown_s`):

```python
        entry_fail_cooldowns: CooldownBook | None = None,
        entry_fail_cooldown_s: float | None = None,
        ) -> tuple[list[TradeOrder], str]:
```

Replace it with:

```python
        entry_fail_cooldowns: CooldownBook | None = None,
        entry_fail_cooldown_s: float | None = None,
        hot_token_volume: dict[str, dict] | None = None,
        ) -> tuple[list[TradeOrder], str]:
```

Then find the `hot_token_signals(...)` call (currently lines 111-114):

```python
            if manage_classes is None or tc.MEME in manage_classes:
                mp = tc.params(tc.MEME)
                sigs += hot_token_signals(hot_token_items, breakout_min=mp.breakout_min,
                                          breakout_max=mp.breakout_max)
```

Replace it with:

```python
            if manage_classes is None or tc.MEME in manage_classes:
                mp = tc.params(tc.MEME)
                sigs += hot_token_signals(hot_token_items, breakout_min=mp.breakout_min,
                                          breakout_max=mp.breakout_max, vol_mult=mp.vol_mult,
                                          price_info_by_contract=hot_token_volume)
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_sniper.py -k hot_token_volume_gate -v`
Expected: both PASS.

- [ ] **Step 10: Run the full sniper suite**

Run: `python -m pytest -q tests/test_sniper.py`
Expected: all pass (23 total: original 16 + 3 entry-fail-cooldown + 2 hot-token-volume-gate +
1 SPCX regression = matches running count from earlier tasks).

- [ ] **Step 11: Write the failing tests for `_w3w_hot_token_volume_data`**

In `tests/test_agent_loop.py`, add after the last `_w3w_hot_token_items` test:

```python
def test_hot_token_volume_data_batches_price_info(mocker):
    items = [{"tokenContractAddress": "0xaaa"}, {"tokenContractAddress": "0xbbb"}]
    pi = mocker.patch("src.agent.execution.binance_web3.price_info",
                      return_value={"0xaaa": {"volume5M": "1"}})
    out = al._w3w_hot_token_volume_data(items)
    assert out == {"0xaaa": {"volume5M": "1"}}
    assert pi.call_args.args[0] == ["0xaaa", "0xbbb"]


def test_hot_token_volume_data_empty_items_returns_empty():
    assert al._w3w_hot_token_volume_data(None) == {}
    assert al._w3w_hot_token_volume_data([]) == {}


def test_hot_token_volume_data_network_error_returns_empty(mocker):
    mocker.patch("src.agent.execution.binance_web3.price_info", side_effect=RuntimeError("boom"))
    assert al._w3w_hot_token_volume_data([{"tokenContractAddress": "0xaaa"}]) == {}
```

- [ ] **Step 12: Run them to verify they fail**

Run: `python -m pytest -q tests/test_agent_loop.py -k hot_token_volume_data -v`
Expected: all FAIL with `AttributeError: module 'src.agent.agent_loop' has no attribute
'_w3w_hot_token_volume_data'`.

- [ ] **Step 13: Add the `_w3w_hot_token_volume_data` helper and wire it into `_event_decision`**

In `src/agent/agent_loop.py`, add a new function right after `_w3w_hot_token_items()`:

```python
def _w3w_hot_token_volume_data(items: list[dict] | None) -> dict[str, dict]:
    """Batch price_info() for every hot-token candidate this tick, so
    hot_token_signals() can compute a REAL vol_multiple instead of the old
    hardcoded 0.0 (real-money bug, 2/7 — see the SPCX incident). {} on any
    failure or when there are no candidates — hot_token_signals() then rejects
    everything (fail-safe: no real volume data, never fire)."""
    if not items:
        return {}
    from .execution import binance_web3 as bw
    contracts = [it.get("tokenContractAddress") for it in items if it.get("tokenContractAddress")]
    if not contracts:
        return {}
    try:
        return bw.price_info(contracts)
    except Exception as e:  # noqa: BLE001 — a hiccup here must fall back, never break the tick
        log.warning("w3w_hot_token_volume_failed", error=type(e).__name__)
        return {}
```

Then in `_event_decision()`, find:

```python
    hot_token_items = _w3w_hot_token_items()
```

Replace it with:

```python
    hot_token_items = _w3w_hot_token_items()
    hot_token_volume = _w3w_hot_token_volume_data(hot_token_items)
```

Then find the `sniper.run(...)` call again (already modified in Task 4 to include
`entry_fail_cooldowns`/`entry_fail_cooldown_s`) and add the new kwarg:

```python
    orders, mode = sniper.run(sniper_state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=meme_flag, universe=symbols, now=now_ts, trending=trending,
                              max_meme_positions=meme_cap, meme_usd=meme_usd,
                              manage_classes=sniper_classes, allow=entry_allow,
                              hot_token_items=hot_token_items,
                              safety_check=_w3w_safety_check(state.equity_usd) if hot_token_items is not None else None,
                              entry_fail_cooldowns=entry_fail_cooldowns,
                              entry_fail_cooldown_s=settings.entry_fail_cooldown_seconds)
```

Replace it with:

```python
    orders, mode = sniper.run(sniper_state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=meme_flag, universe=symbols, now=now_ts, trending=trending,
                              max_meme_positions=meme_cap, meme_usd=meme_usd,
                              manage_classes=sniper_classes, allow=entry_allow,
                              hot_token_items=hot_token_items,
                              safety_check=_w3w_safety_check(state.equity_usd) if hot_token_items is not None else None,
                              entry_fail_cooldowns=entry_fail_cooldowns,
                              entry_fail_cooldown_s=settings.entry_fail_cooldown_seconds,
                              hot_token_volume=hot_token_volume)
```

- [ ] **Step 14: Run the tests to verify they pass**

Run: `python -m pytest -q tests/test_agent_loop.py -k hot_token_volume_data -v`
Expected: all 3 PASS.

- [ ] **Step 15: Run the FULL test suite**

Run: `python -m pytest -q`
Expected: all pass — this is the last and most integrative task, confirming everything from
Tasks 1-7 works together.

- [ ] **Step 16: Lint + commit**

```bash
ruff check src/agent/aegis/volume_breakout.py src/agent/aegis/sniper.py src/agent/agent_loop.py tests/test_volume_breakout.py tests/test_sniper.py tests/test_agent_loop.py
git add src/agent/aegis/volume_breakout.py src/agent/aegis/sniper.py src/agent/agent_loop.py tests/test_volume_breakout.py tests/test_sniper.py tests/test_agent_loop.py
git commit -m "Close the vol_multiple=0.0 hot-token bypass with a real volume-confirmation gate"
```

---

### Task 8: Deploy to the VPS and verify live

**Files:** none (deployment only)

**Interfaces:** none

- [ ] **Step 1: Push everything to `origin/main`**

```bash
git push origin main
```

- [ ] **Step 2: Pull and restart on the VPS**

```bash
ssh -i ~/.ssh/hostinger_openclaw root@187.127.188.62 "sudo -u agent bash -c 'cd /home/agent/Track1-trade-onchain && git pull --ff-only' && systemctl restart agent && sleep 3 && systemctl status agent --no-pager | head -8"
```

Expected: `Active: active (running)`, no errors in the last few status lines.

- [ ] **Step 3: Watch logs for a few ticks**

```bash
ssh -i ~/.ssh/hostinger_openclaw root@187.127.188.62 "sleep 65 && tail -n 40 /home/agent/Track1-trade-onchain/logs/agent.log"
```

Expected: no new tracebacks/exceptions; `tick` lines continue as before; if a hot-token
candidate is scanned this tick, look for `w3w_holders_too_low` / `w3w_liquidity_too_low` /
the volume-gate rejecting silently (no log line for a clean reject — that's expected, only
failures/blocks log) confirming the new gates are active without breaking the tick.

- [ ] **Step 4: Verify the trade journal file exists once any real trade fires**

```bash
ssh -i ~/.ssh/hostinger_openclaw root@187.127.188.62 "sudo -u agent bash -c 'cd /home/agent/Track1-trade-onchain && test -f data/runtime/trade_journal.jsonl && cat data/runtime/trade_journal.jsonl || echo \"no trades yet — file created on first real fill\"'"
```

Expected: either the journal file with valid JSON lines, or the "no trades yet" message (both
are fine — the file is created lazily on the first real trade).

- [ ] **Step 5: Update memory**

This is a real-money system with persistent memory at
`C:\Users\Ánh\.claude\projects\E--Track1-trade-onchain\memory\`. Write a new memory file
`post-spcx-hardening-deployed.md` (type: project) summarizing: what shipped (7 hardening
tasks), that it's live and verified, and the trade-journal file location for future sessions
evaluating the soak-test pass bar. Add one line to `MEMORY.md`'s index pointing to it.
