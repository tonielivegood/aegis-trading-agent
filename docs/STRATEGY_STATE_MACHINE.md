# Aegis — Event-Driven Alpha Momentum: state machine, entry & exit policy

> ⚠️ **Conceptual / partly historical.** The live entry/exit numbers are the
> **two-tier (MAJOR/MEME) params** in `aegis/token_class.py` + the regime valve in
> `aegis/regime.py` (see the strategy table in [`README.md`](../README.md)). The
> catalyst-score model below is the original framing; the live trigger is a real
> volume breakout.
>
> Implementation note: Aegis does not run a literal finite-state-machine object.
> The states below are a conceptual model of the **layered gates** in
> `aegis/orchestrator.py` + `strategy/event_driven_alpha_momentum.py`. Each tick
> re-evaluates every open position and every catalyst candidate.

## State model

```
SCANNING ─► WATCHLIST ─► ARMED ─► ENTERED ─► PROTECT_PROFIT ─► EXITED ─► COOLDOWN
```

| State | Meaning | What advances it |
|---|---|---|
| **SCANNING** | Catalyst scanner aggregates all sources each tick | a catalyst event appears |
| **WATCHLIST** | A catalyst signal exists for a token | maps to an eligible contract + score ≥ threshold |
| **ARMED** | Eligible + liquid + score ≥ 70 | price breakout + **real 5m volume** (or Tier-1 fast path) |
| **ENTERED** | All gates passed → $10 position opened (DRY_RUN: simulated) | position recorded in the PositionBook |
| **PROTECT_PROFIT** | Position open, managed each tick | trailing / take-profit / FOMO-defense logic active |
| **EXITED** | An exit rule fired → sell to stablecoin | — |
| **COOLDOWN** | Position closed | no pyramiding; a fresh catalyst is required to re-enter |

## Entry policy (ALL must hold)

1. Token is in the **official 149 allowlist by BSC contract address**.
2. Token is in the **liquid tradable subset** (`tradable_alpha.json`).
3. **Catalyst score ≥ `EVENT_SIGNAL_THRESHOLD`** (default 70).
4. **Price confirmation**: 5m breakout ≥ `AEGIS_BREAKOUT_PCT`.
5. **Volume confirmation**: real Binance Alpha 5m volume spike — *or* a Tier-1
   authority catalyst on the faster price+liquidity path.
6. Not already pumped beyond `AEGIS_OVERPUMP_PCT`.
7. Liquidity/slippage acceptable; free slot (`MAX_OPEN_POSITIONS` = 3);
   stablecoin floor preserved; not already holding the token.
8. `DRY_RUN` gate downstream — simulated unless explicitly run live.

A **Tier-3 / unverified** signal can **never** enter alone. A catalyst with **no
valid volume** stays in WATCHLIST/ARMED — Aegis never invents volume.

## Exit policy (priority order)

1. **Drawdown breaker** (global, latched) — flatten everything to stablecoin.
2. **Hard take-profit** — exit when value reaches `HARD_TAKE_PROFIT_MULTIPLE` (2×, $10→$20).
3. **Hard stop-loss** — `AEGIS_HARD_STOP_PCT` (default −8%).
4. **Max hold = 5h** — `MAX_HOLD_MINUTES` (300). **Hard exit**: meme positions are
   never held longer than 5 hours.
5. **5× volume FOMO defense** — when 5m volume ≥ `VOLUME_EXIT_MULTIPLE` × the
   entry baseline (after `MIN_HOLD_MINUTES_FOR_VOLUME_EXIT`): **not a blind sell.**
   The trailing stop tightens (`AEGIS_FOMO_TRAILING_PCT`), and the position exits
   only if price is **also** stalling/reversing (printing below the 5m-ago level).
   If price keeps rising on the volume, the position is held with the tighter trail.
6. **Trailing stop** — once profitable, exit if price falls `AEGIS_TRAILING_STOP_PCT`
   below the peak (tightened while FOMO defense is active).

## Objective

The objective is **final portfolio value in USDT** (or a configurable
scoring-safe stablecoin). Meme/Alpha tokens are temporary instruments to earn
stable value — Aegis enters small, protects profit, exits fast, and survives first.
