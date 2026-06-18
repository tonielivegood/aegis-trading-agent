"""Central typed configuration, loaded once from .env.

Secrets come from environment variables only. Nothing here is ever logged.
Access via the module-level `settings` singleton:

    from src.agent.config import settings
    print(settings.total_budget_usd)
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]

# Load .env from repo root (real env vars still take precedence).
load_dotenv(REPO_ROOT / ".env")


def _get(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


class Settings(BaseModel):
    # --- Wallet / chain ---
    agent_private_key: str = Field(repr=False)        # never shown in repr/logs
    agent_wallet_address: str
    bsc_rpc_url: str
    bsc_chain_id: int = 56

    # --- Contracts ---
    hackathon_contract: str
    pancake_router: str
    wbnb_address: str
    usdt_address: str

    # --- API keys (never logged) ---
    bscscan_api_key: str = Field(repr=False)
    cmc_api_key: str = Field(repr=False)
    cmc_api_base: str = "https://pro-api.coinmarketcap.com"
    anthropic_api_key: str = Field(default="", repr=False)
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # --- Telegram alerts (optional; both empty = disabled) ---
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_chat_id: str = ""

    # --- Risk parameters ---
    total_budget_usd: float = 100.0
    max_drawdown_alert: float = 0.20
    max_drawdown_cap: float = 0.30
    max_position_pct: float = 0.10
    stablecoin_floor_pct: float = 0.20
    deploy_frac: float = 0.65          # fraction of equity deployed to the basket (walk-forward sweet spot: more upside, 0% DQ over 5.7yr)
    basket_size: int = 6               # number of tokens in the deploy basket
    min_trade_interval_h: int = 4
    target_daily_trades: int = 4
    slippage_bps: int = 50
    min_portfolio_value_usd: float = 1.50
    strategy_tick_min: int = 15

    # --- Execution backend: "pancake" (default, registered wallet) or "twak" ---
    execution_backend: str = "pancake"

    # --- TWAK credentials (optional; only needed when execution_backend="twak") ---
    twak_access_id: str = Field(default="", repr=False)
    twak_hmac_secret: str = Field(default="", repr=False)

    # --- Binance Wallet Web3 API (optional; quote/connectivity layer only, never signs) ---
    binance_web3_api_key: str = Field(default="", repr=False)
    binance_web3_api_secret: str = Field(default="", repr=False)
    binance_web3_api_base: str = "https://api.binance.com"

    # --- Mode ---
    dry_run: bool = True

    @field_validator("execution_backend")
    @classmethod
    def _check_backend(cls, v: str) -> str:
        v = v.lower()
        if v not in ("pancake", "twak"):
            raise ValueError("EXECUTION_BACKEND must be 'pancake' or 'twak'")
        return v

    @field_validator("agent_private_key")
    @classmethod
    def _check_pk(cls, v: str) -> str:
        if not v or v.startswith("PASTE_"):
            raise ValueError("AGENT_PRIVATE_KEY is not set in .env")
        return v if v.startswith("0x") else "0x" + v

    @property
    def slippage_fraction(self) -> float:
        return self.slippage_bps / 10_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        agent_private_key=_get("AGENT_PRIVATE_KEY"),
        agent_wallet_address=_get("AGENT_WALLET_ADDRESS"),
        bsc_rpc_url=_get("BSC_RPC_URL", "https://bsc-dataseed.binance.org/"),
        bsc_chain_id=int(_get("BSC_CHAIN_ID", "56")),
        hackathon_contract=_get("HACKATHON_CONTRACT"),
        pancake_router=_get("PANCAKE_ROUTER", "0x10ED43C718714eb63d5aA57B78B54704E256024E"),
        wbnb_address=_get("WBNB_ADDRESS", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
        usdt_address=_get("USDT_ADDRESS", "0x55d398326f99059fF775485246999027B3197955"),
        bscscan_api_key=_get("BSCSCAN_API_KEY", ""),
        cmc_api_key=_get("CMC_API_KEY"),
        cmc_api_base=_get("CMC_API_BASE", "https://pro-api.coinmarketcap.com"),
        anthropic_api_key=_get("ANTHROPIC_API_KEY", ""),
        anthropic_model=_get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
        total_budget_usd=float(_get("TOTAL_BUDGET_USD", "100")),
        max_drawdown_alert=float(_get("MAX_DRAWDOWN_ALERT", "0.20")),
        max_drawdown_cap=float(_get("MAX_DRAWDOWN_CAP", "0.30")),
        max_position_pct=float(_get("MAX_POSITION_PCT", "0.10")),
        stablecoin_floor_pct=float(_get("STABLECOIN_FLOOR_PCT", "0.20")),
        deploy_frac=float(_get("DEPLOY_FRAC", "0.50")),
        basket_size=int(_get("BASKET_SIZE", "6")),
        min_trade_interval_h=int(_get("MIN_TRADE_INTERVAL_H", "4")),
        target_daily_trades=int(_get("TARGET_DAILY_TRADES", "4")),
        slippage_bps=int(_get("SLIPPAGE_BPS", "50")),
        min_portfolio_value_usd=float(_get("MIN_PORTFOLIO_VALUE_USD", "1.50")),
        strategy_tick_min=int(_get("STRATEGY_TICK_MIN", "15")),
        execution_backend=_get("EXECUTION_BACKEND", "pancake"),
        twak_access_id=_get("TWAK_ACCESS_ID", ""),
        twak_hmac_secret=_get("TWAK_HMAC_SECRET", ""),
        binance_web3_api_key=_get("BINANCE_WEB3_API_KEY", ""),
        binance_web3_api_secret=_get("BINANCE_WEB3_API_SECRET", ""),
        binance_web3_api_base=_get("BINANCE_WEB3_API_BASE", "https://api.binance.com"),
        dry_run=_get("DRY_RUN", "true").lower() in ("1", "true", "yes"),
    )


settings = get_settings()
