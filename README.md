# BNB Hack 2026 — Track 1 Autonomous Trading Agent

A self-custody autonomous trading agent for BNB Hack 2026 Track 1. Trades live on
BSC through the **1inch DEX aggregator** during the contest window (22–28 June 2026),
scored on raw total wallet return with a hard 30% max-drawdown disqualification gate.

**Design philosophy: survival first, then asymmetric upside.** Track 1 ranks on raw
return but disqualifies anyone who draws down 30%. Aegis is built to *never get
disqualified*, sit in cash by default, and deploy only into confirmed momentum —
chasing a few asymmetric winners rather than churning many scalps into fees.

---

## Why this matters (Aegis differentiator)

Aegis is **not a black-box trading bot** but a risk-managed autonomous agent that
sits in cash, watches real money-flow, and deploys only when safety gates pass —
with **self-custody preserved end to end** (it signs every transaction locally and
never hands a key to any API). It combines:

1. **1inch DEX aggregator execution** — best price across all BSC DEXs (V2/V3/
   Biswap/THENA…), unlocking a tradable universe (~91 tokens) far larger than any
   single DEX. 1inch returns ready-to-sign calldata; **Aegis signs it locally**.
   Live-proven on-chain (see below). OpenOcean is a keyless backup; PancakeSwap V2
   is the emergency fallback **and** the on-chain price source for BNB/WBNB.
2. **CoinMarketCap Pro pricing** — the aggregator universe is priced by CMC id
   (on-chain V2 prices are unreliable for these thin-pool tokens), so wallet
   valuation and the drawdown breaker never trip on a bad on-chain read.
