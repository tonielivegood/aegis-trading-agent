# BNB Hack 2026 — Track 1 Autonomous Trading Agent

A self-custody autonomous trading agent for BNB Hack 2026 Track 1. Trades live on
BSC via PancakeSwap during the contest window (22–28 June 2026), scored on
hourly PnL with a hard 30% max-drawdown disqualification gate.

**Design philosophy: survival first.** Track 1 is a risk-adjusted survival game,
not a max-return race. The agent is built to *never get disqualified* and to
*stay profitable after fees*, while capturing upside when the market trends up.

---

## Why this matters (Aegis differentiator)

Binance Wallet Web3 API was released during the hackathon window, and Aegis is
designed to use it as a **native on-chain agent layer**: Binance Alpha 5-minute
market data for volume confirmation, Binance Web3 quote/route readiness, unsigned
transaction construction, MEV-aware routing where available, and Trust Wallet
Agent Kit for self-custody signing. The result is **not a black-box trading bot**,
but a **risk-managed autonomous agent** that can detect catalysts, verify live
market confirmation, size small, and execute only when safety gates pass.

Aegis combines five layers:

1. **CoinMarketCap Pro** — signals and multi-year historical validation.
2. **Binance Alpha / Binance Web3 market data** — live 5-minute volume + market confirmation.
3. **Binance Wallet Web3 API** — quote / route / **unsigned**-transaction readiness (non-custodial).
4. **Trust Wallet Agent Kit** — self-custody signing / execution safety.
5. **Aegis risk engine** — position sizing, entry/exit, drawdown breaker, DRY_RUN safety.

Signing stays **local / self-custody**; the Web3 layer returns unsigned transactions
only, the agent never auto-broadcasts, and **`DRY_RUN=true` by default**. Setup:
[`docs/BINANCE_WEB3_SETUP.md`](docs/BINANCE_WEB3_SETUP.md).

---

## Track 1 strategy — Event-Driven Alpha Momentum (primary)

For Track 1, Aegis trades **only the official 149-token BEP-20 allowlist** (matched
by **contract address**), concentrating on the liquid, routable subset. It is a
catalyst-confirmed momentum strategy, not a buy-and-hold:

**State model** (implemented as layered gates, not a black box):

```
SCANNING ─► WATCHLIST ─► ARMED ─► ENTERED ─► PROTECT_PROFIT ─► EXITED ─► COOLDOWN
  catalyst    catalyst    +price   all gates   trailing / TP /   exit fired  no re-entry
  scan        mapped to   +liquid   pass →      FOMO-defense                 (no pyramiding)
  each tick   eligible    +real 5m  $10 entry   active
                          volume
```

- **Entry** requires *all*: eligible (by contract) + liquid subset + catalyst
  score ≥ 70 + price breakout + **real Binance Alpha 5m volume confirmation**
  (or Tier-1 authority fast path) + slippage/liquidity OK + risk gates + DRY_RUN.
  A Tier-3/unverified signal **can never enter alone**; a catalyst without volume
  stays WATCHLIST.
- **Exit** (priority): drawdown breaker → hard take-profit (**2×**) → stop-loss →
  **max hold 5h (hard exit)** → **5× volume FOMO defense** → trailing stop.
- **Meme/Alpha tokens are temporary instruments** to earn the stablecoin
  settlement asset — never long-term holdings.

> **Max hold = 5h is a hard exit.** **5× volume is NOT a blind sell** — it engages
> FOMO defense: the trailing stop tightens and the position exits only if price is
> *also* stalling/reversing; if price keeps rising, the position is held.

Full detail: [`docs/STRATEGY_STATE_MACHINE.md`](docs/STRATEGY_STATE_MACHINE.md).
The majors-hold strategy below is the **validated baseline/fallback**, used only
when no high-confidence catalyst exists.

## Why this can win Track 1

- **Right universe, by contract address.** Many entries fail by trading
  non-eligible majors; Aegis trades only the official allowlist and proves it
  with an anti-DQ test (`tradable ⊆ eligible`).
- **Real confirmation, not vibes.** Live Binance Alpha 5-minute volume (83/149
  eligible tokens mapped) gates every entry; no faked volume, ever.
- **Survival first.** A 30% drawdown disqualifies; Aegis caps risk with tiny $10
  positions, a stablecoin floor, a 5h max hold, and a latched drawdown breaker.
- **Self-custody + on-chain proof.** Keys stay local; execution via PancakeSwap /
  Trust Wallet Agent Kit, with an on-chain proof transaction already on BscScan.
