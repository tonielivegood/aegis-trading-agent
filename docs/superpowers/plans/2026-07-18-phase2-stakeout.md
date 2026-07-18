# Phase-2 Stakeout Implementation Plan (gem-hunt v4 entry architecture)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Written by a stronger model for execution by a smaller model.** Every design decision is made — do not re-litigate. Where a step says "verbatim", copy exactly. Task 1 is URGENT (every hour it's not deployed is lost training film) — execute and deploy it before touching Tasks 2-6.

**Goal:** Stop buying the pump we're detecting. On 2026-07-18 the bot lost -$6.15 in 4 trades because "3 wallets converged" *is* the pump — every entry paid a +31-50% lag premium on micro-caps and then reverted. This plan replaces reflex-buying with a **stakeout**: a signal ARMS a token dossier, the bot FILMS it (one metrics sample per 60s tick), and money moves only when ≥15-30 min of film shows a Phase-2 accumulation signature (price base + holder growth + dips absorbed + our wallets still holding + liquidity/concentration stable) at a non-chased price. Plus two independent safety layers (holder-concentration gate, daily-loss circuit breaker) and an audition upgrade (earliness metric).

**Architecture:** New `src/agent/copy_trade/watchlist.py` (Dossier store + pure `phase2_score`); `prices.py` gains sampled fields (`txns`, `price_change`) and a new `get_holder_stats` (GoPlus holders parse — same API call family already used by `get_taxes`); `monitor.py` wires recording (Task 1, live immediately, record-only) and later phase-2 entry (Task 5, behind a config flag that ships OFF); `trade_engine.py` gains the circuit breaker; `scripts/wallet_audition.py` gains the earliness bar; `scripts/film_report.py` scores collected films against 48h outcomes so thresholds get tuned from real data at the 20-21/7 checkpoint instead of guessed.

**Tech Stack:** Python 3 (existing venv), pytest + unittest.mock (existing idiom), DexScreener + GoPlus + GeckoTerminal free APIs (all already integrated). No new dependencies.

## Global Constraints

- **Real money, currently ZERO trading**: `wallets.json` on the VPS has `voting=0/50` (all suspended 18/7, user-approved) — the bot cannot open positions no matter what this plan's code does, until the promotion + go-live checkpoint. Every new feature still defaults OFF in code (`phase2_entry: false` ships in config; recording is the only thing that turns on).
- **Task 1 deploys ahead of everything else.** Film collection must start ASAP — it runs during the audition window (opens 2026-07-20T04:32 UTC) and its data tunes the Phase-2 thresholds at that checkpoint. Tasks 2-6 follow at normal pace.
- **Free-tier API budget**: max **8 concurrent dossiers**; per armed dossier per 60s tick: 1 DexScreener fetch (shared with the existing 60s price cache — usually free) + 1 GoPlus fetch (own 55s TTL cache). GoPlus free tier ≈ 30 calls/min — 8/min is safe.
- **Worktree/branch discipline**: every implementer's first command is `git branch --show-current`, must NOT be `main` (a prior task committed to the wrong checkout — do not repeat). VPS: `ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62`, repo `/home/agent/Track1-trade-onchain`, python as `sudo -u agent .venv/bin/python`. `wallets.json` is VPS-only; `config.json` is in git.
- Full suite green at every task: baseline going in is **772 passed, 2 skipped** (after the gem-wallets-v2 Tasks 1-3 built 18/7 — verify with `python -m pytest tests/ -q` before Task 1 and use the actual number you see as your baseline).
- Config knob names (exact): `watchlist_enabled`, `watchlist_max_dossiers`, `watchlist_max_hours`, `phase2_entry`, `phase2_min_samples`, `phase2_base_ratio_max`, `phase2_holder_growth_min_pct`, `phase2_entry_band`, `phase2_max_vs_arm`, `max_single_holder_pct`, `max_top5_holder_pct`, `daily_loss_limit_pct`.
- Do not modify the gem-wallets-v2 plan's artifacts (`find_recent_winners.py`, `build_bsc_smart_wallets.py`, audition PROMOTE/REJECT/INSUFFICIENT semantics) except the single earliness addition in Task 4.

## Design rationale (locked — do not reopen)

