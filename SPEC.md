# Spec: BNB Hack 2026 — Track 1: Autonomous Trading Agent

> ⚠️ **Historical design doc (original spec).** The live system evolved during the
> build: execution is now the **1inch DEX aggregator** (self-custody local signing),
> the universe is ~91 aggregator-routable tokens priced via **CoinMarketCap**, and the
> live strategy is a **cash-default two-tier volume-breakout sniper** with a CMC
> AI Agent Hub regime overlay. For the *current* architecture see
> [`README.md`](README.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Objective

Build an autonomous AI trading agent that:
- Registers on-chain on BSC **before 22/6/2026** via contract `0x212c61b9b72c95d95bf29cf032f5e5635629aed5`
- Trades LIVE on BSC during the 22–28/6/2026 window (7 days)
- Maximises total PnL while staying below the ~30% max drawdown cap
- Meets the minimum trade count requirement (non-zero trading activity every hour)
- Uses all three sponsor stacks: CMC (data), TWAK (execution), BNB Chain / PancakeSwap (venue)
- Remains profitable net of simulated transaction costs

**Success = ranked by total return (%) during trading window, with no disqualification triggers.**

---

## ASSUMPTIONS (confirm before proceeding)

1. Registration contract ABI is not publicly verified on BscScan — we will call `eth_getCode` then attempt known function selectors (`register()`, `registerAgent(address)`, `join()`), OR fetch the ABI via BscScan API with a valid key before 21/6.
2. The exact 149-token eligible list will be fetched from the CMC API / DoraHacks page; spec uses "CMC BEP-20 top 149" as placeholder until confirmed.
3. Starting capital: we assume the team funds the agent wallet before 22/6 (amount TBD — a few hundred USD in BNB + stablecoins covers fees + positions).
4. The ~30% drawdown cap is measured peak-to-trough on the portfolio at any point during the trading window; hard exit at -20% to stay safely under the -30% disqualification threshold.
5. "Minimum trade count" means at least one swap per ~4-hour window (assumption); we target ≥4 trades/day to be safe.
6. PnL is measured hourly — portfolio value must stay > $1 every clock hour to count.
7. Primary language: **Python** (bnbagent SDK is Python; web3.py for direct BSC access; pandas for analytics).
8. Execution route: **PancakeSwap V2 Router** (`0x10ED43C718714eb63d5aA57B78B54704E256024E`) via direct web3.py calls + TWAK CLI for fallback/automation.
9. Signal layer uses Claude API via Anthropic SDK — outputs structured JSON only, never passed to `exec()` or transaction builder directly.

---

## Tech Stack

| Layer | Tool | Version |
|---|---|---|
| Language | Python | 3.11+ |
| BSC RPC | web3.py | 7.x |
| DEX execution | PancakeSwap V2 Router (direct ABI) | - |
| Agent identity | bnbagent SDK (ERC-8004) | latest |
| Execution CLI | Trust Wallet Agent Kit (`twak`) | latest |
| Market data | CMC Data API + CMC MCP (12 tools) | v3 |
| AI signal | Anthropic SDK (Claude claude-sonnet-4-6) | latest |
| Task scheduling | APScheduler | 3.x |
| Config | python-dotenv | - |
| Logging | structlog | - |
| Testing | pytest + pytest-asyncio | - |

---

## Commands

```bash
# Setup
pip install -r requirements.txt
cp .env.example .env        # fill in secrets

# Register agent on-chain (run ONCE before 22/6)
python -m agent register

# Dry-run simulation (paper trading mode)
python -m agent run --dry-run

# Live trading (22-28/6 only)
python -m agent run --live

# Check current PnL/status
python -m agent status

# Tests
pytest tests/ -v --tb=short

# Lint
ruff check . && mypy src/
```

---

## Project Structure

```
E:\Track1-trade-onchain\
├── SPEC.md                          ← this file
├── .env.example                     ← template (no secrets)
├── .env                             ← gitignored real secrets
├── .gitignore
├── requirements.txt
├── pyproject.toml
│
├── src/
│   └── agent/
│       ├── __main__.py              ← CLI entrypoint (register / run / status)
│       │
│       ├── registration/
│       │   ├── __init__.py
│       │   ├── register_agent.py    ← calls hackathon contract to join participant list
│       │   └── contract_abi.json    ← ABI fetched from BscScan
│       │
│       ├── data/
│       │   ├── __init__.py
│       │   ├── cmc_client.py        ← CMC Data API + MCP wrapper
│       │   ├── token_list.py        ← loads & validates the 149-token eligible list
│       │   └── price_feed.py        ← real-time price polling (CMC + on-chain fallback)
│       │
│       ├── signal/
│       │   ├── __init__.py
│       │   ├── signal_engine.py     ← orchestrates all signal sources → SignalBundle
│       │   ├── momentum.py          ← price momentum / RSI calculations
│       │   ├── sentiment.py         ← news/social → Claude API → structured score JSON
│       │   └── signal_schema.py     ← Pydantic models for SignalBundle (isolation firewall)
│       │
│       ├── risk/
│       │   ├── __init__.py
│       │   ├── portfolio.py         ← track positions, cost basis, PnL per token
│       │   ├── drawdown.py          ← peak-to-trough drawdown monitor + circuit breaker
│       │   ├── position_sizer.py    ← Kelly fraction / fixed-fraction sizing
│       │   └── trade_counter.py     ← ensures min trade count compliance
│       │
│       ├── strategy/
│       │   ├── __init__.py
│       │   ├── base_strategy.py     ← abstract Strategy interface
│       │   ├── momentum_strategy.py ← trend-following on 149-token universe
│       │   └── rebalance_strategy.py← equal-weight rebalance to stablecoin on drawdown alert
│       │
│       ├── execution/
│       │   ├── __init__.py
│       │   ├── pancakeswap.py       ← PancakeSwap V2 Router direct calls via web3.py
│       │   ├── twak_executor.py     ← TWAK CLI wrapper (subprocess) for fallback swaps
│       │   └── tx_builder.py        ← builds, signs, submits BSC transactions locally
│       │
│       └── monitor/
│           ├── __init__.py
│           ├── scheduler.py         ← APScheduler jobs (hourly PnL check, strategy tick)
│           ├── safeguard.py         ← auto-derisking: if drawdown > -20% → stablecoin flight
│           └── logger.py            ← structlog structured JSON logs
│
└── tests/
    ├── test_registration.py
    ├── test_data.py
    ├── test_signal.py
    ├── test_risk.py
    ├── test_strategy.py
    └── test_execution.py
```

---

## Code Style

```python
# Good: typed, pydantic-validated, never eval() or exec() on external content
from pydantic import BaseModel
from decimal import Decimal

class SignalBundle(BaseModel):
    token_symbol: str
    direction: Literal["BUY", "SELL", "HOLD"]
    confidence: float          # 0.0–1.0
    momentum_score: float
    sentiment_score: float     # Claude output, parsed from JSON, NEVER executed

class TradeOrder(BaseModel):
    token_in: str
    token_out: str
    amount_in_wei: int
    min_amount_out_wei: int    # slippage-protected
    deadline: int              # unix timestamp
```

- Snake_case everywhere; `SCREAMING_SNAKE` for module-level constants
- All secrets via `os.getenv()` only; `dotenv.load_dotenv()` in entrypoint
- No `print()` statements — use `structlog.get_logger()`
- All external inputs (CMC API response, Claude output, news text) validated through Pydantic before use
- Transaction signing always happens locally via `web3.py` Account; private key never leaves process memory

---

## Testing Strategy

- **Framework:** pytest + pytest-asyncio
- **Unit tests:** pure logic (signal math, drawdown calc, position sizing) — fast, no network
- **Integration tests:** mocked BSC RPC + mocked CMC API (VCR cassettes)
- **Smoke test:** one end-to-end dry-run on BSC Testnet before going live
- **Coverage target:** ≥80% on `risk/` and `signal/` modules (these are the critical path)
- No tests mock the private key or bypass the signing path; use a throwaway testnet wallet instead

---

## Security & Hardening Boundaries

### Always do
- Load private key from `os.getenv("AGENT_PRIVATE_KEY")` only — never from a file or CLI arg
- Validate ALL CMC API responses and Claude outputs through Pydantic models before use
- Set hard stop-loss in code: if `drawdown > 0.20` (20%) → `safeguard.emergency_derisking()`
- Log every transaction attempt (even failed ones) with structlog JSON
- Keep signal layer **read-only**: `signal/` modules never import from `execution/`
- Use `min_amount_out` (slippage protection) on every swap — never `0`

### Ask first (human approval required)
- Increasing position size limits above 10% of portfolio per token
- Adding new tokens not in the verified 149-token list
- Changing the drawdown circuit-breaker threshold
- Modifying the registration transaction after it's submitted

### Never do
- Hardcode `AGENT_PRIVATE_KEY`, `CMC_API_KEY`, or `ANTHROPIC_API_KEY` in source files
- Pass raw news/tweet text as a prompt with trading instructions to the execution layer
- Execute any string from external content via `eval()`, `exec()`, or `subprocess`
- Trade tokens not in the verified 149-token eligible list
- Allow signal layer code to directly call `pancakeswap.swap()` — always goes through `risk/` gate first
- Push `.env` to git (enforced via `.gitignore` + pre-commit hook)

---

## Module Architecture (End-to-End Flow)

```
┌─────────────────────────────────────────────────────────────────┐
│                        AGENT MAIN LOOP                          │
│                      (scheduler.py, 15-min tick)                │
└────────────────────┬────────────────────────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │     DATA LAYER        │  CMC API → price, volume, market cap
         │    (cmc_client.py)    │  token_list.py → 149-token whitelist
         └───────────┬───────────┘
                     │ raw market data
         ┌───────────▼───────────┐
         │    SIGNAL LAYER       │  momentum.py → RSI/momentum scores
         │  (signal_engine.py)   │  sentiment.py → Claude API → JSON score
         │                       │  Output: SignalBundle[] (Pydantic validated)
         └───────────┬───────────┘
                     │ SignalBundle[] (structured, validated)
                     │ ← FIREWALL: signal layer cannot call execution
         ┌───────────▼───────────┐
         │   RISK MANAGEMENT     │  drawdown.py → check circuit breaker
         │   (risk gate)         │  position_sizer.py → size each trade
         │                       │  trade_counter.py → enforce min trades
         │                       │  Output: approved TradeOrder[] or HOLD
         └───────────┬───────────┘
                     │ approved TradeOrder[]
         ┌───────────▼───────────┐
         │  EXECUTION LAYER      │  pancakeswap.py → build swap txn
         │  (tx_builder.py)      │  tx_builder.py → sign locally (web3.py)
         │                       │  TWAK fallback if direct call fails
         └───────────┬───────────┘
                     │ signed transaction
         ┌───────────▼───────────┐
         │     BSC MAINNET       │  PancakeSwap V2 Router
         │   (on-chain)          │  BNB/BEP-20 settlement
         └───────────┬───────────┘
                     │ tx receipt
         ┌───────────▼───────────┐
         │     MONITOR           │  portfolio.py → update positions
         │  (safeguard.py)       │  drawdown.py → hourly check
         │                       │  auto-derisking if >-20% drawdown
         └───────────────────────┘
```

**Prompt Injection Firewall (critical):**
```
┌─────────────────────────────────┐
│   EXTERNAL CONTENT (news/tweets)│
│   → sentiment.py reads it       │
│   → sends to Claude API as      │
│     "Analyse sentiment only,    │
│      output JSON score 0-1"     │
│   → Claude returns JSON         │
│   → Pydantic validates JSON     │
│   → float score passed forward  │
│   NEVER: raw text → execution   │
└─────────────────────────────────┘
```

---

## Risk Parameters (Hard-Coded Constants)

```python
MAX_DRAWDOWN_ALERT    = 0.20   # 20% → trigger derisking
MAX_DRAWDOWN_CAP      = 0.30   # 30% → disqualification (never reach)
MAX_POSITION_PCT      = 0.10   # 10% of portfolio per token
STABLECOIN_FLOOR_PCT  = 0.20   # keep ≥20% in USDT/BUSD at all times
MIN_TRADE_INTERVAL_H  = 4      # at least 1 trade per 4-hour window
TARGET_DAILY_TRADES   = 4      # safety buffer above minimum
SLIPPAGE_BPS          = 50     # 0.5% slippage tolerance
MIN_PORTFOLIO_VALUE   = 1.50   # USD — never let hourly snapshot go below $1
STRATEGY_TICK_MIN     = 15     # run strategy every 15 minutes
HOURLY_CHECK_MIN      = 60     # full PnL + drawdown check every hour
```

---

## Registration Module — Priority 1 (deadline: 21/6/2026)

### What we know
- Contract: `0x212c61b9b72c95d95bf29cf032f5e5635629aed5` on BSC mainnet (chain ID 56)
- Registers agent wallet address as an immutable participant
- Must be called BEFORE 22/6 trading window opens

### Step 1 — Fetch ABI (do immediately)
```bash
# Option A: BscScan API (requires free API key)
curl "https://api.bscscan.com/api?module=contract&action=getabi&address=0x212c61b9b72c95d95bf29cf032f5e5635629aed5&apikey=YOUR_KEY"

# Option B: read contract bytecode + decode via 4byte.directory
# Look for function selectors in the bytecode
```

### Step 2 — Call registration function
```python
# Pseudocode until ABI is confirmed:
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(os.getenv("BSC_RPC_URL")))
contract = w3.eth.contract(
    address="0x212c61b9b72c95d95bf29cf032f5e5635629aed5",
    abi=load_abi("contract_abi.json")   # fill after Step 1
)

# Call register/join/registerAgent — confirm function name from ABI
txn = contract.functions.register().build_transaction({
    "from": agent_wallet_address,
    "nonce": w3.eth.get_transaction_count(agent_wallet_address),
    "gas": 200_000,
    "gasPrice": w3.eth.gas_price,
})
signed = w3.eth.account.sign_transaction(txn, private_key=os.getenv("AGENT_PRIVATE_KEY"))
tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
print(f"Registered! TX: {tx_hash.hex()}")
```

---

## Success Criteria

1. **Registration:** Agent wallet address appears in hackathon contract participant list on BSC before 22/6 — verifiable on BscScan.
2. **Capital deployed:** Wallet holds ≥1 eligible token from the 149-token list at contest start (22/6 00:00 UTC).
3. **Trade activity:** ≥4 completed swaps per day throughout 22–28/6 window.
4. **Drawdown discipline:** Peak-to-trough portfolio drawdown never exceeds -20% during trading window (hard circuit-breaker fires at -20%, competition disqualifies at -30%).
5. **Positive PnL:** Total return > 0% after simulated transaction costs at end of window.
6. **Hourly value:** Every hourly PnL snapshot shows portfolio value > $1.
7. **Prompt injection safe:** No external content (news, tweets, CMC descriptions) is ever passed with execution intent to any function that signs transactions.
8. **All tests pass:** `pytest tests/ -v` exits 0 before going live.

---

## RESOLVED (research complete 14/6/2026)

1. **Contract ABI:** ✅ FETCHED. Registration function is **`register()`** (no args — contract records `msg.sender`). Saved to `src/agent/registration/contract_abi.json`. Other functions: `isRegistered(address)→bool`, `registrationStart()→uint`, `registrationDeadline()→uint`, owner-admin funcs.
2. **Registration window (on-chain truth):** start = 2026-06-02 21:15 UTC, **deadline = 2026-06-25 00:00 UTC**. Window is OPEN now. NOTE: on-chain deadline (25/6) is LATER than the "before 22/6" rule text — register EARLY to be safe.
3. **Starting capital:** ✅ $100 budget (confirmed by user).
4. **CMC API key:** ✅ Provided, in `.env`.
5. **Anthropic API key:** ✅ Provided, in `.env`. Model: `claude-haiku-4-5-20251001`.
6. **BscScan API key:** ✅ Provided, in `.env`.
7. **Agent wallet:** ✅ `0xA5200DC306d8273f9Ccdbf5221a6cC3916aC2Ffa` — private key verified to match.

## Open Questions (still need answers)

1. **Token list:** Exact list of 149 eligible BEP-20 tokens (symbols + contract addresses)? → Fetch from CMC hackathon page / DoraHacks.
2. **Minimum trade count:** Exact minimum number of trades, or just "non-zero activity"? → Check DoraHacks rules.
3. **Drawdown measurement:** Is the 30% cap measured on starting capital or rolling peak? → Confirm from rules.
4. **Wallet funding:** Agent wallet `0xA520...2Ffa` currently has 0 BNB. Must be funded with BNB (gas) + ~$100 USDT (trading capital) before registering/trading.
5. **BSC RPC:** Currently using public `https://bsc-dataseed.binance.org/`. Paid provider (QuickNode/Ankr) recommended for live trading reliability.
