# HANDOFF — Aegis Track-1 Trading Agent (BNB Hack 2026)

> **GOAL: win the Track-1 main prize** (ranked by RAW total wallet return over the contest window).
> Written 2026-06-20 ~22:30 UTC. Go-live = **2026-06-22 00:00 UTC**. Read this once → you have 100% context.

---

## 0. ENVIRONMENT (where everything lives)

- **Local repo:** `E:\Track1-trade-onchain` (Windows; Git Bash + PowerShell available)
- **Remote (live bot):** VPS `root@2.25.184.43`, agent dir `/home/agent/bnbhack-track1-agent`
  - SSH: `ssh -i "$USERPROFILE/.ssh/hostinger_openclaw" root@2.25.184.43`
  - Run agent commands as the `agent` user: `sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent <cmd>`
  - Service: systemd unit **`agent`** (`systemctl {status|stop|start} agent`, logs `journalctl -u agent`)
- **Git:** GitHub `github.com/tonielivegood/aegis-trading-agent`. Work branch **`harden/breaker-and-pricing`**,
  pushed to **`main`** (VPS does `git fetch && git reset --hard origin/main`). ~19 commits on 20/6.
- **Wallet (registered, self-custody):** `0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa` — ~$36
  (≈ $24 USDT + ~$4 ETH from the smoke + ~$7.5 BNB gas/0.012 + dust). `.env` holds `AGENT_PRIVATE_KEY`.
- **`.env` (gitignored; on BOTH local + VPS):** `ONEINCH_API_KEY`, `EXECUTION_BACKEND=1inch`, `CMC_API_KEY`
  (Pro plan), `BSC_RPC_URL` (NodeReal), `AGENT_PRIVATE_KEY`, `BINANCE_WEB3_API_KEY` (unused),
  `TELEGRAM_*`. Mask secrets in any output; never commit `.env`.

---

## 1. CURRENT STATUS — DONE ✅

- **Universe: 91 tradable tokens** (56 majors + 35 memes), rebuilt 20/6 from the official 149-eligible set via
  **1inch aggregator slippage** (up from ~14 effectively-tradable on PancakeSwap-V2-only). Majors incl
  ETH/XRP/TRX/DOGE/ADA/LINK/BCH/AVAX/DOT/UNI/AAVE/ATOM/FIL/BONK/CAKE...
- **Execution: 1inch** (`EXECUTION_BACKEND=1inch`) — **LIVE-PROVEN** with a real on-chain tx:
  `0x2727f6d5337a60c1ec2991258fa36c8deaf2652c908743dd29cf3186b11e7d6c` (status 0x1, routed through 1inch
  AggregationRouterV6 `0x1111…842a65`, $4 USDT→0.002294 ETH). Self-custody: 1inch returns calldata, WE sign locally.
- **Pricing: CMC-by-id** (`cmc_client.get_prices_by_id`) — on-chain V2 price is GARBAGE for these tokens
  (AAVE read $0.81 vs ~$76). CMC verified correct (AAVE $76, AVAX $6.1, UNI $3.03 == 1inch). BNB/WBNB stay on-chain.
- **Strategy: two-tier by cost** + regime beta valve:
  - MAJOR (cheap ~0.6% round-trip) = ACTIVE/modest: vol≥2.0× (RISK_ON ×0.75=1.5×), TP +10%, trail 5%, stop −5%, no-prog 20m.
  - MEME (expensive) = RARE/big ride: vol≥3.0× (strict every regime), TP **+100%**, trail 15%, stop −8%, no-prog 25m, $5 size.
  - The 35 memes are the **asymmetric-tail win-lever** (memecoin contests are won by one big +100-300% meme hit).
- **Hardening:** breaker DEBOUNCE (latches only after 3 consecutive breach ticks — a 1-tick price glitch no longer
  DQs us); last-known-good price fallback (`_apply_price_fallback`); kill-switch (`panic`); gas guard; min-trade
  compliance; full-wallet valuation (`valuation_tokens` = core∪alpha).
- **Soak:** new setup ran ~6h DRY, equity stable ~$36.15, drawdown ~0, **ZERO errors**, tick ~8s.
- **Tests:** **387 pass / 2 skip, ruff clean.** Bot currently **DRY soak** on the 91-token/1inch/CMC setup.

⚠️ **Observed:** service ticks show `dry_run=True` (DRY). The systemd unit ExecStart is `run --live` but DRY_RUN
env / scheduler forces dry. **The exact DRY→LIVE flip mechanism is TODO #1 to verify (see §3).**

---

## 2. KEY FILES / SCRIPTS (full paths from repo root `src/agent/...`)