1. **Why film instead of snapshot:** one DexScreener call cannot distinguish accumulation from a pump top; a 60s-sampled time series can. The bot already ticks every 60s — the recorder just persists what the tick can see.
2. **The 6 Phase-2 fingerprints** (all computable from the film): (a) price base — last-30-min high/low ratio small, no lower-lows bleed; (b) holder count grows while price is flat (faking holder GROWTH costs real gas per sybil, unlike wash-trade counts); (c) dips absorbed — sells happened, price held; (d) the arming wallets have emitted no "out" event; (e) liquidity ≥ 90% of arm-time; (f) no unlocked whale >15% / top-5 >40%, and top share not rising.
3. **Entry price discipline:** enter only ≤ `phase2_entry_band` (1.15×) the median of the last 15 samples AND ≤ `phase2_max_vs_arm` (1.25×) the arm price. Under these rules all three of 18/7's losing entries (+31%/+50%/+33% chases that never retraced) would have been skipped entirely.
4. **Provisional thresholds ship as config, tuned at the checkpoint:** the film_report at 20-21/7 shows which fingerprint values actually preceded winners vs duds — thresholds are then adjusted by config edit, not code.
5. **Circuit breaker is stateless:** today's realized PnL is recomputed from `closed_trades.jsonl` on every open attempt — no state file, resets itself at UTC midnight.
6. **Recording arms on ANY watched wallet** (all 50, voting or observe-only) buying a gem-band token — maximum film during the audition window. ENTRY (Task 5, later) requires ≥2 **voting** wallets among the armers — quality-gated separately from recording.

---

### Task 1 (URGENT): recorder — prices extensions + watchlist store + monitor wiring + deploy

**Files:**
- Modify: `src/agent/copy_trade/prices.py`
- Create: `src/agent/copy_trade/watchlist.py`
- Modify: `src/agent/copy_trade/monitor.py`
- Test: `tests/test_copy_trade_prices.py` (append), `tests/test_watchlist.py` (new), `tests/test_copy_trade_monitor.py` (append)

**Interfaces:**
- `get_pair_stats` gains keys (additive, existing consumers unaffected): `"txns_h1_buys": int, "txns_h1_sells": int, "txns_m5_buys": int, "txns_m5_sells": int, "price_change_m5": float | None, "price_change_h1": float | None` (0/None when DexScreener omits them).
- New `get_holder_stats(token_address) -> dict | None` with keys `"holder_count": int, "top_pct": float, "top5_pct": float` — from GoPlus `token_security` (`holders` array), **excluding** entries that have a non-empty `tag` (LP pools like "PancakeV2"), the dead address, and `is_locked == 1`. `top_pct` = max remaining percent, `top5_pct` = sum of top 5 remaining. Own cache `_holder_cache` (55s TTL, failures never cached).
- `watchlist.Watchlist(films_path: Path, max_dossiers: int = 8, max_age_s: float = 21600)` with methods `arm(token, wallet, price, liquidity, now) -> bool`, `note_buy(token, wallet)`, `note_sell(token, wallet, now)` (disarms with reason `armer_sold` if wallet is an armer), `add_sample(token, sample: dict)`, `expire(now)`, `active() -> list[Dossier]`, `get(token) -> Dossier | None`. Every arm/sample/disarm appends one JSONL line to `films_path` with an `"event"` field (`"arm"|"sample"|"disarm"`).
- `Dossier` dataclass: `token_address, armed_at, arm_price, arm_liquidity, armers: list[str], samples: list[dict], disarmed: str | None = None`.

- [ ] **Step 1: prices tests (append to `tests/test_copy_trade_prices.py`)**

```python
@patch("src.agent.copy_trade.prices.requests.get")
def test_pair_stats_includes_txns_and_change(get_mock):
    p = _pair(liq=100, price="1.0", created=1)
    p["txns"] = {"m5": {"buys": 7, "sells": 3}, "h1": {"buys": 70, "sells": 30}}
    p["priceChange"] = {"m5": -2.5, "h1": 36.4}
    get_mock.return_value = _resp([p])
    s = prices.get_pair_stats(TOKEN)
    assert s["txns_h1_buys"] == 70 and s["txns_h1_sells"] == 30
    assert s["txns_m5_buys"] == 7 and s["txns_m5_sells"] == 3
    assert s["price_change_m5"] == -2.5 and s["price_change_h1"] == 36.4
    prices._pairs_cache.clear()
    get_mock.return_value = _resp([_pair(liq=100)])       # fields absent
    s = prices.get_pair_stats(TOKEN)
    assert s["txns_h1_buys"] == 0 and s["price_change_m5"] is None


@patch("src.agent.copy_trade.prices.requests.get")
def test_holder_stats_excludes_lp_dead_locked(get_mock):
    r = MagicMock(); r.raise_for_status.return_value = None
    r.json.return_value = {"result": {TOKEN: {"holder_count": "105", "holders": [
        {"address": "0xpool", "tag": "PancakeV2", "percent": "0.93", "is_locked": 0},
        {"address": "0x000000000000000000000000000000000000dead", "tag": "",
         "percent": "0.05", "is_locked": 1},
        {"address": "0xwhale", "tag": "", "percent": "0.205", "is_locked": 0},
        {"address": "0xsmall1", "tag": "", "percent": "0.03", "is_locked": 0},
        {"address": "0xsmall2", "tag": "", "percent": "0.02", "is_locked": 0},
    ]}}}
    get_mock.return_value = r
    prices._holder_cache.clear()
    h = prices.get_holder_stats(TOKEN)
    assert h["holder_count"] == 105
    assert h["top_pct"] == 0.205                          # whale, not the LP pool
    assert abs(h["top5_pct"] - 0.255) < 1e-9
    assert prices.get_holder_stats(TOKEN) == h and get_mock.call_count == 1  # cached


@patch("src.agent.copy_trade.prices.requests.get", side_effect=RuntimeError("down"))
def test_holder_stats_failure_returns_none_never_cached(get_mock):
    prices._holder_cache.clear()
    assert prices.get_holder_stats(TOKEN) is None
    assert prices._holder_cache == {}
```

