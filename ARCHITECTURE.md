# Architecture

Autonomous trading agent for BNB Hack 2026 — Track 1. This document explains how
the system is structured and *why* the key decisions were made.

## 1. Design thesis

Track 1 scores hourly PnL over one week with a **hard 30% max-drawdown
disqualification gate**, a minimum-trade requirement, and simulated fees. It is a
**risk-adjusted survival game, not a max-return race**. Every architectural choice
flows from one priority order:

1. Never get disqualified (drawdown).
2. Keep capital deployed (wallet value > $1 each hour).
3. Meet the minimum trade count.
4. Be profitable after fees.
5. Only then, maximize return.

This was validated empirically: across 111 weekly walk-forward windows (~125 days
of real Binance data), aggressive long strategies lost 15–20% and risked the DQ
cap, while the chosen conservative strategy capped drawdown near 13% and stayed
profitable in up-weeks. See `memory`/`PLAN.md` for the evidence trail.

## 2. Data flow (one tick, every 15 min)

```
                         ┌─────────────────────────────────────────┐
                         │            agent_loop.tick()             │
                         └─────────────────────────────────────────┘
                                            │
   ┌────────────────────┬───────────────────┼───────────────────┬─────────────────┐
   ▼                    ▼                    ▼                   ▼                 ▼
 balances            CMC quotes          drawdown           safeguard          strategy
 (Multicall3,        (% change,          tracker            evaluate()         decide()
  1 RPC call)         price)             update(equity)      derisk?            adaptive_hold
   │                    │                    │               halt?              or rebalance
   └──────► Portfolio valuation ◄────────────┘               min-trade?            │
            equity / risk / stable                              │                  ▼
                                                                └──────────► TradeOrder[]
                                                                                   │
                                                                                   ▼
                                                                  execution.PancakeSwap.swap()
                                                                  (DRY_RUN gate · min_out ·
                                                                   exact approval · receipts)
                                                                                   │
                                                                                   ▼
                                                          persist state + Telegram alerts
```

## 3. Modules

| Package | Responsibility |
|---|---|
| `config.py` | Typed settings from `.env`; secrets marked `repr=False` (never logged). |
| `data/rpc.py` | Multi-endpoint BSC RPC with failover; cached singleton. |
| `data/token_list.py` | Verified curated token universe; liquidity-ranked basket selection. |
| `data/cmc_client.py` | CoinMarketCap quotes (price, % change), cached. |
| `data/price_feed.py` | USD prices — on-chain PancakeSwap first, CMC fallback. |
| `risk/drawdown.py` | Latching drawdown breaker (alert −20%, cap −30%). |
| `risk/position_sizer.py` | Per-token cap + stablecoin floor; fails safe on bad input. |
| `risk/trade_counter.py` | Min-trade-count tracking, persisted. |
| `risk/portfolio.py` | Valuation, cost-basis PnL, on-chain balance reads (Multicall3). |
| `signal/` *(optional, not in default path)* | Momentum + Claude sentiment behind a prompt-injection firewall. |
| `strategy/adaptive_hold_strategy.py` | **Production strategy**: fractional diversified hold + breaker. |
| `strategy/rebalance_strategy.py` | Defensive derisk-to-stablecoin. |
| `monitor/safeguard.py` | Per-tick derisk / halt / compliance-trade decision. |
| `monitor/pnl.py` | Contest-aware PnL (hour ≤ $1 → 0%). |
| `monitor/notifier.py` | Telegram alerts (send-only, best-effort). |
| `backtest/` | Engine, metrics, Binance loader, walk-forward — the evidence base. |
| `agent_loop.py` / `scheduler.py` / `__main__.py` | Orchestrator, scheduler, CLI. |

## 4. Key decisions & rationale

- **Strategy = fractional hold + breaker** (deploy `DEPLOY_FRAC` of equity into the
  top-`BASKET_SIZE` liquid majors, rest in stablecoin, exit on −20%). Chosen by
  walk-forward validation, not intuition. Hand-crafted momentum/regime strategies
  were tested and *rejected* because they lost to cash in the sample.
- **Latching breaker.** Once −20% drawdown hits, exit to stablecoin and stay there
  for the session. A one-week contest rewards preserving a bad start over trying
  to trade back (which historically deepens drawdown → DQ).
- **Trade only a curated, on-chain-verified token set.** CMC symbol lookups gave
  wrong/scam contracts (WBNB, BTCB, BUSD); every tradable address is verified via
  on-chain `symbol()`. Deep-liquidity majors only → low slippage, safe exits.
- **Multicall3 for balances.** 21 sequential RPC calls → 1 (6× faster, far fewer
  failure points over a 7-day unattended run), with a sequential fallback.
- **PnL baseline = actual starting equity** (persisted), not a static budget, so
  reported return is meaningful regardless of how much is funded.
- **Native BNB excluded from deployable risk** — it is the gas reserve, counted in
  equity (for PnL) but never treated as tradable capital.

## 5. Trust boundaries & security

| Boundary | Threat | Mitigation |
|---|---|---|
| Private key | Theft / leakage | `.env` only, `repr=False`, local signing, gitignored. |
| Execution | MEV / over-spend | `min_out` from live quote, exact approval, short deadline, balance clamp, `DRY_RUN` gate. |
| Token universe | Scam contracts | On-chain-verified whitelist; swaps refuse non-curated tokens. |
| Signal (news/social) | Prompt injection | Model output is a clamped number, never an instruction; signal pkg cannot import execution (enforced by test). |
| RPC / CMC data | Bad/garbage values | Numeric guards (NaN/negative rejected); per-token failures skipped. |
| Telegram | Inbound command abuse | Send-only; never reads/acts on messages; best-effort (failure can't break trading). |

## 6. Verification

- **131 unit/integration tests** (`pytest tests/`), every module built test-first.
- **Backtest + walk-forward** over real Binance history selected the strategy.
- **Live-verified** with real funds: single swap, sequential multi-swap, full
  sell-back, and Multicall3 reads matched the per-token method exactly.
- **Operational soak** (continuous dry-run) ran with zero crashes; the
  breaker → derisk path was exercised end-to-end.

## 7. Operations

See `README.md` (commands, Telegram setup, GO-LIVE CHECKLIST) and `DEPLOY.md`
(VPS deployment with systemd). Live trading needs only BSC RPC + CoinMarketCap;
Binance is used for backtesting only.
