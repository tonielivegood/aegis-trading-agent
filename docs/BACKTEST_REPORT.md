# Aegis — Track 1 Validation & Backtest Report

**Scope:** validate whether the new **Event-Driven Alpha Momentum** strategy is
better than the old baseline and safe enough for Track 1. Generated read-only,
DRY_RUN, no trades.

Commands used:
```bash
python scripts/run_walkforward.py            # hourly, ~125d (Binance majors)
python scripts/run_walkforward.py --cmc      # daily, 5.7yr (CMC Pro majors)
python scripts/run_track1_backtest.py        # coverage probe + catalyst replay + safety
python -m src.agent compliance               # min-trade tracker report
python -m src.agent tick                      # live-forward dry-run readiness
python -m pytest -q ; ruff check src tests scripts
```

## 1. Data sources & coverage

| Source | Resolution | Coverage | Use |
|---|---|---|---|
| CoinMarketCap Pro | **daily**, ~5.7 yr | **Majors** (BTC/ETH/BNB/…); **eligible Alpha tokens: ~none** | risk-engine + baseline walk-forward |
| Binance public klines | **hourly**, ~125 d | Majors | hourly walk-forward cross-check |
| Binance Alpha klines | **5-minute** | eligible subset, **shallow** (token lifetime, days–weeks) | **live** volume confirmation (not historical depth) |
| Historical catalyst / news | — | **unavailable** | replaced by documented scenario replay |

**Eligible-universe coverage probe** (`run_track1_backtest.py`, real CMC by id):
the sampled liquid Alpha tokens returned **no usable CMC daily history** — they
are 2025–26 listings. **Consequence:** a multi-year walk-forward of the event
strategy is **impossible**, and since the strategy is **intraday** (5 h max hold,
5 m volume), daily bars cannot represent it even where they exist. We do **not**
fake history.

## 2. Strategy comparison (real walk-forward, 681×7-day windows, CMC daily 5.7 yr)

> Universe = **majors** (the only assets with multi-year history). This validates
> the **risk engine and the baseline/fallback**, *not* the eligible Alpha universe.
> Fees 25 bps + slippage 50 bps. Daily resolution **understates** intraday drawdown.

| Strategy | AvgRet | Median | Worst | Best | Win% | WorstDD | DQ% |
|---|---|---|---|---|---|---|---|
| A. Preserve (all stable) | +0.0% | +0.0% | 0.0% | 0.0% | 0% | **0.0%** | 0% |
| B. Buy & Hold (market) | +1.1% | +0.8% | −33.7% | +61.2% | 55% | 43.5% | 0%* |
| Hold + breaker (−20%) | +0.8% | +0.0% | −33.8% | +61.2% | 47% | 33.9% | 0%* |
| **B′. Fractional-50 + breaker (baseline/fallback)** | **+0.6%** | +0.4% | −16.8% | +38.3% | 55% | **17.9%** | **0%** |
| Concentr top-2 (80%) | −0.8% | −0.7% | −20.9% | +71.0% | 23% | 21.5% | 0% |

\* daily WorstDD already near/over the 30% cap → intraday these **would risk DQ**;
the **Fractional-50 + breaker** baseline is the safe one (WorstDD 17.9%, 0% DQ).

**C. Event-Driven Alpha Momentum** — **not historically backtestable** (no historical
catalysts; eligible tokens too new; strategy is intraday). Validated by §3 + §4 +
the test suite, not by a historical-alpha number. **We do not claim a historical
return for it.**

**D. Compliance-only safe mode** — deterministic, not a return strategy: guarantees
≥1 valid eligible trade/day, ≥7/week, via a late-day safe trade that **never bypasses
risk gates** and **safe-skips** if no safe route exists. Verified by `test_compliance.py`.

## 3. Catalyst scenario replay (real scanner logic, documented manual events)

| Event (manual feed) | Tier | Score | Outcome |
|---|---|---|---|
| Binance authority mention (TWT) | 1 | **70** | → WATCHLIST (still needs vol+price+liquidity) |
| CMC authority mention (SFP) | 1 | 50 | below threshold → no candidate |
| Unverified rumor | 3 | (penalised) | cannot enter alone |
| Spam/giveaway (SCAMX) | 3 | **−55** | rejected; not eligible by contract |
| Stale (>5 h) | — | decays to 0 | no new entry |

Proven: **a catalyst alone never trades.** Entry additionally requires
eligible-by-contract + liquid subset + **real 5 m volume** + price breakout +
slippage/liquidity + risk gates. *Scenario validation, not historical alpha proof.*

## 4. Contest-safety checks (enforced by the test suite — 306 tests, ruff clean)

Trades only the official 149 allowlist (by contract) · Tier-3 can't enter alone ·
no-volume catalyst stays WATCHLIST · max 3 positions · $10 cap · stablecoin floor
never breached · 5 h max hold · 2× TP / stop / trailing · **5× volume = FOMO defense,
not blind sell** · drawdown breaker overrides · compliance never forces a bad trade ·
**DRY_RUN prevents broadcasting**. `DRY_RUN=true`, `broadcast=false` by default.

## 5. What is proven / not proven

**Proven**
- The **risk engine + baseline** survive 5.7 years of weekly windows at **0% DQ**,
  worst drawdown 17.9% (daily) — well under the 30% cap.
- Concentrated/aggressive variants are **worse** (lower win rate, deeper DD) — the
  conservative baseline is correctly chosen.
- All Track-1 **safety and compliance** properties hold (unit-tested).
- The catalyst → confirmation → gate **pipeline logic** behaves correctly.

**Not proven (honest)**
- The **event strategy's historical return/alpha** — impossible to backtest (no
  historical catalysts; tokens too new; intraday horizon vs daily data).
- Real-money live performance — only DRY_RUN so far.
- Exact organizer **scoring** (total NAV vs eligible holdings vs valid-trade PnL)
  is unconfirmed; we deliberately do not hard-code an assumption.

## 6. Does Event-Driven Alpha improve over baseline?

- **On risk/survival:** equal or better — same breaker + tiny $10 sizing + max 3
  positions + stablecoin floor; the eligible-only + compliance layer is strictly
  safer for Track-1 scoring than the old majors basket (which would score ~0).
- **On return:** **unproven historically** by design. Its edge is conditional and
  *live*: it only acts on a confirmed public catalyst + a real 5-minute volume/price
  move, sized small, exited fast. We claim a sound, risk-bounded *mechanism*, not a
  backtested return.

## 7. Why Aegis is still a strong Track 1 submission

- Trades the **right universe** (eligible-by-contract) with a proven anti-DQ guard.
- Uses **real** Binance Alpha 5-minute volume for live confirmation (no faked data).
- **Survival-first** risk engine validated over 5.7 years at 0% DQ.
- **Self-custody** + on-chain proof; **DRY_RUN-safe**; uses the new Binance Web3 stack.
- **Scientifically honest:** clearly separates what is backtested (risk/baseline)
  from what is live-conditional (catalyst alpha) — no overclaiming.

> **Event-Driven Alpha depends on live catalyst detection and live 5-minute market
> confirmation.** It is a real-time agent edge, validated here by mechanism +
> scenario + safety tests, and intended to be demonstrated live in DRY_RUN.
