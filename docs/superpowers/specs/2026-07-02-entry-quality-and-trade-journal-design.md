# Design: Entry-quality gates + trade journal (post-SPCX-incident hardening)

**Date:** 2026-07-02
**Status:** Approved (brainstorm). Ready for implementation plan.
**Author:** Aegis / Track-1 trading agent

## 1. Motivation

Real incident (2026-07-02, see `[[spcx-position-tracking-incident]]`): the bot bought a real
~$5.10 of a meme token (SPCX) via the hot-token discovery path. The token had 10 holders and a
~$0.83-liquidity pool — a pool so thin that selling the position back would lose ~86% to price
impact. Root cause was a *tracking* bug (fixed same day, separate spec). This design fixes the
*entry-quality* gap that let a pool this thin qualify in the first place, plus the operational
and record-keeping gaps the incident exposed:

- The hot-token path's `BreakoutSignal` hardcodes `vol_multiple=0.0`, so the "money-flow
  confirmation" gate (`ClassParams.vol_mult` — 4x for MEME, 2x for MAJOR, both configured
  2026-07-02) **never actually applies** to a hot-token candidate. Only the price-change floor
  is checked. This is the single biggest hole: it lets a token in on price alone, with zero
  real volume confirmation, silently violating the entry rule the user believes is enforced.
- Binance's hot-token endpoint supports server-side `liquidityMin`, `volumeMin`, and
  `top10HoldingPercentMax` filters, none of which the bot currently passes — so a 10-holder,
  near-zero-liquidity token was never even filtered out upstream.
- The just-in-time safety check (`_w3w_safety_check`) verifies honeypot/tax/price-impact but
  has no absolute liquidity or holder-count floor of its own.
- A failed *entry* attempt (as opposed to an exit) is never cooled down — the log showed the
  bot retrying the same dead candidate every tick for 5+ minutes straight.
- `data/runtime/trades.json` records only a timestamp per trade — no entry/exit price, no PnL,
  no reason. The user has since set an explicit pass/fail bar for this soak-test phase
  (win-rate ≥ 40%, no trade loses beyond its own class's configured hard stop — 5% major / 6%
  meme — over the next 3 months, no repeat of an operational incident like this one) but there
  is currently no data source to evaluate that bar against.

## 2. Goals / Non-goals

**Goals**
- A hot-token candidate must pass a REAL 5-minute-volume-vs-baseline check at the class's
  configured multiple (4x meme / 2x major) before it can fire — closing the gap above.
- Binance's own server-side filters (liquidity, volume, top-10 holding %) are used to keep
  obviously-unsuitable candidates (near-zero liquidity, near-zero holders, concentrated
  ownership) out of the candidate list entirely, before any further checks run.
- The just-in-time safety check adds its own liquidity and holder-count floor, as a second
  layer independent of the server-side filter (in case liquidity moves between discovery and
  the actual entry decision).
- A token that fails to enter (every backend failed) goes on a short cooldown, so the bot
  doesn't burn ticks hammering a dead candidate.
- Every executed trade (entry and exit) is recorded with enough detail (price, size, reason,
  PnL) to evaluate the win-rate / stop-discipline bar the user set, at any point during the
  soak-test.

**Non-goals**
- No hard cap on trade count/cadence — the user explicitly wants entries driven purely by
  whether a candidate clears the (now-tighter) quality bar, not by an artificial throttle.
- No change to the exit rails themselves (TP/trail/hard-stop values) — those were just retuned
  2026-07-01/02 and are out of scope here.
- No change to `beta_core` (currently disabled) or the major-momentum mechanism.
- No dashboard/UI work — the trade journal is a data file for now; visualizing it is a
  separate, later concern if wanted.

## 3. Design

### 3.1 Server-side discovery filters (`agent_loop._w3w_hot_token_items`)

Three new config settings (all overridable via `.env`, same pattern as existing
`binance_w3w_*` settings in `config.py`):

```python
binance_w3w_min_liquidity_usd: float = 20_000.0   # hot-token liquidityMin
binance_w3w_min_volume_usd: float = 5_000.0       # hot-token volumeMin
binance_w3w_max_top10_holding_pct: float = 30.0   # hot-token top10HoldingPercentMax
```

`_w3w_hot_token_items()` passes these straight through to `binance_web3.hot_token()`'s
existing `liquidity_min`, `volume_min`, and `top10_holding_percent_max` keyword arguments
(already implemented in `binance_web3.py`, just never called with these params before).