- [ ] **Step 2: run → FAIL. Then implement in `prices.py`:**

Extend `get_pair_stats`'s return dict (after the existing keys, same `best` pair):

```python
        "txns_h1_buys": int(((best.get("txns") or {}).get("h1") or {}).get("buys") or 0),
        "txns_h1_sells": int(((best.get("txns") or {}).get("h1") or {}).get("sells") or 0),
        "txns_m5_buys": int(((best.get("txns") or {}).get("m5") or {}).get("buys") or 0),
        "txns_m5_sells": int(((best.get("txns") or {}).get("m5") or {}).get("sells") or 0),
        "price_change_m5": (best.get("priceChange") or {}).get("m5"),
        "price_change_h1": (best.get("priceChange") or {}).get("h1"),
```

Add at module level (near `_pairs_cache`) and as a new function:

```python
_HOLDER_TTL_S = 55
_holder_cache: dict[str, tuple[float, dict]] = {}
_DEAD = "0x000000000000000000000000000000000000dead"


def get_holder_stats(token_address: str) -> dict | None:
    """Holder distribution facts for the concentration gate + phase-2 films.
    Excludes LP pools (non-empty GoPlus tag), the dead address, and locked
    holders — what's left is the supply that can actually dump on us."""
    key = token_address.lower()
    hit = _holder_cache.get(key)
    if hit is not None and time.time() - hit[0] < _HOLDER_TTL_S:
        return hit[1]
    try:
        r = requests.get(_GOPLUS + token_address, timeout=15)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return None
        free = [float(h.get("percent") or 0) for h in (info.get("holders") or [])
                if not h.get("tag") and (h.get("address") or "").lower() != _DEAD
                and not h.get("is_locked")]
        free.sort(reverse=True)
        out = {"holder_count": int(info.get("holder_count") or 0),
               "top_pct": free[0] if free else 0.0,
               "top5_pct": sum(free[:5])}
        _holder_cache[key] = (time.time(), out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("goplus_holders_failed", token=token_address, error=type(e).__name__)
        return None
```

- [ ] **Step 3: watchlist tests — create `tests/test_watchlist.py`:**

```python
"""Dossier store for the phase-2 stakeout. Critical behaviors: cap on concurrent
dossiers, armer-sold disarm, 6h expiry, every event persisted as one JSONL line."""
import json

from src.agent.copy_trade.watchlist import Watchlist

T1, T2 = "0x" + "a" * 40, "0x" + "b" * 40
W1, W2 = "0x" + "1" * 40, "0x" + "2" * 40


def _wl(tmp_path, **kw):
    return Watchlist(films_path=tmp_path / "films.jsonl", **kw)


def _lines(tmp_path):
    return [json.loads(l) for l in (tmp_path / "films.jsonl").read_text().splitlines()]


def test_arm_note_buy_sample_lifecycle(tmp_path):
    wl = _wl(tmp_path)
    assert wl.arm(T1, W1, price=1.0, liquidity=50_000, now=1000.0) is True
    assert wl.arm(T1, W2, price=1.1, liquidity=50_000, now=1010.0) is False  # already armed
    wl.note_buy(T1, W2)                                   # second wallet joins armers
    d = wl.get(T1)
    assert d.armers == [W1, W2] and d.arm_price == 1.0
    wl.add_sample(T1, {"price": 1.05, "liq": 51_000})
    assert len(d.samples) == 1
    events = [l["event"] for l in _lines(tmp_path)]
    assert events == ["arm", "sample"]


def test_armer_sell_disarms_and_persists_reason(tmp_path):
    wl = _wl(tmp_path)
    wl.arm(T1, W1, price=1.0, liquidity=1, now=0.0)
    wl.note_sell(T1, W2, now=5.0)                         # non-armer: no effect
    assert wl.get(T1).disarmed is None
    wl.note_sell(T1, W1, now=9.0)                         # armer sold -> signal dead
    assert wl.get(T1) is None                             # no longer active
    assert _lines(tmp_path)[-1] == {"event": "disarm", "token_address": T1,
                                    "reason": "armer_sold", "ts": 9.0}


def test_cap_and_expiry(tmp_path):
    wl = _wl(tmp_path, max_dossiers=1, max_age_s=100)
    assert wl.arm(T1, W1, price=1, liquidity=1, now=0.0) is True
    assert wl.arm(T2, W1, price=1, liquidity=1, now=1.0) is False   # cap reached
    wl.expire(now=101.0)
    assert wl.get(T1) is None
    assert _lines(tmp_path)[-1]["reason"] == "expired"
    assert wl.arm(T2, W1, price=1, liquidity=1, now=102.0) is True  # slot freed
```

