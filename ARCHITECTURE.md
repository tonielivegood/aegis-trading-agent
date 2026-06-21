# Architecture

Autonomous trading agent for BNB Hack 2026 — Track 1. This document explains how
the system is structured and *why* the key decisions were made.

## 1. Design thesis

Track 1 ranks on **raw total wallet return** over the contest week with a **hard 30%
max-drawdown disqualification gate**, a minimum-trade requirement, and simulated fees.
Raw return rewards upside, but the DQ gate punishes blowups — so every architectural
choice flows from one priority order:

1. Never get disqualified (drawdown).
2. Keep capital deployed (wallet value > $1 each hour).
3. Meet the minimum trade count.
4. Be profitable after fees.
5. Only then, maximize return.

This was validated empirically: across 111 weekly walk-forward windows (~125 days
of real Binance data), aggressive long strategies lost 15–20% and risked the DQ
cap, while the chosen conservative strategy capped drawdown near 13% and stayed
profitable in up-weeks. See `memory`/`PLAN.md` for the evidence trail.

## 2. Data flow (one event tick, every 60 s)

```
                         ┌─────────────────────────────────────────┐
                         │            agent_loop.tick()             │
                         └─────────────────────────────────────────┘
                                            │
   ┌────────────────────┬───────────────────┼───────────────────┬─────────────────┐
   ▼                    ▼                    ▼                   ▼                 ▼
 balances            CMC pricing         drawdown            regime           sniper.run()
 (Multicall3,        (by-id; BNB         tracker             valve            volume breakout
  1 RPC call)         on-chain)          (debounced)        (hourly:          + two-tier exits
   │                    │                    │               CMC BTC +         + cooldown
   │                    │                    │               Agent Hub F&G)        │
   └──────► Portfolio valuation ◄────────────┘  + last-known-good price            ▼
            equity / risk / stable          (CMC trending re-ranks)──► TradeOrder[]
                                                                                   │
                                                                                   ▼
                                                              execution: 1inch aggregator
                                                              (calldata → LOCAL signing ·
                                                               DRY_RUN gate · min_out · approval)
                                                                                   │
                                                                                   ▼
                                                          persist state + Telegram alerts
```

Sentiment + trending come from the **CMC AI Agent Hub**, fetched hourly by the regime
updater and cached — they never sit in the 60 s hot path.

## 3. Modules

| Package | Responsibility |
|---|---|
| `config.py` | Typed settings from `.env`; secrets marked `repr=False` (never logged). |
| `data/rpc.py` | Multi-endpoint BSC RPC with failover; cached singleton. |
| `data/token_list.py` | Verified curated token universe; liquidity-ranked basket selection. |
| `data/cmc_client.py` | CoinMarketCap quotes + **prices by id** (the universe is priced by CMC id), cached. |
| `data/cmc_agent_hub.py` | **CMC AI Agent Hub** skills: Fear & Greed + community trending (fail-safe, cached). |
| `data/price_feed.py` | USD prices — CMC for the aggregator universe; on-chain PancakeSwap for BNB/WBNB. |
| `risk/drawdown.py` | **Debounced** latching breaker (alert −20% after N consecutive breach ticks, cap −30% instant). |
| `risk/position_sizer.py` | Per-token cap + stablecoin floor; fails safe on bad input. |
| `risk/trade_counter.py` | Min-trade-count tracking, persisted. |
| `risk/portfolio.py` | Full-wallet valuation (core ∪ alpha), cost-basis PnL, Multicall3 balance reads. |
| `aegis/` | **Live strategy**: volume-breakout sniper — regime valve, two-tier params, market feed, positions, cooldown. |
| `execution/oneinch.py` | **Live execution**: 1inch v6 aggregator; returns calldata, signed LOCALLY (self-custody). |
| `execution/openocean.py` · `pancakeswap.py` | Keyless aggregator backup · PancakeSwap V2 fallback + BNB pricing. |
| `execution/twak_executor.py` | **Trust Wallet Agent Kit** backend (`EXECUTION_BACKEND=twak`) on a dedicated wallet — the Trust Wallet leg of the stack. |
| `signal/` *(research, not in live path)* | Momentum + Claude sentiment behind a prompt-injection firewall. |
| `strategy/adaptive_hold_strategy.py` | Deep fallback: fractional diversified hold + breaker (walk-forward-validated). |
| `monitor/safeguard.py` | Per-tick derisk / halt / compliance-trade decision. |
| `monitor/pnl.py` | Contest-aware PnL (hour ≤ $1 → 0%). |
| `monitor/notifier.py` | Telegram alerts (send-only, best-effort). |
| `backtest/` | Engine, metrics, Binance loader, walk-forward — the evidence base. |
| `agent_loop.py` / `scheduler.py` / `__main__.py` | Orchestrator, scheduler, CLI. |

## 4. Key decisions & rationale

- **Strategy = cash-default confirmed-momentum sniper, then ride.** Sits in cash; enters
  only on a CONFIRMED move (sustained **5-minute** volume + price already up **≥3%**, not a
  one-minute blip), then RIDES — majors to a +30% cap on a 7% trail, memes to +200% on a
  wide 25% trail — exiting only on take-profit, a trailing stop, or a hard stop (−7% / −12%).
  There is **no time-based exit** (an earlier no-progress timer was removed after a live soak
  proved it churned). An hourly regime valve throttles **exposure** (size/slots). The
  fractional-hold strategy remains as the walk-forward-validated deep fallback. (Honest: the
  momentum edge is unproven; the engineering minimizes operational + DQ risk, not market risk.)
- **Execution via the 1inch DEX aggregator.** Single-DEX (PancakeSwap V2) slippage
  capped the tradable set at ~18 tokens; the aggregator routes across all BSC DEXs and
  unlocks ~91 routable tokens (incl. the meme tail). 1inch returns calldata that the agent
  **signs locally** — self-custody is preserved. Proven live on-chain.
- **Universe priced by CMC id, not on-chain.** Thin V2 pools give garbage on-chain prices
  for the aggregator tokens (e.g. AAVE read $0.81 vs ~$76), which would crater valuation and
  trip the breaker. CMC-by-id is accurate; only BNB/WBNB stay on-chain.
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

- **403 unit/integration tests** (`pytest tests/`), every module built test-first.
- **Backtest + walk-forward** over real Binance history selected the fallback strategy.
- **Live-verified** self-custody execution: a real on-chain swap routed through the 1inch
  AggregationRouterV6 (USDT→ETH, status `0x1`), calldata signed locally — tx
  `0x2727f6d5337a60c1ec2991258fa36c8deaf2652c908743dd29cf3186b11e7d6c`.
- **Operational soak** (continuous dry-run on the VPS) runs with zero crashes; the
  breaker debounce, last-known-good pricing, and kill-switch paths were exercised end-to-end.

## 7. Operations

See `README.md` (commands, Telegram setup, GO-LIVE CHECKLIST) and `DEPLOY.md`
(VPS deployment with systemd). Live trading needs BSC RPC + CoinMarketCap (pricing +
AI Agent Hub) + 1inch (execution) + Binance public klines (live volume confirmation).