### 3.2 Safety-check liquidity/holder floor (`agent_loop._w3w_safety_check`)

Two new config settings:

```python
binance_w3w_min_holders: int = 30            # reject a candidate with fewer holders than this
binance_w3w_min_liquidity_usd_check: float = 10_000.0  # second-layer liquidity floor (JIT quote time)
```

Confirmed: `quote()`'s response (`fromToken`/`toToken`/route data) does NOT carry `holders` or
`liquidity` — only `price_info()` does. So `check()` makes one additional `price_info([sig.contract])`
call (single-contract, still cheap) after the existing honeypot/tax/price-impact checks pass,
and reads `holders` (int) and `liquidity` (string, parse as float) from that response. Reject
and log (`w3w_holders_too_low`, `w3w_liquidity_too_low`) if either floor isn't met; if the
`price_info` call itself fails or returns nothing for the contract, fail closed (reject, same
policy as every other check in this function).

### 3.3 Entry-fail cooldown (`agent_loop._execute`)

When an **entry** order (buying a token, i.e. `token_in` is a stablecoin) fails on every
configured backend, record it into the existing `CooldownBook` via a new short-duration entry,
distinct from the exit cooldown:

```python
entry_fail_cooldown_seconds: int = 900   # 15 min — "couldn't get in", not "just got out"
```

`CooldownBook.cooling_down(now, cooldown_s)` applies ONE duration uniformly to every entry in
its dict — so entry-fail records (900s) can't share the same `CooldownBook`/file as exit
records (5400s) without corrupting one duration or the other. Decision: use a SECOND
`CooldownBook` instance with its own persisted file
(`data/runtime/aegis_entry_fail_cooldown.json`), reusing the existing class as-is (it's already
generic: symbol → last-touch-time, checked against a caller-supplied duration) — call its
`record_exit(symbol, now)` method to mark a failed-entry symbol (the method name is a slight
misnomer for this use, but the semantics — "don't touch this symbol again until N seconds pass"
— are identical; no change to `cooldown.py` needed). Its `cooling_down(now, 900)` result is
unioned into the same `cooldown_symbols` set already passed to `decide_breakout_entries`.

### 3.4 Real volume gate for hot-token candidates (`hot_token_signals`, `volume_breakout.py`)