- [ ] **Step 4: run → FAIL. Implement `src/agent/copy_trade/watchlist.py`:**

```python
"""Phase-2 stakeout dossiers: a signal ARMS a token, the monitor FILMS it (one
sample per tick), entry logic (config-gated, later task) reads the film. Films
are append-only JSONL so film_report.py can tune thresholds from real outcomes.
ponytail: RAM-held dossiers — a restart loses active stakeouts but never the
film lines already written; acceptable, stakeouts re-arm on the next buy."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dossier:
    token_address: str
    armed_at: float
    arm_price: float
    arm_liquidity: float
    armers: list[str]
    samples: list[dict] = field(default_factory=list)
    disarmed: str | None = None


class Watchlist:
    def __init__(self, films_path: Path, max_dossiers: int = 8,
                 max_age_s: float = 6 * 3600) -> None:
        self._path = films_path
        self._max = max_dossiers
        self._max_age = max_age_s
        self._dossiers: dict[str, Dossier] = {}

    def _write(self, row: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def arm(self, token: str, wallet: str, price: float, liquidity: float,
            now: float | None = None) -> bool:
        token = token.lower()
        if token in self._dossiers or len(self._dossiers) >= self._max:
            return False
        now = time.time() if now is None else now
        self._dossiers[token] = Dossier(token_address=token, armed_at=now,
                                        arm_price=price, arm_liquidity=liquidity,
                                        armers=[wallet.lower()])
        self._write({"event": "arm", "token_address": token, "ts": now,
                     "wallet": wallet.lower(), "price": price, "liquidity": liquidity})
        return True

    def note_buy(self, token: str, wallet: str) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None and wallet.lower() not in d.armers:
            d.armers.append(wallet.lower())

    def note_sell(self, token: str, wallet: str, now: float | None = None) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None and wallet.lower() in d.armers:
            self._disarm(d, "armer_sold", time.time() if now is None else now)

    def add_sample(self, token: str, sample: dict) -> None:
        d = self._dossiers.get(token.lower())
        if d is None:
            return
        d.samples.append(sample)
        self._write({"event": "sample", "token_address": d.token_address,
                     **sample})

    def expire(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for d in list(self._dossiers.values()):
            if now - d.armed_at > self._max_age:
                self._disarm(d, "expired", now)

    def disarm(self, token: str, reason: str, now: float | None = None) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None:
            self._disarm(d, reason, time.time() if now is None else now)

    def _disarm(self, d: Dossier, reason: str, now: float) -> None:
        d.disarmed = reason
        self._dossiers.pop(d.token_address, None)
        self._write({"event": "disarm", "token_address": d.token_address,
                     "reason": reason, "ts": now})

    def active(self) -> list[Dossier]:
        return list(self._dossiers.values())

    def get(self, token: str) -> Dossier | None:
        return self._dossiers.get(token.lower())
```

- [ ] **Step 5: monitor wiring test (append to `tests/test_copy_trade_monitor.py`):**