**Universe & token data**
- `src/agent/data/tradable_alpha.json` — **THE LIVE UNIVERSE** (91 tokens: symbol, contract, decimals, token_class, slippage_12usd, id)
- `src/agent/data/tradable_alpha_new.json` — full 103-token 1inch scan (incl 12 memes NOT on Binance Alpha = no volume, parked)
- `src/agent/data/tradable_alpha.bak.json` — **Pancake-18 SAFE FALLBACK** (use with `EXECUTION_BACKEND=pancake`)
- `src/agent/data/alpha_symbol_map.json` — contract→Binance-Alpha-symbol (meme 1m volume); 53 entries
- `src/agent/data/eligible_resolved.json` — 147 eligible: symbol, CMC id, contract, vol24h, token_class (source of truth for ids)
- `src/agent/data/eligible_tokens.json` — official allowlist by contract (`is_eligible`)
- `src/agent/data/curated_core.json` — 20 core blue-chips (WBNB/BTCB/etc., for gas + valuation)
- `src/agent/data/runtime/` — drawdown.json, baseline.json, aegis_positions.json, aegis_cooldown.json,
  regime.json, track1_compliance.json, last_prices.json, market_cache.json (cleared by `reset`)

**Execution backends** (all share `.swap(token_in, token_out, amount_human) -> SwapResult`)
- `src/agent/execution/oneinch.py` — **1inch v6 (ACTIVE, live-proven)**. Needs `ONEINCH_API_KEY`. Approves router `0x1111…842a65`.
- `src/agent/execution/openocean.py` — OpenOcean (keyless alternative aggregator)
- `src/agent/execution/pancakeswap.py` — PancakeSwap V2 (default/fallback) + `ERC20_ABI`, `SwapResult`, `_to_hex`
- `src/agent/execution/twak_executor.py` — Trust Wallet Agent Kit CLI (unused)
- `src/agent/execution/tx_builder.py` — wei/slippage/deadline helpers
- `src/agent/execution/binance_web3.py` — Binance Wallet connectivity stub (NOT built; swap spec behind portal login)
- backend selection: `agent_loop._make_executor` (reads `settings.execution_backend`: pancake|twak|openocean|1inch)

**Pricing**
- `src/agent/data/cmc_client.py` — `get_quotes` (by symbol), **`get_prices_by_id`** (by CMC id, used for the universe)
- `src/agent/data/price_feed.py` — on-chain PancakeSwap price (BNB/WBNB only now) + CMC fallback
- `src/agent/agent_loop.py` → **`_event_prices`** (CMC-by-id universe pricing) + **`_apply_price_fallback`** (last-known-good)

**Strategy (aegis)**
- `src/agent/aegis/sniper.py` — orchestrator (`run`, `_scan_by_class`); exits→cooldown→regime-gated entries
- `src/agent/aegis/token_class.py` — **two-tier params** (MAJOR active / MEME ride). TUNE HERE.
- `src/agent/aegis/regime.py` — regime params (RISK_ON 35%/2, CAUTIOUS 20%/1) + **`entry_vol_factor`** (RISK_ON 0.75 beta valve) + BTC classifier
- `src/agent/aegis/volume_breakout.py` — `scan_breakouts` + `decide_breakout_entries` (gates: eligible, cooldown, floor, slots, meme_usd sizing)
- `src/agent/aegis/market_feed.py` — `MarketFeed` snapshots; **static slippage gate** via `token_list.tradable_slippage`
- `src/agent/aegis/binance_alpha_volume.py` — meme 1m volume (Binance Alpha klines, via alpha_symbol_map)
- `src/agent/aegis/binance_spot_volume.py` — major 1m volume (Binance spot klines, data-api.binance.vision)
- `src/agent/aegis/cooldown.py`, `positions.py`, `compliance.py`

**Risk / safety**
- `src/agent/risk/drawdown.py` — **debounced breaker** (`latch_ticks`, alert 0.20, cap 0.30)
- `src/agent/risk/portfolio.py` — `valuation_tokens` (core∪alpha), `read_onchain_balances` (multicall), `Portfolio.equity`
- `src/agent/agent_loop.py` → `flatten_to_cash` (kill-switch), `_make_executor`, `tick`

**Config**
- `src/agent/config.py` — all knobs (see §5). `__main__.py` = CLI (`status|reset|tick|run|panic|compliance|notify-test`)

**Scripts**
- `scripts/build_alpha_symbol_map.py` — refresh meme→Binance-Alpha volume map (run when memes change)
- `scripts/rebuild_universe_1inch.py` — rebuild universe via 1inch slippage (writes `tradable_alpha_new.json`)
- `scripts/build_universe_from_resolved.py`, `build_alpha_universe.py` — older universe builders
- `scripts/slippage_scan.py`, `scripts/wallet_check.py`, `scripts/scan_diag.py` — diagnostics (read-only)
- **`/root/go-live.sh`** (on VPS) — reset + flip live + 3 Telegram reminder timers (21/6 12:00, 21/6 22:00, 22/6 00:00 UTC)

