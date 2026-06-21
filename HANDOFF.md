# HANDOFF — Aegis Track-1 Trading Agent (BNB Hack 2026)

> **GOAL: win Track-1** (ranked by RAW total wallet return; ≥30% drawdown = DQ).
> Updated **2026-06-21 ~13:30 UTC**. Read this once → full context. Code is at commit
> `51cd50e` on branch `harden/breaker-and-pricing` (pushed to `main`, deployed to VPS).

---

## 0. ENVIRONMENT / ACCESS

- **Local repo:** `E:\Track1-trade-onchain` (Windows; Git Bash + PowerShell). Tests: `python -m pytest -q` (**411 pass / 2 skip**), lint `python -m ruff check src tests`.
- **VPS (live bot):** `root@2.25.184.43`, dir `/home/agent/bnbhack-track1-agent`.
  - SSH: `ssh -i "$USERPROFILE/.ssh/hostinger_openclaw" root@2.25.184.43`
  - Run agent cmds as the `agent` user: `sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent <cmd>`
  - systemd units: **`agent`** (the bot) and **`aegis-dash`** (the dashboard static server).
- **Git:** `github.com/tonielivegood/aegis-trading-agent` (PRIVATE). Work on `harden/breaker-and-pricing`, push to `main` (VPS does `git fetch && git reset --hard origin/main`).
- **Wallet (registered, self-custody):** `0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa` — ~$33. `.env` holds `AGENT_PRIVATE_KEY`.
- **`.env` (gitignored, on local + VPS):** `ONEINCH_API_KEY`, `EXECUTION_BACKEND=1inch`, `CMC_API_KEY` (Pro), `ANTHROPIC_API_KEY`, `BSC_RPC_URL` (NodeReal), `AGENT_PRIVATE_KEY`, `TELEGRAM_*`. **Mask secrets in any output; never commit `.env`.**

---

## 1. CURRENT STATE (very important)

- **Bot is LIVE NOW** (`run --live`, flipped early **21/6** for strategy validation). **Trades 21/6 do NOT count** — the scored contest window is **22–28/6**. Equity ~$33.25, holds dust + a few small meme positions (RAVE/GUA/etc.). Regime currently **CAUTIOUS** (Claude tightened on Fear & Greed 22).
- **Dashboard is LIVE + public:** **http://2.25.184.43:8080/dashboard.html** (served by `aegis-dash.service` from `web/`). Auto-refreshes from `web/status.json` (a MASKED snapshot the bot writes each tick — verified 0 secrets).
- **⚠️ At 22/6 00:00 UTC (contest start) — run `panic --live` → `reset`, then KEEP it live.**
  Sequence (NOT just `reset`):
  1. `systemctl stop agent`
  2. `sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent panic --live` — flatten ALL non-stable holdings to USDT.
  3. `sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent reset` — re-baseline drawdown/compliance + clear the books.
  4. `systemctl start agent` — keep it `--live`.

  **WHY panic first (this is the fix, 21/6):** the wallet holds ~24 leftover meme/dust
  tokens (RAVE/NEX/GUA/SAHARA/…) but the position book tracks only RAVE → every other
  holding is ORPHANED (no stop/trail manages it; `decide_exits` only iterates the book).
  A bare `reset` clears the book → it would ORPHAN **RAVE too**, and `reset` does NOT sell
  coins. `panic --live` consolidates everything to USDT first, so the contest starts 100%
  in cash with a clean drawdown baseline and zero unmanaged positions. (Sub-$2 dust may
  remain — harmless.) **Do NOT run `/root/go-live.sh`** — its guard aborts when the unit
  is already `--live`. (To go back to DRY instead: `sed -i 's#run --live#run#' /etc/systemd/system/agent.service` + `daemon-reload` + restart.)

---

## 2. WHAT THIS SESSION BUILT (don't forget any of it)