```python
@patch("src.agent.copy_trade.monitor.get_holder_stats",
       return_value={"holder_count": 50, "top_pct": 0.03, "top5_pct": 0.1})
@patch("src.agent.copy_trade.monitor.get_pair_stats")
@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.monitor.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_watchlist_records_gem_band_buys(_s, _mp, stats_mock, _hs, tmp_path):
    import time as _t
    from src.agent.copy_trade.watchlist import Watchlist
    young = _t.time() * 1000 - 2 * 86400_000
    stats_mock.return_value = {"price_usd": 1.0, "liquidity_usd": 50_000.0,
                               "market_cap_usd": 400_000.0,
                               "pair_created_at_ms": young, "pair_address": "0xp",
                               "txns_h1_buys": 5, "txns_h1_sells": 2,
                               "txns_m5_buys": 1, "txns_m5_sells": 0,
                               "price_change_m5": 1.0, "price_change_h1": 5.0}
    tracker, engine, store = _pipeline(tmp_path)
    wl = Watchlist(films_path=tmp_path / "films.jsonl")
    cfg = {"max_token_age_days": 14, "max_market_cap_usd": 5_000_000,
           "min_liquidity_usd": 20_000}
    meta = lambda addr: ("GEM", 18)
    process_events([_ev(W1)], tracker, engine, store, None, meta,
                   voting=set(), watchlist=wl, gem_cfg=cfg)   # voting empty: no trade
    assert len(wl.active()) == 1                              # but the film started
    assert wl.get(T).armers == [W1.lower()]
    process_events([_ev(W2), _ev(W1, direction="out")], tracker, engine, store,
                   None, meta, voting=set(), watchlist=wl, gem_cfg=cfg)
    assert wl.get(T) is None                                  # armer sold -> disarmed
```

- [ ] **Step 6: run → FAIL. Implement in `monitor.py`:**

Add imports: `from .prices import get_price_usd, get_pair_stats, get_holder_stats`, `from .watchlist import Watchlist`. Add constant `FILMS_PATH = ROOT / "data" / "copy_trade" / "watchlist_films.jsonl"`.

Add a module-level helper:

```python
def _is_gem_band_stats(stats: dict | None, cfg: dict) -> bool:
    """Same three rules as the engine's gem filter — used to decide which
    tokens deserve a film. Unknowns = not gem band."""
    if stats is None or stats.get("pair_created_at_ms") is None:
        return False
    age_d = (time.time() - stats["pair_created_at_ms"] / 1000) / 86400
    if age_d > cfg.get("max_token_age_days", 14):
        return False
    mcap = stats.get("market_cap_usd")
    if mcap is None or mcap > cfg.get("max_market_cap_usd", 5_000_000):
        return False
    return (stats.get("liquidity_usd") or 0) >= cfg.get("min_liquidity_usd", 20_000)
```

Extend `process_events` signature: `..., voting: set[str] | None = None, watchlist=None, gem_cfg: dict | None = None)`. Inside the loop:
- In the `direction == "out"` branch, BEFORE the existing exit-signal handling, add: `if watchlist is not None: watchlist.note_sell(ev.wallet and ev.token_address and ev.token_address, ev.wallet)` — concretely: `watchlist.note_sell(ev.token_address, ev.wallet)`.
- In the `direction == "in"` path, BEFORE the `voting` skip (recording covers ALL watched wallets), add:

```python
        if watchlist is not None and gem_cfg is not None:
            if watchlist.get(ev.token_address) is not None:
                watchlist.note_buy(ev.token_address, ev.wallet)
            else:
                stats = get_pair_stats(ev.token_address)
                if _is_gem_band_stats(stats, gem_cfg):
                    watchlist.arm(ev.token_address, ev.wallet,
                                  price=stats["price_usd"],
                                  liquidity=stats["liquidity_usd"])
                    log.info("stakeout_armed", token=ev.token_address,
                             wallet=ev.wallet)
```

In `run_scan`: build `watchlist = Watchlist(FILMS_PATH, max_dossiers=cfg.get("watchlist_max_dossiers", 8), max_age_s=cfg.get("watchlist_max_hours", 6) * 3600) if cfg.get("watchlist_enabled", True) else None`; pass `watchlist=watchlist, gem_cfg=cfg` into `process_events`; and after `engine.check_exits()` add the sampling pass:

```python
        if watchlist is not None:
            watchlist.expire()
            for d in watchlist.active():
                stats = get_pair_stats(d.token_address)
                hs = get_holder_stats(d.token_address)
                if stats is None:
                    continue          # no sample this tick; film gap is visible in ts
                watchlist.add_sample(d.token_address, {
                    "ts": time.time(), "price": stats["price_usd"],
                    "liq": stats["liquidity_usd"],
                    "buys_h1": stats["txns_h1_buys"], "sells_h1": stats["txns_h1_sells"],
                    "buys_m5": stats["txns_m5_buys"], "sells_m5": stats["txns_m5_sells"],
                    "chg_m5": stats["price_change_m5"],
                    "holders": (hs or {}).get("holder_count"),
                    "top_pct": (hs or {}).get("top_pct"),
                    "top5_pct": (hs or {}).get("top5_pct")})
```

- [ ] **Step 7: run all touched suites + full suite** (`tests/test_copy_trade_prices.py tests/test_watchlist.py tests/test_copy_trade_monitor.py -v`, then `tests/ -q` — no regressions vs your recorded baseline).

