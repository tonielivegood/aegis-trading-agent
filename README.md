# BNB Hack 2026 — Track 1 Autonomous Trading Agent

A self-custody autonomous trading agent for BNB Hack 2026 Track 1. Trades live on
BSC via PancakeSwap during the contest window (22–28 June 2026), scored on
hourly PnL with a hard 30% max-drawdown disqualification gate.

**Design philosophy: survival first.** Track 1 is a risk-adjusted survival game,
not a max-return race. The agent is built to *never get disqualified* and to
*stay profitable after fees*, while capturing upside when the market trends up.

---

## Architecture

```
data/      market data: RPC (multi-endpoint failover), CMC quotes, price feed, token universe
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

## The strategy (validated)

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
