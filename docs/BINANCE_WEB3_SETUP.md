# Binance Wallet Web3 API — Setup (local, safe)

Aegis can use the **Binance Wallet Web3 API** as a native on-chain agent layer for
**quote / route discovery and unsigned-transaction readiness**, with **MEV-aware
routing** where available. It is **non-custodial**: the API only returns market
data and *unsigned* transactions — **signing always stays local/self-custody**
(Trust Wallet Agent Kit / PancakeSwap). Aegis **never auto-signs and never
broadcasts** from this layer.

## 1. Create your local env file

```bash
cp .env.example .env
```

`.env` is gitignored and must never be committed. `.env.example` holds
placeholders only.

## 2. Paste your Binance Web3 API key — locally only

Open `.env` in your editor and fill in:

```
BINANCE_WEB3_ENABLED=true
BINANCE_WEB3_API_KEY=<paste-your-key-here>
BINANCE_WEB3_API_SECRET=<paste-if-required>
BINANCE_WEB3_BASE_URL=<leave blank for default>
BINANCE_WEB3_QUOTE_ENABLED=true        # quote/route discovery
BINANCE_WEB3_EXECUTION_ENABLED=false   # build unsigned tx only (still no broadcast)
BINANCE_WEB3_BROADCAST_ENABLED=false   # keep false — Aegis never broadcasts here
BINANCE_WEB3_MEV_PROTECTION_ENABLED=true
```

> **Never paste your API key into a chat, an issue, a screenshot, or a log.**
> Aegis reads the key from your local environment only and masks it everywhere
> (e.g. `abc123...xyz789`). It is never printed in full.

## 3. Verify safely (read-only)

```bash
python scripts/check_binance_web3_env.py
```

This prints the masked key, shows which capabilities are enabled, and — only if
enabled and a key is present — runs a harmless connectivity probe. It never
signs, never broadcasts, never sends a transaction, and fails safe with a clear
message if configuration is missing.

## 4. Safety guarantees

- `DRY_RUN=true` by default — no real trades.
- Signing is **local/self-custody**; the Web3 API layer returns **unsigned**
  transactions only.
- `BINANCE_WEB3_BROADCAST_ENABLED` stays `false`; the agent never broadcasts
  from this layer.
- **Binance Alpha market data** (live 5-minute volume) is a **separate** path
  (`BINANCE_ALPHA_MARKET_DATA_ENABLED=true`) and does not depend on the Web3
  execution flags — market data stays available even with Web3 execution off.