- [ ] **Step 8: Commit** — `feat(copy_trade): phase-2 stakeout recorder — films every gem-band buy by a watched wallet`

- [ ] **Step 9: DEPLOY NOW (do not wait for later tasks):** merge branch → `main` (full suite green first), push, then on the VPS: `git pull`, `systemctl restart copy-trade`, verify startup line still `voting=0`, and within ~15 min confirm `data/copy_trade/watchlist_films.jsonl` exists once any watched wallet buys a gem-band token (may take an hour or two — check `stakeout_armed` in the log). Zero trading risk: voting is 0 and `phase2_entry` doesn't exist yet.

---

### Task 2: `scripts/film_report.py` — score films against outcomes

**Files:** Create `scripts/film_report.py`, `tests/test_film_report.py`.

**Interfaces:** reads `data/copy_trade/watchlist_films.jsonl`; reuses `fetch_max_price_since` from `scripts.gem_report`. Pure (tested): `load_films(rows) -> dict[token, dict]` grouping arm/samples/disarm per token (a token can be armed multiple times — key by `(token, armed_ts)`; simplest: list of film dicts `{"token_address", "armed_at", "arm_price", "samples", "disarmed"}` in arm order), and `film_fingerprints(film) -> dict` computing per-film: `n_samples`, `base_ratio` (max/min price of last 30 samples), `holder_growth_pct` (last vs first non-None holders), `liq_ratio` (last liq / arm_liquidity), `max_top_pct`. `main()` prints one row per film: fingerprints + outcome multiple (`fetch_max_price_since(token, armed_at)` / arm_price) and a summary split: films whose token did ≥2x vs <2x, with median fingerprint values per group — **this table is what tunes the Phase-2 thresholds at the checkpoint.**