Currently `hot_token_signals()` builds each `BreakoutSignal` with `vol_multiple=0.0` — a
hardcoded bypass of the class's volume-confirmation gate. Fix: before building a signal, fetch
`price_info()` for the candidate (batched across all hot-token candidates in one call, same
tick) and compute `vol_multiple = volume5M / baseline`, where baseline is approximated from
`price_info`'s `volume1H` or `volume4H` field (per the approved approach — real baseline data,
not a rough estimate from the discovery-time volume alone). Reject up front (never construct a
signal) if `baseline <= 0` (fail-safe, matches `scan_breakouts`'s existing "no real baseline,
never fire" philosophy) or downstream, once passed through as a real `vol_multiple`, let the
EXISTING `scan_breakouts`/class-param gate (`ClassParams.vol_mult`, unchanged: meme 4x / major
2x) reject it exactly as it already does for the classic client-side scan path.

This closes the gap without touching the class thresholds themselves — a hot-token candidate
now has to clear the SAME bar a client-side-scanned candidate always had to.

### 3.5 Trade journal (`agent_loop._execute`, new module `aegis/trade_journal.py`)

New append-only file `data/runtime/trade_journal.jsonl` (JSON Lines — one JSON object per
line, easy to append without re-parsing the whole file, easy to `tail`/grep for a quick check).

Two event shapes, written at the point a swap **actually executes successfully** (live, not
simulated) inside `_execute()`:

```jsonc
// entry
{"event": "entry", "time": "2026-07-02T00:16:19Z", "symbol": "SPCX", "token_class": "meme",
 "entry_price": 4.888e-06, "usd_size": 5.10, "reason": "breakout vol 0.0x +14.3%",
 "backend": "1inch", "tx": "0x8f67..."}

// exit
{"event": "exit", "time": "...", "symbol": "SPCX", "token_class": "meme",
 "exit_price": ..., "entry_price": ..., "usd_size": ..., "pnl_usd": ..., "pnl_pct": ...,
 "hold_minutes": ..., "reason": "aegis exit: hard stop -6.1%", "backend": "openocean", "tx": "0x..."}
```

`_execute()` already has everything needed for an ENTRY at the point it logs `swap_sent`:
`o.reason`, `o.token_in`/`token_out`, `o.amount_in_usd`, the executed backend, the tx hash, and
the entry price via `prices[o.token_out]`.

For an EXIT, the entry_price/entry_time needed for PnL live in the `OpenPosition` that gets
removed from the position book via `book.close(symbol)` (11 different call sites across
`edam.decide_exits()`, one per exit reason) — by the time `_execute()` runs, the book has
already been saved with the position gone. Decision: rather than touching any of those call
sites (in `decide_exits`, `sniper.run`, or a currently-disabled `beta_core`), do a simple
before/after DIFF at the single point that already owns the whole book for a tick —
`agent_loop._event_decision()` (the only caller of `sniper.run`, itself only called once, from
`tick()`). At the top of `_event_decision()`, right after `book = PositionBook.load(...)`,
snapshot `positions_before = dict(book.positions)` (a shallow copy — cheap, and the only field
mutated in place downstream is `peak_price`, which PnL doesn't need). At the end, right before
returning, compute `closed = {sym: pos for sym, pos in positions_before.items() if sym not in book.positions}`
— this is exactly the set of positions that were closed THIS tick, by any sleeve, for any
reason, no matter which of the 11 exit branches fired. Add `closed` as a 4th return value from
`_event_decision()` (its only caller, inside `tick()`, already destructures its tuple and is
the only place that needs updating). Zero changes to `sniper.py`, `decide_exits()`, or
`TradeOrder` — and zero test churn in `test_sniper.py`.

PnL is computed once both prices are known: `pnl_usd = usd_size * (exit_price/entry_price - 1)`
for a full-position exit (matches how this bot always exits — no partial scale-outs currently).

A small read-side helper (`trade_journal.report(since=...)`) can compute win-rate, avg
win/loss, max drawdown-per-trade, etc. on demand — useful for checking the soak-test bar
without hand-parsing JSONL, but is a nice-to-have for the plan to size appropriately (could be
a follow-up CLI command, e.g. `python -m src.agent journal-report`).

## 4. Config summary (new settings, `config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `binance_w3w_min_liquidity_usd` | 20,000 | hot-token server-side liquidityMin |
| `binance_w3w_min_volume_usd` | 5,000 | hot-token server-side volumeMin |
| `binance_w3w_max_top10_holding_pct` | 30.0 | hot-token server-side top10HoldingPercentMax |
| `binance_w3w_min_holders` | 30 | safety-check floor (JIT) |
| `binance_w3w_min_liquidity_usd_check` | 10,000 | safety-check floor (JIT, 2nd layer) |
| `entry_fail_cooldown_seconds` | 900 | cooldown after every backend fails an entry |

All defaults are starting points, tunable via `.env` without a redeploy — same pattern as
every other `binance_w3w_*` setting.

## 5. Testing plan

TDD per this project's existing convention (every module in scope already has a test file):
- `test_binance_web3.py`: `hot_token()` called with the 3 new filter kwargs.
- `test_agent_loop.py`: `_w3w_safety_check` rejects on low holders / low liquidity (mirroring
  the existing honeypot/tax/price-impact rejection tests); entry-fail records a cooldown.
- `test_volume_breakout.py` / `test_sniper.py`: `hot_token_signals()` (or its caller) computes
  a real `vol_multiple` from `price_info` data and the existing class-param gate correctly
  rejects a low-volume candidate that would have passed under the old `vol_multiple=0.0` bypass
  — this is the regression test proving the SPCX-shaped hole is closed.
- New `test_trade_journal.py`: entry/exit events written with correct shape; PnL math verified
  against known entry/exit prices; report/aggregation helper (if built) tested for win-rate math.

## 6. Rollout plan

Same pattern as every change this session: implement + test locally, `ruff check`, full suite
green, commit + push to `main`, `git pull --ff-only` + `systemctl restart agent` on the VPS,
watch `logs/agent.log` for a few ticks (confirm no new-code exceptions, confirm the new gates
actually fire in real log lines like `w3w_holders_too_low` when applicable), no separate
DRY_RUN staging step needed (matches this project's established pattern of going live directly
with tight monitoring — see `[[feedback-careful-real-money-code]]` for why extra post-deploy
verification specifically matters here).
