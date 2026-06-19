# Aegis — Judge Demo Runbook (DRY_RUN, ~5 minutes)

Everything below is **read-only / DRY_RUN**: no transaction is signed or
broadcast, and no secret is printed. One-time setup:

```bash
pip install -r requirements.txt
cp .env.example .env     # fill CMC_API_KEY etc. locally; never paste keys into chat
# DRY_RUN=true and STRATEGY_MODE=event_alpha are already the defaults
```

## 1. Agent running in DRY_RUN
```bash
python -m src.agent status     # wallet, equity, mode = DRY-RUN
python -m src.agent tick       # one full event-radar tick; prints strategy + n_orders, no money moved
```
Point out: `dry_run=True`, and Track-1 mode trades only the eligible allowlist.

## 2. Real Binance Alpha 5-minute volume provider
```bash
python scripts/build_alpha_symbol_map.py        # maps eligible contracts -> Alpha symbols (83/149)
python scripts/check_binance_alpha_volume.py     # live 5m quote volume, trade count, freshness
```
Point out: real quote volume + trade count; stale candles fail safe; **no faked volume**.

## 3. Catalyst / manual event → WATCHLIST / candidate
```bash
python scripts/run_catalyst_scanner.py           # scores manual_events.json by source tier
```
Point out: Binance/CMC authority = Tier 1 (high score); unverified rumor penalised;
spam goes negative; everything starts as **WATCHLIST** — a catalyst alone never trades.
(Edit `src/agent/data/manual_events.json` to simulate a fresh Binance/CMC/project/rumor/spam event.)

## 4. Risk gate blocking an unsafe trade
```bash
python -m pytest tests/test_aegis_loop.py tests/test_event_strategy.py -q
```
Point out specific guarantees proven by tests: Tier-3-only can't enter, no-volume
catalyst stays WATCHLIST, max 3 positions, $10 cap, stablecoin floor blocks entry,
5h max hold, 5× FOMO-defense (not a blind sell), drawdown breaker priority.

## 5. Binance Web3 env check with masked key
```bash
python scripts/check_binance_web3_env.py
```
Point out: API key shown **masked** (`abc123...xyz789`); quote/execution/broadcast
flags default **false**; never signs/broadcasts; fails safe if unset.

## 6. TWAK / BscScan on-chain proof (self-custody execution)
On-chain proof transaction (Trust Wallet Agent Kit, BSC):

```
0x02e71c3b54b08560324e9371e5c8b2fab9cd07c6c17426daa13a98af318df10f
```
BscScan: https://bscscan.com/tx/0x02e71c3b54b08560324e9371e5c8b2fab9cd07c6c17426daa13a98af318df10f
Point out: keys stay with the user (self-custody); execution is real and verifiable;
the contest wallet is registered on the hackathon contract.

## 6b. Track 1 compliance (eligible-only + minimum trades)
```bash
python -m src.agent compliance        # daily/weekly valid-trade report
```
Point out:
- Track 1 uses a **fixed 149-token BEP-20 allowlist**; **trades outside the list do not count** (matched by **contract address**, not symbol).
- Requirement: **≥1 valid trade/day, ≥7 over the week** — Aegis tracks this and, as a
  late-day safety net, will make ONE minimum-size, fully risk-gated eligible trade
  (`MIN_TRADE_COMPLIANCE`) if a day would otherwise pass with no valid trade — or
  safe-skip (`COMPLIANCE_UNMET_SAFE_SKIP`) rather than force a bad trade.
- **Honest caveat:** exact scoring (total wallet NAV vs only eligible-token holdings
  vs PnL-from-valid-trades) is **not fully organizer-confirmed**; Aegis does **not**
  hard-code a stablecoin-NAV assumption — it only guarantees trade activity stays
  inside the official allowlist and treats stablecoin as configurable settlement.

## 7. Closing story
> **Survival first. Execution second. Return third.**
> Aegis detects catalysts (CMC + Binance/CZ/BNB/Trust Wallet tiers), confirms them
> with real Binance Alpha 5-minute volume, checks liquidity/slippage via Binance
> Web3 / PancakeSwap routing, sizes small ($10), protects profit, exits fast, and
> caps drawdown well under the 30% DQ gate — self-custody, DRY_RUN-safe by default.

## Full verification (optional)
```bash
python -m pytest -q          # full suite
ruff check src tests scripts # lint
```