---

## 3. REMAINING TO "WIN" (prioritized roadmap)

**MUST DO before 22/6 (verify, not build):**
- [ ] **Verify `go-live.sh`** works end-to-end with the NEW setup: `reset` must NOT break the 91-token/1inch/CMC config,
      and the DRY→LIVE flip must actually set `dry_run=False` (confirm the unit/DRY_RUN mechanism — see §1 ⚠️).
      Test on VPS: run go-live.sh path in a controlled way, confirm one live tick prices via CMC + would route 1inch.
- [ ] **Repo PUBLIC** before 29/6 (judges must see the code). Also: delete the old TONiE8668 repo + revoke its PAT.
- [ ] Confirm gas (BNB ~0.012 = ~1000+ swaps @ 0.05 gwei = plenty) + wallet funded.

**BUILD to raise win odds (full power, agreed priority order):**
1. **(done-ish) go-live readiness** — verify above.
2. **CMC Agent Hub skill** → **$2k side prize** (#CMCAgentHub) + better regime/catalyst signal. CMC AI Agent Hub:
   MCP / x402 ($0.01/req USDC-on-Base, no key) / REST. Catalog open-source on GitHub. Wire one skill (sentiment/
   trending) into the hourly regime + token selection. (Docs: coinmarketcap.com/api/documentation/ai-agent-hub)
3. **Claude catalyst layer** → news-driven entry bias (the original sniper alpha; currently volume-only). Hourly
   Claude reads CMC/social → flags a token with a real catalyst → bias entry. More build; keep Claude OUT of the
   60s hot path (rails stay mechanical).

---

## 4. TIMELINE
- **Today:** 2026-06-20 (DRY soak running, product feature-complete)
- **Go-live:** **2026-06-22 00:00 UTC** (= 07:00 VN) — run `bash /root/go-live.sh`
- **Contest window:** 22–28/6 (ranked by raw total return; drawdown ≥30% = DQ gate)
- **Repo public + final submission housekeeping:** before **29/6**

---

## 5. LOCKED DECISIONS (do NOT revisit without a strong reason)

- **Universe = the 91 tokens in `tradable_alpha.json`** (56 majors + 35 Alpha-memes). Don't expand unless you
  rebuild via 1inch slippage AND add Binance-Alpha volume mapping. The 12 no-Alpha memes stay parked.
- **Execution = 1inch** (live-proven, self-custody calldata signing). Do NOT switch to a CEX or another DEX.
  OpenOcean is the keyless backup; PancakeSwap-18 (`tradable_alpha.bak.json` + `EXECUTION_BACKEND=pancake`) is the
  emergency fallback only.
- **Pricing = CMC-by-id** as primary (on-chain V2 prices are unreliable for the aggregator universe). BNB/WBNB on-chain.
- **Slippage:** `SLIPPAGE_BPS=400` (4% gate+exec for majors) · `MEME_SLIPPAGE_BPS=600` (6%) · `MEME_ORDER_USD=5`.
- **Sizing:** RISK_ON 35%/2 slots (`entry_vol_factor` 0.75) · CAUTIOUS 20%/1 · RISK_OFF 0. Total deploy ≤70% NAV.
- **Exits — MAJOR:** TP +10% / trail 5% / stop −5% / no-prog 20m. **MEME:** TP +100% / trail 15% / stop −8% / no-prog 25m.
- **Breaker:** alert 20% (latched after **3 consecutive** breach ticks), cap 30% (instant). Never lower latch_ticks to 1.
- **Compliance:** ≥1 valid (eligible-by-contract) trade/day, fires after hour 20 UTC, order $10, picks deepest eligible.
- **Self-custody is absolute:** the agent signs locally; never hand a key/seed to any API. Claude does NOT manually
  broadcast txs — only the agent's own commands (run/panic/go-live.sh) move real money, at the user's direction.

---

## 6. HOW TO OPERATE (quick reference)

```bash
# status / health
ssh -i "$USERPROFILE/.ssh/hostinger_openclaw" root@2.25.184.43 \
  "systemctl status agent; journalctl -u agent -n 20 --no-pager"

# deploy latest code to VPS
ssh ... "cd /home/agent/bnbhack-track1-agent && systemctl stop agent && \
  sudo -u agent git fetch origin -q && sudo -u agent git reset --hard origin/main && systemctl start agent"

# one DRY tick (no real money) / verify pricing
sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent tick

# KILL-SWITCH (emergency: sell all non-stable → USDT)
sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent panic --live

# GO-LIVE (22/6)
bash /root/go-live.sh
```

Tests: `python -m pytest -q` (387 pass) · lint: `python -m ruff check src tests`.
Build standard: TDD + security-hardening + code-review per module. Convert relative dates to absolute.
