# Aegis — Judge Demo Runbook (DRY_RUN, ~5 minutes)

Everything below is **read-only / DRY_RUN**: no transaction is signed or broadcast,
and no secret is printed. One-time setup:

```bash
pip install -r requirements.txt
cp .env.example .env     # fill CMC_API_KEY, ONEINCH_API_KEY etc. locally; never paste keys into chat
# DRY_RUN=true, STRATEGY_MODE=event_alpha, EXECUTION_BACKEND=1inch are the live config
```

## 1. Agent running in DRY_RUN
```bash
python -m src.agent status     # wallet, equity, mode = DRY-RUN
python -m src.agent tick       # one full sniper tick; prices via CMC, regime, would-route 1inch
```
Point out: `dry_run=True`; the universe is the **aggregator-routable subset of the
official 149 allowlist** (~91 tokens = 56 majors + 35 Binance-Alpha memes); pricing
is **CMC-by-id** (accurate for thin-pool aggregator tokens); execution `backend=1inch`.

## 1b. CMC AI Agent Hub signals (#CMCAgentHub)
```bash
python -m src.agent signals     # live Fear & Greed + community-trending → regime
```
Point out: Aegis consumes two **CMC AI Agent Hub** REST skills with our Pro key —
`/v3/fear-and-greed/latest` (market sentiment) and `/v1/community/trending/token`
(community trending). They feed the agent **out of the 60s hot path** and **fail safe**:
- **Sentiment** refines the hourly regime, **tightening-only** — extreme fear (F&G ≤ 20)
  forces `RISK_ON → CAUTIOUS`; it can never make the agent more aggressive.
- **Trending** re-ranks already-qualified volume breakouts (a 1.5× boost), steering the
  scarce position slots toward tokens with real community attention — it never opens a
  position on its own. Code: `src/agent/data/cmc_agent_hub.py`.

## 2. Real Binance volume confirmation (no faked volume)
```bash
python scripts/build_alpha_symbol_map.py        # maps eligible meme contracts -> Binance Alpha symbols
python scripts/check_binance_alpha_volume.py     # live 5m/1m quote volume, trade count, freshness
```
Point out: real quote volume + trade count from Binance Alpha (memes) and Binance spot
(majors); stale candles fail safe; an entry needs a genuine volume spike — **no faked volume**.

## 3. Two-tier sniper logic + risk gates (proven by tests)
```bash
python -m pytest tests/test_volume_breakout.py tests/test_regime.py tests/test_aegis_loop.py -q
```
Point out specific guarantees proven by tests: cash by default, MAJOR vs MEME tiers,
regime valve (RISK_OFF = no entries), cooldown (no re-entry), stablecoin floor blocks
entry, no pyramiding, drawdown breaker priority, and the Agent-Hub overlays
(sentiment tightens-only, trending re-ranks).

## 4. Track 1 compliance (eligible-only + minimum trades)
```bash
python -m src.agent compliance        # daily/weekly valid-trade report
```
Point out:
- Track 1 uses a **fixed 149-token BEP-20 allowlist**; **trades outside the list do not
  count** (matched by **contract address**, not symbol).
- Requirement: **≥1 valid trade/day, ≥7 over the week** — Aegis tracks this and, as a
  late-day safety net, makes ONE minimum-size, fully risk-gated eligible trade
  (`MIN_TRADE_COMPLIANCE`) if a day would otherwise pass with no valid trade — or
  safe-skips (`COMPLIANCE_UNMET_SAFE_SKIP`) rather than force a bad trade.
- **Honest caveat:** exact scoring (total wallet NAV vs only eligible-token holdings vs
  PnL-from-valid-trades) is **not fully organizer-confirmed**; Aegis does **not**
  hard-code a stablecoin-NAV assumption — it only guarantees activity stays inside the
  allowlist and treats stablecoin as configurable settlement.

## 5. Self-custody execution — on-chain proof (1inch aggregator)
Real on-chain proof transaction routed through the **1inch AggregationRouterV6** on BSC
(USDT → ETH, status `0x1` success). 1inch returned the calldata; **Aegis signed it
locally** — the key never left the machine:

```
0x2727f6d5337a60c1ec2991258fa36c8deaf2652c908743dd29cf3186b11e7d6c
```
BscScan: https://bscscan.com/tx/0x2727f6d5337a60c1ec2991258fa36c8deaf2652c908743dd29cf3186b11e7d6c

Point out: self-custody (the agent signs, never hands out a key); execution is real and
verifiable; the contest wallet is registered on the hackathon contract. OpenOcean is a
keyless backup; PancakeSwap V2 is the emergency fallback and the BNB/WBNB price source.

## 6. Kill-switch (emergency flatten)
```bash
python -m src.agent panic        # DRY preview: sell all non-stable -> USDT (add --live to execute)
```
Point out: one command flattens the wallet to the settlement asset; DRY by default.

## 7. Closing story
> **Survival first. Asymmetric upside second.**
> Aegis sits in cash, confirms real Binance volume, prices the universe via CoinMarketCap,
> reads the CMC AI Agent Hub for market regime + community attention, routes execution
> through the 1inch aggregator with **local self-custody signing**, takes modest profit on
> cheap majors while letting a rare meme ride to +100%, and caps drawdown well under the
> 30% DQ gate — DRY_RUN-safe by default.

## Full verification (optional)
```bash
python -m pytest -q          # full suite
ruff check src tests scripts # lint
```