1. **Strategy redesign — "confirmed momentum, then RIDE"** (`aegis/token_class.py`). Replaced the old scalp/churn design after a live soak proved it bled. Cash by default. Entry needs a **5-minute** volume surge **AND** a confirmed price move (filters 1-min noise). Exits are **take-profit / hard-stop / trailing ONLY — no time-based exit** (the no-progress timer + volume-death exits were removed). The regime throttles **exposure** (size/slots), never the signal bar (beta valve removed).

   | Tier | Vol bar | Confirm | TP cap | Trail | Stop | Size |
   |---|---|---|---|---|---|---|
   | **MAJOR** | 2.5× | +3% | +30% | 7% | −7% | ~35% NAV (regime) |
   | **MEME** | 4× | **+3%** | **+80%** | **10%** | **−8%** | **$5 fixed lottery** |

   Plus a **BREAKEVEN stop** on both tiers (21/6): once a trade runs to **+5%** peak, exit ~flat
   (entry +0.5% fee buffer) if it falls back to entry — closes the gap where the trailing stop
   (gated on price>entry) let a "+5% pop then fade" ride down to the −8% hard stop. Meme entry is
   **+3%** (was +6% earlier 21/6): the meme sleeve is a bounded-downside/huge-upside lottery →
   optimise for SHOTS ON GOAL, and let the breakeven stop (not a higher entry floor) handle the
   pop-then-fade false starts.
