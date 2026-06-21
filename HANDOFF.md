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
   | **MEME** | 4× | **+6%** | **+80%** | **10%** | **−8%** | **$5 fixed lottery** |

   (Meme +6%/+80%/10%/−8% are user-tuned this session: memes wiggle on +3% noise; wide trail gave back too much.)
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
12. **Signal fix — breakout% now from same-source kline move (21/6, commit `7bafb89`, deployed + live-verified)** — the entry gate measured the 5m price move from a tick-sampled CMC price cache, which lags thin Alpha memes → bought the top as the spike faded (RAVE/GUA). Now `MarketSnapshot.breakout_pct` is fed the `price_change_5m_pct` from the SAME klines as the volume (Binance Alpha for memes / spot for majors) via a new `volume_and_move()` provider method (one fetch, 3-tuple). `scan_breakouts`/dashboard/`scan_diag` all use the shared `breakout_pct()` helper; falls back to the cache when no kline move is supplied. +6 tests (418 pass). `scan_diag` on the VPS confirmed real kline moves (BEAT −2.7%, FORM +0.7%) with discipline intact (GENIUS 242% of vol bar but flat → no fire).

---

## 3. REMAINING TO WIN (user actions — code is done)

- [ ] **Paste the rewritten BUIDL** on DoraHacks (shorter, professional, NO exact strategy numbers = no "lộ bài"). Add the dashboard link. (The full text was given in chat; regenerate if needed — keep numbers out.)
- [ ] **Post #CMCAgentHub** on X (tag @CoinMarketCap) for the $2k side prize — code requirement is met; can link the dashboard.
- [ ] **Repo PUBLIC before 29/6** — but keep it **PRIVATE during the trading week (22–28/6)** to protect the strategy from competitors. Also delete the old `TONiE8668` repo + revoke its PAT.
- [ ] **22/6 00:00 UTC:** `panic --live` → `reset` → keep live (see §1 — flatten orphans to USDT first, NOT a bare reset).
- Optional/nice: a demo video; update README/BUIDL to mention the Claude advisor (now a true claim).

---

## 4. LOCKED DECISIONS (don't revisit/revert without a strong reason — and never SILENTLY)

- **Strategy = confirmed-momentum + ride** with the table in §2. Exits TP/stop/trailing only (NO time exit).
- **Execution = 1inch** (live-proven, self-custody calldata signing). OpenOcean = keyless backup; PancakeSwap-18 (`tradable_alpha.bak.json` + `EXECUTION_BACKEND=pancake`) = emergency fallback + BNB/WBNB on-chain pricing. **TWAK** (`twak_executor.py`) is a WORKING backend on a SEPARATE Trust Wallet wallet (the Trust Wallet partner leg) — NOT a collision (user runs 2 wallets). Contest wallet registered directly on the hackathon contract (NOT via twak).
- **Pricing = CMC-by-id** (on-chain V2 is garbage for the aggregator universe). Universe = 91 tradable (56 majors + 35 Alpha memes) in `data/tradable_alpha.json`.
- **Claude = tightening-only, hourly, advisory, fail-safe.** Never let it loosen risk or enter the 60s hot path.
- **Breaker:** alert −20% (latched after 3 consecutive breach ticks), cap −30% (instant). Valuation from on-chain balances (last-known-good fallback). Self-custody is absolute.
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