- **Uses the newest BNB stack.** Built around the Binance Wallet Web3 API
  (released during the hackathon window) for quote/route/unsigned-tx readiness.

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
| Binance Alpha 5m volume (market data) | **LIVE** (read-only) — verified |
| CoinMarketCap Pro signals + walk-forward validation | **LIVE** (read-only) |
| Catalyst scanner (manual feed) | **LIVE**; Tier-1 network adapters off until creds/flags set |
| On-chain pricing / route / slippage (PancakeSwap) | **LIVE** (read-only quotes) |
| Trade execution | **DRY_RUN** — simulated, **no broadcast**, by default |
| Binance Web3 API quote/route | adapter ready; **off** until `BINANCE_WEB3_ENABLED=true` |
| Transaction signing / broadcast | **never automatic**; local/self-custody only |

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
data/      market data: RPC (multi-endpoint failover), CMC quotes, price feed, token universe
aegis/     catalyst scanner + scoring, Binance Alpha 5m volume, market feed, positions, orchestrator
risk/      drawdown breaker, position sizing, trade counter, portfolio valuation, input guards
execution/ PancakeSwap V2 swaps + local tx signing (safety rails: whitelist, min_out, exact approval, DRY_RUN gate)
signal/    momentum + Claude sentiment, behind a prompt-injection firewall (output = bounded number only)
strategy/  adaptive fractional-hold (production) + momentum/rebalance variants
monitor/   safeguard (derisk/halt/compliance), contest PnL accounting, structured logging
backtest/  engine, metrics, Binance data loader, walk-forward validation
agent_loop.py  orchestrator: data → risk → safeguard → strategy → execution
scheduler.py   APScheduler: tick every STRATEGY_TICK_MIN
__main__.py    CLI
```

## Baseline fallback strategy (validated)

> This majors-hold strategy is the **Layer-A fallback** (used when no
> high-confidence catalyst exists) and the original walk-forward-validated core.
> The **primary Track-1 strategy is the Event-Driven Alpha Momentum radar above.**

**Adaptive fractional hold + breaker** — chosen by walk-forward validation over
111 weekly windows (~125 days of real Binance data):

- Deploy `DEPLOY_FRAC` (default 50%) of equity into the top-`BASKET_SIZE`
  (default 6) most-liquid majors (BTCB, ETH, WBNB, CAKE, XRP, ADA), equal weight.
- Keep the rest in USDT (stablecoin reserve).
- Hard drawdown breaker: at −20% drawdown, exit all risk to stablecoin (latched).
- Per-token cap 10% of equity; never breaches the stablecoin floor.

Validated profile: avg −0.4%/week, worst −10.3%, best +4.7%, **worst drawdown
12.8%** (vs 30% DQ cap), **0% disqualification across all windows**.

> **Signal layer status:** the default live strategy (adaptive hold) is
> price-only and does NOT depend on the `signal/` layer. The CMC-sentiment +
> momentum signal engine (with its prompt-injection firewall) is built, tested,
> and available to re-enable, but is not wired into the default path — the
> walk-forward winner didn't need it. It remains usable for research and for
> sentiment-driven strategy variants.

> Honest note: no tested strategy showed large reliable alpha in this regime.
> The edge here is *survival* — while aggressive competitors blow up on the 30%
> cap, this agent caps drawdown near 13% and catches up-weeks. The backtest
> tooling is reusable to keep researching better strategies.

## Risk controls

| Control | Value | Purpose |
|---|---|---|
| Drawdown breaker (latched) | −20% | exit to cash well under the 30% DQ cap |
| Per-token cap | 10% of equity | no single-token blowup |
| Stablecoin floor | 20% | wallet never drains (hourly value stays > $1) |
| Slippage protection | min_out from live quote | anti-MEV/sandwich |
| Token approval | exact amount | no unlimited allowance |
| DRY_RUN gate | env flag | no transaction sent unless explicitly live |
| Signal firewall | JSON-only, clamped | external text can't inject trade instructions |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in secrets (see below)
```

Required in `.env` (never commit it — it is gitignored):

- `AGENT_PRIVATE_KEY`, `AGENT_WALLET_ADDRESS` — the agent wallet (self-custody)
- `BSCSCAN_API_KEY`, `CMC_API_KEY`, `ANTHROPIC_API_KEY`
- Risk dials: `DEPLOY_FRAC=0.50`, `BASKET_SIZE=6`, `MAX_DRAWDOWN_ALERT=0.20`, etc.
- `DRY_RUN=true` (keep true until go-live)

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
- The agent ticks every `STRATEGY_TICK_MIN` (default 15 min).
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
python -m pytest tests/        # 129 unit/integration tests
```

Every module was built test-first (TDD) with a security threat model and a
code-review pass. The signal layer's prompt-injection firewall and the risk
layer's drawdown/sizing invariants have dedicated abuse-case tests.

## Security

- Private key in env only, never logged (`repr=False`); all signing is local.
- Signal layer is isolated from execution; external text becomes a bounded
  number, never an instruction (verified live against an injection payload).
- `.env`, `env.txt`, keys, and runtime data are gitignored.