Steps: standard TDD (write the two pure-function tests with hand-built film rows exactly as in prior tasks' style; implement; full suite; commit `feat(copy_trade): film_report — phase-2 fingerprint outcomes table`). Keep `main()`'s network part thin and failure-tolerant like `gem_report.py` (sleep 0.5s between GeckoTerminal calls).

---

### Task 3: concentration gate + daily circuit breaker in the engine

**Files:** Modify `src/agent/copy_trade/trade_engine.py`; test in `tests/test_trade_engine.py` (append).

**Interfaces:** `TradeEngine.__init__` gains `max_single_holder_pct: float | None = None`, `max_top5_holder_pct: float | None = None`, `daily_loss_limit_usd: float | None = None` (all default OFF). In `open_cluster_position`, two new gates run between the gem filter and the budget check:

```python
        if self._daily_loss_limit_usd is not None:
            lost = self._realized_pnl_today()
            if lost <= -self._daily_loss_limit_usd:
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_circuit_breaker", f"day_pnl_{lost:.2f}")
                log.warning("circuit_breaker_open", day_pnl=round(lost, 2))
                return False
        if self._max_single_holder_pct is not None or self._max_top5_holder_pct is not None:
            hs = get_holder_stats(token)
            if hs is None:
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", "no_holder_data")
                return False           # fail closed — can't see the whales, don't buy
            if (self._max_single_holder_pct is not None
                    and hs["top_pct"] > self._max_single_holder_pct):
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", f"top_{hs['top_pct']:.2f}")
                return False
            if (self._max_top5_holder_pct is not None
                    and hs["top5_pct"] > self._max_top5_holder_pct):
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", f"top5_{hs['top5_pct']:.2f}")
                return False
```

`_realized_pnl_today()` (stateless — recomputed each call, self-resets at UTC midnight):

```python
    def _realized_pnl_today(self) -> float:
        if not self._journal_path.exists():
            return 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0.0
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("closed_at", "").startswith(today) and not row.get("simulated"):
                    total += float(row.get("pnl_usd") or 0)
            except (ValueError, TypeError):
                continue
        return total
```

(Store `self._journal_path = journal_path` in `__init__` if not already an attribute — check the current file; the cooldown seed reads it as a local param today.) Import `get_holder_stats` from `.prices`. Wire the three kwargs in `monitor._build_runtime` from `cfg.get(...)` (defaults `None`; for the breaker: `cfg.get("daily_loss_limit_pct")` × `cfg.get("total_budget_usd", 15.9)` when the pct is present, else `None`).

Tests (append; follow the `_engine_v3` fixture pattern): (1) breaker blocks after journaled same-day losses exceed the limit and signal row says `skipped_circuit_breaker` — build by writing two fake journal rows with today's `closed_at` and `pnl_usd: -1.5` each, engine with `daily_loss_limit_usd=2.0`; verify a yesterday-dated row does NOT count; (2) concentration blocks on `top_pct` 0.205 with limit 0.15 (patch `get_holder_stats`), blocks on `hs=None` (fail closed), passes at 0.03 with `top5` under limit; (3) all-None defaults change nothing (existing tests already prove this — just run them).

Verification against real data: the 龙虾 token's GoPlus data (checked 18/7) shows an unlocked untagged holder at 20.5% — with `max_single_holder_pct: 0.15` that trade is blocked at entry. State this in the task's commit message. Commit: `feat(copy_trade): concentration gate + stateless daily circuit breaker`.

---

### Task 4: audition earliness bar

**Files:** Modify `scripts/wallet_audition.py`; test `tests/test_wallet_audition.py` (append/adjust).

**Interfaces:** `audit_wallets(rows, stats_by_token, holds, cfg)` adds to each row: `"median_entry_age_min": float | None` — median over the wallet's GEM-band buys of `(buy_ts − pair_created_at_ms/1000) / 60`, using `stats_by_token[token]["pair_created_at_ms"]` (skip tokens with None). New module constant `EARLY_MAX_MEDIAN_AGE_MIN = 60`. PROMOTE now additionally requires `median_entry_age_min is not None and median_entry_age_min <= EARLY_MAX_MEDIAN_AGE_MIN` — a wallet that habitually buys gems 3 hours into the run is a follower, not a hunter; it gets REJECT (not INSUFFICIENT) when its other bars pass but earliness fails. The report table prints the new column.

Steps: TDD — add a test where a wallet has 3 gem buys at token ages 10/20/40 min (PROMOTE) vs another at 90/180/240 min (REJECT with reason visible), keeping the existing INSUFFICIENT semantics untouched; adjust any existing test fixtures that now need `pair_created_at_ms` values consistent with their intended verdicts (the existing `test_audit_promote_reject_insufficient` builds young tokens — set its row timestamps so W1's buys land within 60 min of the fixture's `pair_created_at_ms`). Full suite. Commit: `feat(copy_trade): audition earliness bar — gem hunters buy in the first hour, followers get rejected`.

---

### Task 5: phase-2 scoring + config-gated stakeout entry

**Files:** Modify `src/agent/copy_trade/watchlist.py` (add pure `phase2_score`), `src/agent/copy_trade/monitor.py` (entry pass); tests in `tests/test_watchlist.py` + `tests/test_copy_trade_monitor.py`.

**Interfaces:**

```python
def phase2_score(d: Dossier, cfg: dict, voting: set[str]) -> tuple[bool, str]:
    """All six fingerprints green + enough film + ≥2 voting armers + price in band.
    Returns (ok, reason) — reason names the FIRST failing check for the signal log."""
```

Checks in order (each with its config knob, values read via `cfg.get(name, default)`):
1. `len([a for a in d.armers if a in voting]) >= 2` else `"need_2_voting_armers"`.
2. `len(d.samples) >= cfg phase2_min_samples (15)` else `"film_too_short"`.
3. Base: over the last 30 samples (or all, if fewer): `max(price)/min(price) <= phase2_base_ratio_max (1.35)` else `"no_base"`; prices must be truthy.
4. Holder growth: first/last non-None `holders` in the window — `last >= first * (1 + phase2_holder_growth_min_pct (0.05))` else `"holders_flat"`; if all None → `"holders_unknown"` (fail closed).
5. Liquidity: `last liq >= 0.9 * d.arm_liquidity` else `"liq_draining"`.
6. Concentration in-film: latest non-None `top_pct <= max_single_holder_pct (0.15)` and `top5_pct <= max_top5_holder_pct (0.40)` else `"whale_risk"`; all-None → `"holders_unknown"`.
7. Price band: `last price <= phase2_entry_band (1.15) * median(prices of last 15 samples)` AND `last price <= phase2_max_vs_arm (1.25) * d.arm_price` else `"chasing"`.
Return `(True, "")` when all pass.

Monitor entry pass (in `run_scan`, after the sampling loop, only when `cfg.get("phase2_entry", False)` and `voting`):

```python
            for d in watchlist.active():
                ok, why = phase2_score(d, cfg, voting)
                if not ok:
                    continue
                symbol, decimals = _token_meta(pool, d.token_address)
                opened = engine.open_cluster_position(
                    d.token_address, symbol, decimals,
                    {"wallets": d.armers, "first_ts": d.armed_at,
                     "first_price_usd": d.arm_price})
                watchlist.disarm(d.token_address,
                                 "entered" if opened else "entry_rejected")
                if opened:
                    _notify(notifier, f"[COPY-TRADE] PHASE2 BUY {symbol}",
                            f"token {d.token_address}\narmers: {', '.join(d.armers)}\n"
                            f"arm price {d.arm_price}, film {len(d.samples)} samples")
```

Also disarm on runaway inside the sampling loop: after `add_sample`, `if stats["price_usd"] >= 2 * d.arm_price: watchlist.disarm(d.token_address, "ran_away")`.

Tests: pure `phase2_score` cases — one fully-green dossier passes; each fingerprint individually broken returns its named reason (build samples programmatically); the 18/7 failure shapes as regression fixtures: a dossier whose last price is 1.5× arm price fails `"chasing"` (龙虾 shape), a 20-sample film with `holders` flat fails `"holders_flat"`. Monitor test: with `phase2_entry` False nothing opens even for a perfect dossier; with True + 2 voting armers + green film, exactly one position opens and the dossier disarms `"entered"`. Full suite. Commit: `feat(copy_trade): phase2_score + stakeout entry (config-gated, ships OFF)`.

---

### Task 6: config, deploy, checkpoint protocol

- [ ] **Step 1: `data/copy_trade/config.json`** — add inside `copy_settings` (keep everything else byte-identical):

```json
    "watchlist_enabled": true,
    "watchlist_max_dossiers": 8,
    "watchlist_max_hours": 6,
    "phase2_entry": false,
    "phase2_min_samples": 15,
    "phase2_base_ratio_max": 1.35,
    "phase2_holder_growth_min_pct": 0.05,
    "phase2_entry_band": 1.15,
    "phase2_max_vs_arm": 1.25,
    "max_single_holder_pct": 0.15,
    "max_top5_holder_pct": 0.40,
    "daily_loss_limit_pct": 0.15,
```

`phase2_entry` MUST ship `false`. Full suite; commit `feat(copy_trade): phase-2 stakeout config — entry ships OFF pending checkpoint`.

- [ ] **Step 2: merge → main, push, VPS pull + restart, verify** (startup `voting=0`, service active, no poll-failure streaks, films still appending).

- [ ] **Step 3: HUMAN CHECKPOINT (20-21/7, combined with the gem-wallets-v2 Task 5 audition checkpoint).** Present to the user in one sitting: (a) the audition PROMOTE/REJECT/INSUFFICIENT table (now including earliness); (b) `film_report.py` output — fingerprint medians for ≥2x films vs <2x films; (c) proposed threshold adjustments (config diff) based on (b); (d) the go-live decision: promote wallets AND set `phase2_entry: true` ONLY if the user approves both. If the film data shows no fingerprint separates winners from duds, say so plainly and recommend keeping `phase2_entry: false` while collecting another cycle — do not argue for going live on weak evidence.

- [ ] **Step 4: update the memory handoff** (`gem-wallets-v2-rebuild-handoff.md` or successor): stakeout built, films collecting since <date>, entry OFF, what the checkpoint decided, resumption state.

## Resumption criteria (the bot trades again only when ALL true)

1. Audition produced ≥1 PROMOTE wallet (with the earliness bar) and the user approved the promotion.
2. `film_report` evidence reviewed at the checkpoint; thresholds tuned; user explicitly approved `phase2_entry: true`.
3. Circuit breaker + concentration gate deployed (Tasks 3, 6).
Capital note for the checkpoint conversation: the wallet holds ~$9 ≈ 3 slices. The plan's stance: fire zero bullets until the free evidence (audition + films + signal scorecard) is positive; whether to add capital afterwards is solely the user's decision and should be raised only if that evidence is positive.

## Self-Review (done at write time)

1. **Coverage vs design**: 6 fingerprints → `phase2_score` checks 3-6 + samples/armers/band; recording (T1), outcome-tuning (T2), whale+breaker (T3), earliness (T4), entry (T5), ship-safe config + human gates (T6). The 18/7 post-mortem items all land: chase entries → band checks ("chasing"), 龙虾 whale → concentration fail-closed, -$6.15 day → breaker, condemned-signal reflex-buying → entry requires voting armers (currently zero) + film + flag OFF.
2. **Placeholder scan**: Task 2 and Task 4 describe tests by exact scenario rather than full listings (same style previously executed successfully by the Sonnet session on the v2 plan's Task 2-3); all engine/monitor/watchlist code is verbatim. Task 5's monitor block is verbatim; its test list names concrete fixtures and expected reasons.
3. **Interface consistency**: `get_pair_stats` new keys match T1 tests and T5 sampling dict; `get_holder_stats` keys match T3 gate + T5 fingerprint 6 + T1 sample fields; `Dossier`/`Watchlist` methods match monitor wiring and `phase2_score` signature; `process_events(..., watchlist=, gem_cfg=)` kwargs are additive-default so every existing test/call stays valid; circuit-breaker journal rows use the existing `closed_at`/`pnl_usd`/`simulated` fields exactly as written by `_close`/`_close_partial`.