2. **CMC AI Agent Hub (#CMCAgentHub, $2k prize)** — `data/cmc_agent_hub.py`. Two REST skills: **Fear & Greed** tightens the regime (tightening-only, ≤20=extreme fear); **community trending** re-ranks qualified breakouts (1.5× boost). Out of the 60s hot path, cached, fail-safe. Demo: `python -m src.agent signals`.
3. **Claude regime advisor** — `aegis/claude_advisor.py`. **Real LLM in the loop.** Hourly, Haiku reads BTC H1/24h + Fear & Greed → recommends a regime. **TIGHTENING-ONLY** (can only step risk DOWN, enforced in code), **FAIL-SAFE** (error → base regime), out of hot path, output is a bounded enum. Calibrated to keep the base unless concrete danger. Toggle `CLAUDE_ADVISOR_ENABLED`. Shown on the dashboard ("Claude risk officer") + `regime.reason`.
4. **Receipt-status fix** — all 3 execution backends (`oneinch.py` live / `openocean.py` / `pancakeswap.py`) now RAISE on `receipt.status != 1`, so a mined-but-reverted swap is no longer counted as a valid trade or trusted as a fill.
5. **Telegram fix** — `monitor/notifier.py` switched urllib→`requests` (urllib's TLS handshake to Telegram times out on the VPS). Alerts (heartbeat/trades/breaker) now deliver.
6. **Live dashboard** — `web/dashboard.html` (self-contained) + `_write_status_snapshot` in `agent_loop.py` (masked `web/status.json` each tick, NO secrets, fail-safe). Shows: equity+sparkline, regime, CMC Agent Hub (F&G gauge + trending + **Claude panel**), live signal scan (proves discipline), positions, trade feed, strategy (genericized — no exact numbers), risk engine, 3-partner stack, compliance, on-chain proof. `aegis-dash.service` = `python -m http.server 8080 --directory web` (read-only, only serves `web/`).
7. **Go-live verified** — the unit is `run` (DRY) by design; `go-live.sh` sed-flips it to `run --live` which forces `dry_run=False` explicitly. (We flipped early — see §1.)
8. **Docs synced** to the current architecture: `README.md`, `ARCHITECTURE.md`, `docs/JUDGE_DEMO_RUNBOOK.md`, `SPEC.md`/`PLAN.md` banners.
9. **Real on-chain trade test passed** — forced buy + panic sell via 1inch, all status 1 (proves the live path with the receipt-status fix).
10. **Dashboard leak fixed (21/6, commit `51cd50e`, deployed + live-verified)** — the public `status.json` scan rows had been publishing `bar` = the exact volume-multiple entry threshold (2.5 major / 4.0 meme). Removed it; the scan now exposes only observed market state (`vol_x`/`bo_pct`) + the `fires` boolean (can't be inverted to the bar). +regression test. Verified on the live VPS: `any bar field leaked: False`.
11. **Contest-start AUTOMATED (21/6, commit `eaebd94`, installed + scheduled on VPS)** — a one-shot systemd timer `contest-start.timer` fires at **2026-06-22 00:00:00 UTC** and runs `contest-start.sh`: stop agent → `panic --live` (flatten all >$2 holdings to USDT, clearing orphaned positions) → `reset` (re-baseline) → start agent (stays `--live`) → Telegram-confirm the clean baseline. Sentinel-guarded against double-fire; always restarts the agent. Files in `deploy/golive/`. Verified scheduled + components (equity-parse/notify/panic-dry) green. **This replaces the manual §1 step — it will run itself; just confirm afterward.**
12. **Signal fix — breakout% now from same-source kline move (21/6, commit `7bafb89`, deployed + live-verified)** — the entry gate measured the 5m price move from a tick-sampled CMC price cache, which lags thin Alpha memes → bought the top as the spike faded (RAVE/GUA). Now `MarketSnapshot.breakout_pct` is fed the `price_change_5m_pct` from the SAME klines as the volume (Binance Alpha for memes / spot for majors) via a new `volume_and_move()` provider method (one fetch, 3-tuple). `scan_breakouts`/dashboard/`scan_diag` all use the shared `breakout_pct()` helper; falls back to the cache when no kline move is supplied. +6 tests. `scan_diag` on the VPS confirmed real kline moves (BEAT −2.7%, FORM +0.7%) with discipline intact (GENIUS 242% of vol bar but flat → no fire).
13. **Breakeven stop + meme entry +3% (21/6, commit `0237a45`, deployed + live-verified)** — phased-barbell day-1 safe fixes (see §4 for the strategy direction). (a) **Breakeven stop**: once a trade runs to +5% peak, exit ~flat (+0.5% fee buffer) if it falls back to entry — closes the gap where the trailing stop (gated on price>entry) let a pop-then-fade ride to the −8% hard stop. Global knobs `aegis_breakeven_trigger_pct`/`_buffer_pct`. (b) **Meme entry +6%→+3%**: the lottery sleeve optimises for shots-on-goal; the breakeven stop (not a higher entry floor) handles pop-then-fade false starts. +6 tests (421 pass).
15. **Daily soft breaker + beta momentum-by-id (21/6, commit `5ad18e3`, deployed + live-verified)** — a 2nd-pass adversarial review flagged that meme +3% (item 13) without a frequency/loss cap re-opens the churn-bleed tail that can reach the −20% latch (= contest-ending). **(#1) Daily soft circuit-breaker** (`risk/daily_breaker.py`): anchors the UTC-day open equity; once intraday drawdown ≥ `DAILY_SOFT_BREAKER_PCT` (default 8%) it halts NEW entries for the rest of the day (exits/stops + min-trade compliance still run), resets 00:00 UTC. Wired at the SOURCE in event mode (entry valve forced RISK_OFF → no phantom book positions); `reset` clears it. Bounds bleed from ANY source → keeps the +3% upside without the tail. **(#2)** beta momentum now sourced by CMC **id** (`cmc_client.get_quotes_by_id`), matching the price feed, so a same-symbol collision can't rank the wrong token into the basket. +12 tests (439 pass). Live: day-state anchors, tick clean.
14. **Beta core BUILT — barbell Phase-2 brain + soak tool, gated OFF (21/6, commit `48ed9b1`, deployed)** — `aegis/beta_core.py`: a PURE, fully-tested decision module = regime-gated momentum-major basket (rank majors by blended CMC 1h/24h momentum; RISK_ON holds top N with trailing/breakeven/hard-stop; rotates on momentum loss; flattens on RISK_OFF/breaker; majors-only, memes stay in the lottery sleeve; never re-enters a same-tick exit; respects the floor). `scripts/beta_diag.py` = read-only soak tool (shows the ranked basket + simulated orders). Config `BETA_CORE_ENABLED` (default **false**) + `beta_core_*` knobs. +12 tests. Live `beta_diag` verified sane: in CAUTIOUS → 0 orders; basket would be the top momentum majors (e.g. FORM/PENDLE/LTC).
16. **Beta core WIRED into the tick — gated OFF (21/6, commit `cfd4e47`, deployed)** — `_event_decision` now runs the barbell when `BETA_CORE_ENABLED` (default **false**): beta owns MAJORS (momentum basket), the sniper owns MEMES only. Class ownership via a `manage_classes` filter threaded through `sniper.run`/`decide_exits`/`decide_breakout_entries` (each sleeve only manages + counts slots for its own classes). No double-spend: beta runs first, the meme sniper sees stable cash reduced by beta's net deploy, and beta reserves `BETA_CORE_CASH_RESERVE_USD` ($10) so memes still fire. Daily breaker `block_entries` suppresses NEW entries in both sleeves without flattening. Momentum by CMC id. **Flag OFF ⇒ `manage_classes=None` ⇒ behavior byte-identical to before** (live agent unchanged, no restart). +3 tests (442 pass). Validated by a one-off DRY tick with the flag on → `strategy=sniper:cautious+beta:beta`, 0 orders in CAUTIOUS, no errors. **TO ACTIVATE mid-week: set `BETA_CORE_ENABLED=true` in the VPS `.env` + restart** (after `beta_diag` soak + the market is actually trending). Tune `beta_core_*` knobs as needed.

---

## 3. REMAINING TO WIN (user actions — code is done)

- [ ] **Paste the rewritten BUIDL** on DoraHacks (shorter, professional, NO exact strategy numbers = no "lộ bài"). Add the dashboard link. (The full text was given in chat; regenerate if needed — keep numbers out.)
- [ ] **Post #CMCAgentHub** on X (tag @CoinMarketCap) for the $2k side prize — code requirement is met; can link the dashboard.
- [ ] **Repo PUBLIC before 29/6** — but keep it **PRIVATE during the trading week (22–28/6)** to protect the strategy from competitors. Also delete the old `TONiE8668` repo + revoke its PAT.
- [ ] **22/6 00:00 UTC:** `panic --live` → `reset` → keep live (see §1 — flatten orphans to USDT first, NOT a bare reset).
- Optional/nice: a demo video; update README/BUIDL to mention the Claude advisor (now a true claim).

---

## 4. LOCKED DECISIONS (don't revisit/revert without a strong reason — and never SILENTLY)

- **Strategy = confirmed-momentum + ride** with the table in §2. Exits TP/stop/trailing/**breakeven** only (NO time exit).
- **BARBELL IS NOW LIVE — beta core ENABLED + graduated (user decision 21/6).** `BETA_CORE_ENABLED=true` in the VPS `.env`. The agent FLEXES with the regime (BTC+F&G+Claude), it does NOT bet a market direction: **RISK_ON** = full beta basket (3 majors) + meme lottery (2), **CAUTIOUS** = light beta (1 major) + meme (1), **RISK_OFF** = all cash. Beta owns majors (momentum-basket hold), sniper owns memes (lottery); meme cash reserve is regime-scaled (`meme_order_usd × regime meme slots`). **Beta params (after the 21/6 param audit):** entry `min_momentum` **4%** blended (was 2% = noise → whipsaw with the tight trail); RISK_ON `max_names` **2** / CAUTIOUS 1 (3 was unreachable at ~$33 NAV — floor+reserve+3×20% > stable; bump to 3 once equity > ~$45); per-name **20%** NAV; meme cash reserve = **1 ticket ($5)** so 2 majors deploy + memes still get a shot. **Exits:** trailing **6%** (was 12% — wider than meme's 10%, gave back the whole +10-20% move; watch for whipsaw), hard stop **−8%** (paired with the 6% trail), breakeven **+5%**, momentum-lost rotation (held while in top `max_names×2`), regime/breaker flatten — **NO hard TP (rides)**. Claude stays **tightening-only** (the deterministic BTC classifier makes the up/deploy call; Claude can only step risk down). **LIVE-VALIDATED 21/6 17:04 UTC:** in CAUTIOUS the agent auto-deployed its 1 light major **FORM** (top momentum +6.7%/24h) via 1inch (tx `0x4902ca…`), tracked as a major position, no errors. (The 22/6 contest-start `panic` flattens it; beta stays enabled through `reset` → fresh adaptive start.)
- **STRATEGY DIRECTION = PHASED BARBELL (user decision 21/6).** Framing: Track-1 is a 7-day raw-return *tournament* with a 30% DD gate → you don't win from cash (median = ~0% = mid-pack); you need the right tail, bounded by the DD gate. The barbell = **(A) a beta core** (regime-gated long basket of strong momentum majors, hold+trail — the reliable up-week return source) **+ (B) a meme lottery** (small fixed, convex tail ticket); the momentum **scalp middle is the bleeder to demote**. **Phase 1 (live now, 22/6):** current sniper + the two safe fixes (breakeven stop, meme +3%); observe whether we're stuck in cash during a green week. **Phase 2 (mid-week):** the beta core is now **BUILT + soakable, gated OFF** (see §2 item 14) — brain (`aegis/beta_core.py`) + soak tool (`scripts/beta_diag.py`) + config, all tested, live agent unchanged. Items #4 (meme adverse-selection) and #5 (breakeven) are ADDRESSED. **Beta core is now WIRED into the live tick (gated OFF — see §2 item 16).** To activate mid-week: just set `BETA_CORE_ENABLED=true` in the VPS `.env` + restart, after `beta_diag` soak confirms selection quality and the market is actually trending. No more code needed to flip it on.
- **Execution = 1inch** (live-proven, self-custody calldata signing). OpenOcean = keyless backup; PancakeSwap-18 (`tradable_alpha.bak.json` + `EXECUTION_BACKEND=pancake`) = emergency fallback + BNB/WBNB on-chain pricing. **TWAK** (`twak_executor.py`) is a WORKING backend on a SEPARATE Trust Wallet wallet (the Trust Wallet partner leg) — NOT a collision (user runs 2 wallets). Contest wallet registered directly on the hackathon contract (NOT via twak).
- **Pricing = CMC-by-id** (on-chain V2 is garbage for the aggregator universe). Universe = 91 tradable (56 majors + 35 Alpha memes) in `data/tradable_alpha.json`.
- **Claude = tightening-only, hourly, advisory, fail-safe.** Never let it loosen risk or enter the 60s hot path.
- **Breaker:** alert −20% (latched after 3 consecutive breach ticks), cap −30% (instant). Valuation from on-chain balances (last-known-good fallback). Self-custody is absolute. PLUS a **daily soft breaker** (default 8% intraday DD → halt NEW entries till 00:00 UTC; exits/compliance still run) — bounds churn-bleed far below the −20% latch.
- **DON'T LỘ BÀI:** keep exact thresholds OUT of the public BUIDL and the public dashboard (the dashboard strategy panel is already genericized).

---

## 5. HOW TO OPERATE

```bash
# health
ssh -i "$USERPROFILE/.ssh/hostinger_openclaw" root@2.25.184.43 \
  "systemctl status agent --no-pager | head; journalctl -u agent -n 20 --no-pager"
# deploy latest main → VPS
ssh ... "cd /home/agent/bnbhack-track1-agent && systemctl stop agent && \
  sudo -u agent git fetch origin -q && sudo -u agent git reset --hard origin/main && systemctl start agent"
sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent signals     # live CMC Agent Hub read
sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent compliance   # min-trade report
sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent panic --live # KILL-SWITCH → USDT
sudo -u agent env PYTHONPATH=. .venv/bin/python scripts/scan_diag.py      # read-only signal scan
```

- Honest caveat (unchanged): the momentum **edge is unproven**; the engineering minimizes operational + DQ risk, not market risk. Winning the main prize depends on catching a real move in the 22–28/6 window.
- Build standard: TDD + security-hardening + code-review per module. **Be a flexible thinking partner — retain decisions across the work, never silently revert an agreed tuning.**