3. **CoinMarketCap AI Agent Hub** — market Fear & Greed + community-trending feed
   the regime and token ranking (see the #CMCAgentHub section below).
4. **Live volume confirmation** — Binance Alpha 5-minute klines (memes) + Binance spot
   klines (majors); no entry without a real volume spike on a confirmed move. No faked volume, ever.
5. **Aegis risk engine** — regime valve, two-tier exits, debounced drawdown breaker,
   last-known-good pricing, kill-switch, gas guard, DRY_RUN-by-default.

Signing stays **local / self-custody**; the agent never hands out a key or seed,
and **`DRY_RUN=true` by default**.

---

## Track 1 strategy — confirmed-momentum sniper, then ride

For Track 1, Aegis trades **only the official 149-token BEP-20 allowlist** (matched
by **contract address**), on the liquid, aggregator-routable subset (~91 tokens =
56 majors + 35 Binance-Alpha memes). It is **cash by default** and enters only on a
*confirmed* move, sized by an hourly market regime.

**Entry** requires *all*: eligible by contract + aggregator-liquid + a real volume
spike on **5-minute candles** (≥ class bar × baseline) + price already up **≥3%**
(the move is confirmed, not a one-minute blip) but not already blown off + the regime
allowing new risk + cooldown clear + a free slot + the stablecoin floor respected.
A flat/quiet token never trades; a single noisy candle is not enough.

**Two tiers** (`aegis/token_class.py`):

| Tier | Volume bar | Confirm | Take-profit | Trail | Stop | Size |
|---|---|---|---|---|---|---|
| **MAJOR** (deep liquidity) | ≥ 2.5× | price +3% | +30% cap | 7% | −7% | regime % of NAV |
| **MEME** (thin, explosive) | ≥ 4.0× | price +3% | **+200% cap** | 25% | −12% | $5 lottery |

A major having a good day routinely runs +10–30%; the 35 memes are the
**asymmetric-tail win lever** (memecoin contests are won by one big +100–300% hit), so
memes ride far on a wide trail while majors lock a tighter, more frequent gain.

**Regime valve** (`aegis/regime.py`, refreshed hourly from CMC BTC momentum + the
Agent Hub Fear & Greed read): `RISK_ON` 35% NAV / 2 slots · `CAUTIOUS` 20% / 1 slot ·
`RISK_OFF` 0 (no new entries). The regime throttles **exposure** (size/slots), never
the signal bar. Total deploy ≤ 70% NAV, leaving a 30% cushion under the DQ breaker.

**Exits are take-profit, hard-stop, and trailing only — there is no time-based exit.**
A position rides until it hits its cap, trails out from its peak, or stops out; the
global drawdown breaker overrides everything and flattens to cash. (An earlier
time-based no-progress exit was removed after a live soak proved it churned.)

The majors-hold strategy below is the **validated deep fallback**, available when the
sniper path is disabled.

## Why this can win Track 1

- **Right universe, by contract address.** Aegis trades only the official allowlist
  and proves it with an anti-DQ test (`tradable ⊆ eligible`).
- **Bigger tradable universe.** The 1inch aggregator unlocks ~91 routable tokens
  (vs ~18 on PancakeSwap V2 alone), including the meme tail that actually pumps.
- **Real confirmation, not vibes.** Live Binance 5-minute volume gates every entry.
- **Survival first.** A 30% drawdown disqualifies; Aegis defaults to cash, caps
  deployment at 70% NAV, sizes memes at $5, and runs a latched, debounced breaker.
- **Self-custody + on-chain proof.** Keys stay local; execution routes through the
  1inch AggregationRouterV6, **proven live** with a real BSC transaction:
  [`0x2727…7d6c`](https://bscscan.com/tx/0x2727f6d5337a60c1ec2991258fa36c8deaf2652c908743dd29cf3186b11e7d6c).

## CMC AI Agent Hub integration (#CMCAgentHub)

Aegis consumes two **CoinMarketCap AI Agent Hub** REST skills
([docs](https://coinmarketcap.com/api/documentation/ai-agent-hub)) via our Pro key —
see [`src/agent/data/cmc_agent_hub.py`](src/agent/data/cmc_agent_hub.py):

| Agent Hub skill | Endpoint | How Aegis uses it |
| --- | --- | --- |
| **Market sentiment** | `GET /v3/fear-and-greed/latest` | Refines the hourly regime, **tightening-only**: extreme fear (F&G ≤ 20) forces `RISK_ON → CAUTIOUS`. It can never make the agent *more* aggressive (greed ≠ green light). |
| **Community trending** | `GET /v1/community/trending/token` | Re-ranks already-qualified volume breakouts (1.5× boost), steering the scarce position slots toward tokens with real community attention. Never opens a position on its own. |

Both run **out of the 60s hot path** (fetched hourly, cached to
`data/runtime/cmc_signal.json`) and **fail safe** — a network hiccup returns
`None`/empty, so the agent falls back to its momentum-only behaviour. See it live:

```bash
python -m src.agent signals
```

## What is live vs DRY_RUN

| Capability | Status |
|---|---|
| Binance 5m/1m volume (Alpha memes + spot majors) | **LIVE** (read-only) — verified |
| CoinMarketCap Pro pricing (universe priced by CMC id) | **LIVE** (read-only) |
| CoinMarketCap AI Agent Hub (Fear & Greed + trending) | **LIVE** (read-only) — `python -m src.agent signals` |
| 1inch aggregator quote / route / slippage gate | **LIVE** (read-only quotes) |
| Trade execution (1inch, self-custody local signing) | **LIVE-PROVEN** on-chain; **DRY_RUN by default** until go-live |
| Transaction signing / broadcast | **never hands out a key**; local/self-custody only |
| Trust Wallet Agent Kit (TWAK) execution backend | **working** — `EXECUTION_BACKEND=twak` routes `.swap()` through the `twak swap … --chain bsc` CLI on a dedicated Trust Wallet wallet; 1inch is the scoring-wallet path |
| Binance Web3 API | adapter present but **deferred/unused** |

## Track 1 compliance (eligibility + minimum trades)

- **Eligible universe:** a fixed **149-token BEP-20 allowlist** on CoinMarketCap.
  **Trades outside the list do not count** — Aegis matches by **BSC contract
  address**, never symbol alone, and a test enforces `tradable ⊆ eligible`.
- **Minimum trades:** ≥**1 valid trade/day** and ≥**7 over the trading week**.
  A `ComplianceTracker` counts only valid eligible-by-contract trades; as a
  late-day safety net it makes ONE minimum-size, fully risk-gated eligible trade
  (`MIN_TRADE_COMPLIANCE`) if a day would otherwise lapse — or safe-skips
  (`COMPLIANCE_UNMET_SAFE_SKIP`) instead of forcing a bad trade. Report:
  `python -m src.agent compliance`.
- **Scoring (honest):** public sources say Track 1 ranks on total return with a
  max-drawdown cap, minimum trade count, and simulated costs — but whether
  scoring is total wallet NAV, only eligible-token holdings, or PnL from valid
  eligible trades is **not fully confirmed**. Aegis therefore **does not
  hard-code any stablecoin-NAV assumption**; it keeps all trade activity inside
  the allowlist and treats stablecoin as configurable settlement/risk parking.

## Safety constraints (anti-disqualification)

- `DRY_RUN=true` by default — no transaction is broadcast.
- Signing is local/self-custody; `BINANCE_WEB3_BROADCAST_ENABLED` defaults `false`.
- Track-1 mode trades **only** the eligible allowlist (enforced by gates + test).
- Drawdown breaker (latched) exits to stable well under the 30% DQ cap.
- Secrets are read from the local environment only, masked in all output
  (`abc123...xyz789`), never logged in full. `.env` and `data/runtime/` are gitignored.

---

## Architecture

```
data/      market data: RPC (multi-endpoint failover), CMC quotes + AI Agent Hub, price feed, token universe
aegis/     volume-breakout sniper: regime valve, two-tier params, market feed, positions, cooldown, orchestrator
risk/      debounced drawdown breaker, position sizing, trade counter, portfolio valuation, input guards
execution/ 1inch (live) + OpenOcean + PancakeSwap V2 (fallback + BNB pricing) + TWAK (Trust Wallet backend); local self-custody signing
signal/    momentum + Claude sentiment, behind a prompt-injection firewall (research; not in the live hot path)
strategy/  event-driven alpha momentum (sniper) + adaptive fractional-hold fallback + variants
monitor/   safeguard (derisk/halt/compliance), contest PnL accounting, structured logging
backtest/  engine, metrics, Binance data loader, walk-forward validation
agent_loop.py  orchestrator: data → pricing (CMC) → risk → regime → sniper → execution (1inch)
scheduler.py   APScheduler: event tick every EVENT_TICK_SECONDS (60s)
__main__.py    CLI (status | signals | compliance | reset | tick | run | panic)
```

## Baseline fallback strategy (validated)

> This majors-hold strategy is the **deep fallback** and the original
> walk-forward-validated core. The **live Track-1 strategy is the cash-default
> volume-breakout sniper above** (`STRATEGY_MODE=event_alpha`, the default).

**Adaptive fractional hold + breaker** — chosen by walk-forward validation over
111 weekly windows (~125 days of real Binance data):

- Deploy `DEPLOY_FRAC` (default 50%) of equity into the top-`BASKET_SIZE`
  (default 6) most-liquid majors (BTCB, ETH, WBNB, CAKE, XRP, ADA), equal weight.
- Keep the rest in USDT (stablecoin reserve).
- Hard drawdown breaker: at −20% drawdown, exit all risk to stablecoin (latched).
- Per-token cap 10% of equity; never breaches the stablecoin floor.

Validated profile: avg −0.4%/week, worst −10.3%, best +4.7%, **worst drawdown
12.8%** (vs 30% DQ cap), **0% disqualification across all windows**.

> **Signal layer status:** the live sniper is driven by real volume + the CMC
> regime/Agent-Hub signals; the older `signal/` Claude-sentiment engine (with its
> prompt-injection firewall) is built and tested but kept OUT of the 60s hot path —
> available for research and sentiment-driven variants, not the live decision.

> Honest note: no tested strategy showed large reliable alpha in this regime.
> The edge here is *survival* — while aggressive competitors blow up on the 30%
> cap, this agent caps drawdown near 13% and catches up-weeks. The backtest
> tooling is reusable to keep researching better strategies.

## Risk controls

| Control | Value | Purpose |
|---|---|---|
| Drawdown breaker (latched, **debounced**) | alert −20% after 3 consecutive breach ticks; cap −30% instant | survive a 1-tick price glitch without a phantom DQ |
| Last-known-good pricing | `last_prices.json` fallback | a failed price read can't crater valuation/trip the breaker |
| Deploy cap | ≤ 70% NAV (RISK_ON 2×35%) | always keep a cash cushion under the DQ cap |
| Meme position size | $5 fixed lottery | thin pools can't take full size; bounded downside |
| Stablecoin floor | max($6, 15% NAV) | settlement cash never drains |
| Slippage gate + min_out | 4% majors / 6% memes | anti-MEV; rejects illiquid routes |
| Gas guard | block new buys below `MIN_GAS_BNB` | never get stuck unable to exit |
| Token approval | exact amount | no unlimited allowance |
| Kill-switch | `python -m src.agent panic --live` | flatten everything to USDT on demand |
| DRY_RUN gate | env flag / `run --live` | no transaction sent unless explicitly live |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in secrets (see below)
```

Required in `.env` (never commit it — it is gitignored):

- `AGENT_PRIVATE_KEY`, `AGENT_WALLET_ADDRESS` — the agent wallet (self-custody)
- `CMC_API_KEY` (Pro plan — pricing + AI Agent Hub), `BSC_RPC_URL`, `BSCSCAN_API_KEY`
- `EXECUTION_BACKEND=1inch` + `ONEINCH_API_KEY` — the live execution path
- Risk dials: `MAX_DRAWDOWN_ALERT=0.20`, `MAX_DRAWDOWN_CAP=0.30`, `SLIPPAGE_BPS=400`,
  `MEME_SLIPPAGE_BPS=600`, `MEME_ORDER_USD=5`, `DRAWDOWN_LATCH_TICKS=3`
- `ANTHROPIC_API_KEY`, `TELEGRAM_*` (optional)
- `DRY_RUN=true` (keep true until go-live; `go-live.sh` flips the service to `run --live`)

## Commands

```bash
python -m src.agent status     # wallet, equity, balances, mode
python -m src.agent signals    # live CMC AI Agent Hub signals + resulting regime (#CMCAgentHub)
python -m src.agent reset      # clear runtime state (drawdown peak + trade ledger)
python -m src.agent tick       # one tick, DRY-RUN (live data, no money moved)
python -m src.agent tick --live   # one tick, LIVE (sends real transactions)
python -m src.agent run        # scheduler loop, DRY-RUN
python -m src.agent run --live    # scheduler loop, LIVE trading
```

Registration (already done; idempotent check):

```bash
python -m src.agent.registration.register_agent --check
```

Backtest / research:

```bash
python scripts/run_backtest.py       # compare strategies on recent history
python scripts/run_walkforward.py    # walk-forward distribution across windows
python scripts/soak_test.py          # accelerated operational soak (dry-run)
```

---

## Telegram alerts (optional)

Get push alerts for breaker trips, trades, errors, and an hourly heartbeat —
no inbound ports, send-only.

1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Message your new bot once (say "hi"), then get your **chat id**: message
   **@userinfobot**, or open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.
3. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```
4. Verify: `python -m src.agent notify-test` → you should receive a test message.

Leave both blank to disable (the agent runs fine without it).

## GO-LIVE CHECKLIST (22 June 2026)

Run in order, shortly before the trading window opens:

1. [ ] **Confirm registration:** `register_agent --check` → `isRegistered: True`
2. [ ] **Fund wallet:** enough USDT (BEP-20) capital + BNB for gas, on BSC.
       Verify with `python -m src.agent status`.
3. [ ] **Reset runtime state:** `python -m src.agent reset`
       (critical — a stale drawdown peak would falsely trip the breaker)
4. [ ] **Sanity dry-run:** `python -m src.agent tick` → confirm it produces a sane
       deploy plan with no errors.
5. [ ] **Flip to live:** set `DRY_RUN=false` in `.env`.
6. [ ] **Start the agent:** `python -m src.agent run --live`
       (leave running for the whole window; restart-safe via persisted state).
7. [ ] **Monitor:** watch logs; spot-check `status` and BscScan periodically.

### During the window
- The agent ticks every `EVENT_TICK_SECONDS` (default 60s) in event mode.
- It auto-derisks to stablecoin if drawdown hits −20% (and stays in cash — by
  design, capital preservation for the rest of the week).
- It places a small compliance trade if no trade has occurred within
  `MIN_TRADE_INTERVAL_H` (meets the minimum-activity rule).

### Troubleshooting
- **Public RPC flaky:** the RPC client fails over across endpoints; if persistent,
  set a paid `BSC_RPC_URL` (Ankr/QuickNode) in `.env` and restart.
- **Breaker tripped early:** intentional capital preservation; it will not
  re-enter for the session. To resume risk (only if you intend to), `reset` and
  restart — but understand you are re-enabling exposure.
- **Wallet shows 0 orders:** equity may be below the min-order/halt threshold,
  or already at target exposure — both are expected, safe states.

---

## Testing

```bash
python -m pytest tests/        # 403 unit/integration tests
```

Every module was built test-first (TDD) with a security threat model and a
code-review pass. The signal layer's prompt-injection firewall and the risk
layer's drawdown/sizing invariants have dedicated abuse-case tests.

## Security

- Private key in env only, never logged (`repr=False`); all signing is local.
- Signal layer is isolated from execution; external text becomes a bounded
  number, never an instruction (verified live against an injection payload).
- `.env`, `env.txt`, keys, and runtime data are gitignored.
