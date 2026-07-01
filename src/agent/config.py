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


def _get_bool(key: str, default: str = "false") -> bool:
    return _get(key, default).lower() in ("1", "true", "yes")


def mask_secret(value: str | None) -> str:
    """Mask a secret for safe display: first6...last6, or *** / <absent>.
    Used everywhere a key might otherwise be printed/logged."""
    if not value:
        return "<absent>"
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-6:]}"


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
    claude_advisor_enabled: bool = True   # hourly, advisory, TIGHTENING-ONLY Claude regime overlay

    # --- Telegram alerts (optional; both empty = disabled) ---
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_chat_id: str = ""

    # --- Risk parameters ---
    total_budget_usd: float = 100.0
    max_drawdown_alert: float = 0.20
    max_drawdown_cap: float = 0.30
    drawdown_latch_ticks: int = 3      # breaker latches only after N consecutive breach ticks (anti-glitch debounce)
    daily_soft_breaker_pct: float = 0.08  # halt NEW entries for the rest of the UTC day if intraday DD >= this (0 disables)
    max_concurrent_positions: int = 3     # GLOBAL cap on simultaneous positions in RISK_ON (CAUTIOUS=1, RISK_OFF=0).
                                          # Alts are BTC-correlated → total exposure is the DD-gate risk; keep it small.
                                          # 2->3 post-contest (1/7): beta (2 names) was starving meme of every slot.
    max_position_pct: float = 0.10
    stablecoin_floor_pct: float = 0.15
    deploy_frac: float = 0.65          # fraction of equity deployed to the basket (walk-forward sweet spot: more upside, 0% DQ over 5.7yr)
    basket_size: int = 6               # number of tokens in the deploy basket
    min_trade_interval_h: int = 4
    target_daily_trades: int = 4
    slippage_bps: int = 400   # 4% — liquidity gate AND execution min-out. Set for the asymmetric-ride
                              # universe: the 0.5% gate admitted only ~13 deepest tokens; 4% unlocks the
                              # liquid middle (ADA/ASTER/XRP/ZIL ≈ 17 total) while still excluding the
                              # on-chain liquidity cliff (TWT 8.6%+ → AAVE/UNI/DOT 30-100%, untradable on BSC).
    min_portfolio_value_usd: float = 1.50
    strategy_tick_min: int = 15

    # --- Strategy selector: "event_alpha" (event radar primary + basket fallback) | "baseline" ---
    strategy_mode: str = "event_alpha"
    event_radar_enabled: bool = True

    # --- Layer A: eligible-basket fallback ---
    basket_max_position_pct: float = 0.05    # tiny per-token cap for the meme basket (3-5%)

    # --- Layer B: event-driven alpha radar (all DRY_RUN-gated) ---
    event_signal_threshold: float = 70.0     # min combined score to open a position
    max_open_positions: int = 3              # max concurrent event positions
    default_order_usd: float = 10.0          # USD per entry
    max_position_usd: float = 10.0           # hard per-token notional cap
    meme_order_pct: float = 0.15               # post-contest (1/7): meme ticket = % of equity, not a fixed $
                                               # amount — bigger bets on the same small capital, per the
                                               # tighter exit re-tune (fewer, higher-conviction tickets).
    meme_order_cap_usd: float = 20.0           # absolute ceiling regardless of %: thin meme pools can't
                                               # absorb an arbitrarily large order as equity grows.
    meme_slippage_bps: int = 600             # ...and a looser 6% gate — a meme ride targets +100%, not +5%
    oneinch_api_key: str = Field(default="", repr=False)   # 1inch aggregator (portal.1inch.dev); dormant if empty
    oneinch_base_url: str = ""
    stablecoin_floor_usd: float = 6.0        # never let settlement cash drop below this (USD)
    # stablecoin_floor_pct (above, risk block) also applies; floor = max(usd, pct*equity)
    max_hold_minutes: int = 1440             # 24h backstop only (NOT a time exit); rides exit on trail/TP/stop
    aegis_meme_recycle_minutes: int = 480    # recycle a FIZZLED meme (peak never reached the breakeven trigger)
                                             # after 8h — frees a scarce slot for beta. Real runners (peak >= +5%)
                                             # are exempt. 0 = off. Sits ABOVE the 24h max-hold for duds only.
    min_hold_minutes_for_volume_exit: int = 15   # don't volume-exit on noisy first candles
    volume_exit_multiple: float = 5.0        # exit when 5m volume hits Nx the entry baseline
    hard_take_profit_multiple: float = 2.0   # exit full when position value reaches Nx (10->20 USD)
    aegis_vol_spike_mult: float = 3.0        # 5m volume vs baseline = entry confirmation spike
    aegis_breakout_pct: float = 0.015        # 5m price breakout threshold (entry confirmation)
    aegis_overpump_pct: float = 0.20         # skip entry only if already blown off >20% over the recent window
    aegis_trailing_stop_pct: float = 0.03    # trail this far below peak once profitable
    aegis_fomo_trailing_pct: float = 0.015   # TIGHTER trail while a 5x volume blow-off is active
    aegis_hard_stop_pct: float = 0.08        # hard per-position stop loss
    # v2 sniper exits: cut dead trades + bank when the inflow that drove the move dies.
    aegis_no_progress_minutes: int = 15      # cut a flat/dead position after this long...
    aegis_no_progress_min_gain: float = 0.02 # ...if it still hasn't risen at least this much
    aegis_volume_death_mult: float = 1.0     # exit in profit when 5m vol < this x baseline (inflow gone)
    aegis_volume_death_in_profit: bool = False  # redesign: let the trailing stop bank the ride, don't bail on a volume dip
    # Breakeven stop: once a trade has run to +trigger, exit ~flat if it falls back to entry
    # (+buffer for fees) instead of riding it down to the hard stop. Protects "popped then
    # faded" trades — the gap the trailing stop (gated on price>entry) can't cover.
    aegis_breakeven_trigger_pct: float = 0.05  # arm once peak gain reaches +5%
    aegis_breakeven_buffer_pct: float = 0.005  # exit at entry +0.5% (covers the round-trip fee)
    # --- Beta core (barbell Phase-2): regime-gated momentum-major basket. ---
    # Post-contest (1/7): ENABLED as a real profit sleeve, retuned tighter (see
    # docs/superpowers/specs/2026-07-01-beta-core-fear-gate-design.md) — a real whipsaw
    # (24/6, CAUTIOUS + extreme fear + chop) is now fixed via regime.beta_regime().
    beta_core_enabled: bool = True            # master switch (live tick wiring is gated on this)
    beta_core_max_names: int = 2             # RISK_ON basket size. 2 (not 3): at ~$33 NAV a 3rd name
                                             # can't clear floor+reserve (3×25% + $6 + $5 > stable). 2×25%
                                             # = 50% deploy fits + leaves room for a meme. Bump once equity grows.
    beta_core_position_pct: float = 0.25     # per-name size as a fraction of NAV (0.20->0.25, 1/7)
    beta_core_min_momentum: float = 4.0      # min blended (1h+24h) momentum %. 4 (not 2): +2% is noise —
                                             # buying it then trailing at 6% = whipsaw. 4% = only real leaders.
    beta_core_mom_w1h: float = 0.5           # weight on 1h vs 1.0 on 24h in the momentum blend
    beta_core_hard_tp_pct: float = 0.15      # NEW (1/7): bank a clean win at +15% instead of only riding —
                                             # post-contest goal is consistent hits, not an unbounded ride.
    beta_core_trail_pct: float = 0.05        # trailing stop, tightened 0.06->0.05 (1/7) — bank major moves harder.
    beta_core_hard_stop_pct: float = 0.05    # hard per-name stop, tightened 0.08->0.05 (1/7) — paired with the
                                             # tighter trail and the new hard TP; faster loss-cut, disciplined sizing.
    # Rotation hysteresis (anti-whipsaw). Day-1 live churned majors FORM→HOME→BAT→DEXE (a name held
    # 24 min) because the entry & exit momentum thresholds were the same — a major hovering near 4%
    # got bought-and-sold each tick. The hold band + margin + min-hold stop that churn.
    beta_core_exit_min_momentum: float = 2.0  # hold a name until momentum truly decays below this (< the
                                              # 4% entry gate) — the band that prevents dip-under-4% churn.
    beta_core_rotation_margin: float = 3.0    # a challenger must beat a held name by >= this many momentum
                                              # points to displace it (incumbency advantage) → no marginal rotations.
    beta_core_min_hold_sec: float = 1800.0    # min hold (30 min) before a momentum rotation; price stops
                                              # (hard/breakeven/trail) ALWAYS fire regardless. Blocks sub-tick flips.
    # (the meme cash reserve is computed dynamically = meme ticket size × the regime's meme slots)
    # --- Tournament clock (#3): convex-when-behind. In the final window AND only while NOT
    # yet likely in a paying spot, ESCALATE the meme lottery sleeve (extra slots + bigger
    # ticket + ignore the daily soft breaker) — never beta. OFF by default; flip on for the
    # final 48h after calibrating safe_return off the leaderboard recon. ---
    tournament_clock_enabled: bool = False       # master switch (live wiring gated on this)
    tournament_clock_end: str = "2026-06-28T00:00:00Z"  # contest-end UTC (confirm exact end)
    tournament_clock_arm_days: float = 2.0       # escalation arms in the final N days
    tournament_clock_full_send_days: float = 1.0 # heavier tier in the final N days
    tournament_clock_safe_return: float = 0.15   # >= this return => PROTECT, don't push (PROVISIONAL — calibrate post-25/6)
    tournament_clock_max_push_dd: float = 0.15   # kill-switch: stop escalating once DD hits this
    tournament_clock_extra_slots_arm: int = 1    # extra lottery slots in the arm tier
    tournament_clock_extra_slots_full: int = 2   # extra lottery slots in the full-send tier
    tournament_clock_ticket_mult_arm: float = 1.4   # meme ticket size multiplier (arm)
    tournament_clock_ticket_mult_full: float = 2.0  # meme ticket size multiplier (full-send)
    # v2 sniper: breakout entry cap + cooldown + hourly regime cadence/staleness
    aegis_breakout_max_pct: float = 0.10     # entry: price rising but <= this (don't chase a blow-off)
    aegis_cooldown_seconds: int = 5400       # no re-entry into a token for 90 min after an exit
    regime_update_seconds: int = 3600        # hourly regime updater cadence
    regime_max_age_seconds: int = 7200       # a regime flag older than this => CAUTIOUS fallback
    # CMC Agent Hub macro guard: the agent reads CMC's macro/catalyst calendar (the
    # get_upcoming_macro_events skill) hourly and stands DOWN (halts NEW entries) when a
    # catalyst lands within macro_guard_days. Tightening-only + fail-safe.
    macro_events_enabled: bool = True        # master switch for the macro-calendar guard + panel
    macro_guard_days: int = 1                # halt new entries if a catalyst is within this many days
    min_gas_bnb: float = 0.001               # block NEW buys if native BNB gas drops below this (BSC gas is cheap now)

    # --- Real 5-minute volume source (Binance Alpha klines) + event timing ---
    # Entry gating for catalyst-driven trades.
    aegis_require_volume_confirmation: bool = True   # entry needs real 5m volume confirmation
    aegis_fast_confirm_tier1: bool = True            # Tier-1 authority may enter on price+liquidity (faster)

    volume_source: str = "binance_alpha_klines"   # "binance_alpha_klines" | "none"
    binance_alpha_api_base: str = "https://www.binance.com"
    binance_spot_api_base: str = "https://data-api.binance.vision"   # majors volume (api.binance.com is 451 geo-blocked)
    alpha_kline_interval: str = "5m"
    alpha_baseline_candles: int = 24          # recent 5m candles for the baseline
    alpha_freshness_seconds: int = 600        # reject candles older than this (2x 5m)
    event_tick_seconds: int = 60              # event-mode loop cadence while FLAT (no urgency scanning)
    event_tick_seconds_holding: int = 30      # faster cadence while a position is OPEN — protects/harvests
                                              # it more closely (the MYX lesson: a slow tick missed the real
                                              # peak). Conservative first step (60->30); watch logs before
                                              # tuning lower toward 15-10s via env, don't jump straight there.

    # --- Track 1 contest compliance (min-trade qualification; does NOT alter the strategy) ---
    # Contest ended — OFF by default. Forcing a daily trade has no purpose once there's no
    # organizer activity requirement; a permanent bot should only trade real setups.
    track1_compliance_enabled: bool = False
    track1_min_trades_per_day: int = 1
    track1_min_trades_total: int = 7
    track1_compliance_mode: str = "dry_run_safe"
    track1_compliance_after_hour_utc: int = 20   # late-day safety net: let real signals trade first
    track1_scoring_mode: str = "unconfirmed"     # organizer NAV/holdings scoring not fully confirmed
    track1_settlement_asset: str = "USDT"        # configurable settlement / risk-parking asset
    track1_score_nav_assumption: str = "unknown_do_not_hardcode"

    # --- Execution backend: "pancake" (default, registered wallet) or "twak" ---
    execution_backend: str = "pancake"

    # --- TWAK credentials (optional; only needed when execution_backend="twak") ---
    twak_access_id: str = Field(default="", repr=False)
    twak_hmac_secret: str = Field(default="", repr=False)

    # --- Binance Wallet Web3 API (quote/route/connectivity layer; NEVER signs/broadcasts) ---
    binance_web3_enabled: bool = False            # master switch for the Web3 API layer
    binance_web3_api_key: str = Field(default="", repr=False)
    binance_web3_api_secret: str = Field(default="", repr=False)
    binance_web3_base_url: str = "https://api.binance.com"
    binance_web3_quote_enabled: bool = False      # allow quote/route discovery
    binance_web3_execution_enabled: bool = False  # build unsigned tx (still no broadcast)
    binance_web3_broadcast_enabled: bool = False  # MUST stay false: we never broadcast here
    binance_web3_mev_protection_enabled: bool = True
    # Binance Alpha live market data (5m volume) — INDEPENDENT of the Web3 execution flags.
    binance_alpha_market_data_enabled: bool = True
    catalyst_x_enabled: bool = False              # X/Twitter catalyst adapter (needs X_BEARER_TOKEN)

    # --- Binance W3W universe (post-contest, 1/7): server-side-filtered meme discovery
    # (hot-token) + batch pricing/volume (price-info) + just-in-time honeypot/tax check
    # (aggregator/quote), replacing the 149-token static list + CMC full-universe pricing.
    # Feature-flagged so a bad soak reverts with one env change, no redeploy. ---
    binance_w3w_universe_enabled: bool = True
    binance_w3w_max_tax_rate: float = 0.10        # reject a candidate taxed above this (matches
                                                  # Binance's own query-token-audit "critical" bar)

    # --- Mode ---
    dry_run: bool = True

    @field_validator("execution_backend")
    @classmethod
    def _check_backend(cls, v: str) -> str:
        v = v.lower()
        if v not in ("pancake", "twak", "openocean", "1inch", "oneinch"):
            raise ValueError("EXECUTION_BACKEND must be pancake | twak | openocean | 1inch")
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

    def diagnostics(self) -> dict[str, str]:
        """Safe-to-print status: secrets are MASKED, flags shown as-is. Never
        returns a full key — use this for any status output/logging."""
        return {
            "dry_run": str(self.dry_run),
            "strategy_mode": self.strategy_mode,
            "cmc_api_key": mask_secret(self.cmc_api_key),
            "binance_web3_enabled": str(self.binance_web3_enabled),
            "binance_web3_api_key": mask_secret(self.binance_web3_api_key),
            "binance_web3_api_secret": mask_secret(self.binance_web3_api_secret),
            "binance_web3_base_url": self.binance_web3_base_url,
            "binance_web3_quote_enabled": str(self.binance_web3_quote_enabled),
            "binance_web3_execution_enabled": str(self.binance_web3_execution_enabled),
            "binance_web3_broadcast_enabled": str(self.binance_web3_broadcast_enabled),
            "binance_web3_mev_protection_enabled": str(self.binance_web3_mev_protection_enabled),
            "binance_alpha_market_data_enabled": str(self.binance_alpha_market_data_enabled),
            "volume_source": self.volume_source,
        }


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
        claude_advisor_enabled=_get_bool("CLAUDE_ADVISOR_ENABLED", "true"),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
        total_budget_usd=float(_get("TOTAL_BUDGET_USD", "100")),
        max_drawdown_alert=float(_get("MAX_DRAWDOWN_ALERT", "0.20")),
        max_drawdown_cap=float(_get("MAX_DRAWDOWN_CAP", "0.30")),
        drawdown_latch_ticks=int(_get("DRAWDOWN_LATCH_TICKS", "3")),
        daily_soft_breaker_pct=float(_get("DAILY_SOFT_BREAKER_PCT", "0.08")),
        max_concurrent_positions=int(_get("MAX_CONCURRENT_POSITIONS", "3")),
        max_position_pct=float(_get("MAX_POSITION_PCT", "0.10")),
        stablecoin_floor_pct=float(_get("STABLECOIN_FLOOR_PCT", "0.15")),
        deploy_frac=float(_get("DEPLOY_FRAC", "0.50")),
        basket_size=int(_get("BASKET_SIZE", "6")),
        min_trade_interval_h=int(_get("MIN_TRADE_INTERVAL_H", "4")),
        target_daily_trades=int(_get("TARGET_DAILY_TRADES", "4")),
        slippage_bps=int(_get("SLIPPAGE_BPS", "50")),
        min_portfolio_value_usd=float(_get("MIN_PORTFOLIO_VALUE_USD", "1.50")),
        strategy_tick_min=int(_get("STRATEGY_TICK_MIN", "15")),
        strategy_mode=_get("STRATEGY_MODE", "event_alpha"),
        event_radar_enabled=_get("EVENT_RADAR_ENABLED", "true").lower() in ("1", "true", "yes"),
        basket_max_position_pct=float(_get("BASKET_MAX_POSITION_PCT", "0.05")),
        event_signal_threshold=float(_get("EVENT_SIGNAL_THRESHOLD", "70")),
        max_open_positions=int(_get("MAX_OPEN_POSITIONS", "3")),
        default_order_usd=float(_get("DEFAULT_ORDER_USD", "10")),
        max_position_usd=float(_get("MAX_POSITION_USD", "10")),
        meme_order_pct=float(_get("MEME_ORDER_PCT", "0.15")),
        meme_order_cap_usd=float(_get("MEME_ORDER_CAP_USD", "20")),
        meme_slippage_bps=int(_get("MEME_SLIPPAGE_BPS", "600")),
        oneinch_api_key=_get("ONEINCH_API_KEY", ""),
        oneinch_base_url=_get("ONEINCH_BASE_URL", ""),
        stablecoin_floor_usd=float(_get("STABLECOIN_FLOOR_USD", "6")),
        max_hold_minutes=int(_get("MAX_HOLD_MINUTES", "1440")),
        aegis_meme_recycle_minutes=int(_get("AEGIS_MEME_RECYCLE_MINUTES", "480")),
        min_hold_minutes_for_volume_exit=int(_get("MIN_HOLD_MINUTES_FOR_VOLUME_EXIT", "15")),
        volume_exit_multiple=float(_get("VOLUME_EXIT_MULTIPLE", "5")),
        hard_take_profit_multiple=float(_get("HARD_TAKE_PROFIT_MULTIPLE", "3.0")),
        aegis_vol_spike_mult=float(_get("AEGIS_VOL_SPIKE_MULT", "3.0")),
        aegis_breakout_pct=float(_get("AEGIS_BREAKOUT_PCT", "0.015")),
        aegis_overpump_pct=float(_get("AEGIS_OVERPUMP_PCT", "0.20")),
        aegis_trailing_stop_pct=float(_get("AEGIS_TRAILING_STOP_PCT", "0.15")),
        aegis_fomo_trailing_pct=float(_get("AEGIS_FOMO_TRAILING_PCT", "0.05")),
        aegis_hard_stop_pct=float(_get("AEGIS_HARD_STOP_PCT", "0.08")),
        aegis_no_progress_minutes=int(_get("AEGIS_NO_PROGRESS_MINUTES", "15")),
        aegis_no_progress_min_gain=float(_get("AEGIS_NO_PROGRESS_MIN_GAIN", "0.02")),
        aegis_volume_death_mult=float(_get("AEGIS_VOLUME_DEATH_MULT", "1.0")),
        aegis_volume_death_in_profit=_get("AEGIS_VOLUME_DEATH_IN_PROFIT", "false").lower() in ("1", "true", "yes"),
        aegis_breakeven_trigger_pct=float(_get("AEGIS_BREAKEVEN_TRIGGER_PCT", "0.05")),
        aegis_breakeven_buffer_pct=float(_get("AEGIS_BREAKEVEN_BUFFER_PCT", "0.005")),
        beta_core_enabled=_get_bool("BETA_CORE_ENABLED", "true"),
        beta_core_max_names=int(_get("BETA_CORE_MAX_NAMES", "2")),
        beta_core_position_pct=float(_get("BETA_CORE_POSITION_PCT", "0.25")),
        beta_core_min_momentum=float(_get("BETA_CORE_MIN_MOMENTUM", "4.0")),
        beta_core_mom_w1h=float(_get("BETA_CORE_MOM_W1H", "0.5")),
        beta_core_hard_tp_pct=float(_get("BETA_CORE_HARD_TP_PCT", "0.15")),
        beta_core_trail_pct=float(_get("BETA_CORE_TRAIL_PCT", "0.05")),
        beta_core_hard_stop_pct=float(_get("BETA_CORE_HARD_STOP_PCT", "0.05")),
        beta_core_exit_min_momentum=float(_get("BETA_CORE_EXIT_MIN_MOMENTUM", "2.0")),
        beta_core_rotation_margin=float(_get("BETA_CORE_ROTATION_MARGIN", "3.0")),
        beta_core_min_hold_sec=float(_get("BETA_CORE_MIN_HOLD_SEC", "1800.0")),
        tournament_clock_enabled=_get_bool("TOURNAMENT_CLOCK_ENABLED", "false"),
        tournament_clock_end=_get("TOURNAMENT_CLOCK_END", "2026-06-28T00:00:00Z"),
        tournament_clock_arm_days=float(_get("TOURNAMENT_CLOCK_ARM_DAYS", "2.0")),
        tournament_clock_full_send_days=float(_get("TOURNAMENT_CLOCK_FULL_SEND_DAYS", "1.0")),
        tournament_clock_safe_return=float(_get("TOURNAMENT_CLOCK_SAFE_RETURN", "0.15")),
        tournament_clock_max_push_dd=float(_get("TOURNAMENT_CLOCK_MAX_PUSH_DD", "0.15")),
        tournament_clock_extra_slots_arm=int(_get("TOURNAMENT_CLOCK_EXTRA_SLOTS_ARM", "1")),
        tournament_clock_extra_slots_full=int(_get("TOURNAMENT_CLOCK_EXTRA_SLOTS_FULL", "2")),
        tournament_clock_ticket_mult_arm=float(_get("TOURNAMENT_CLOCK_TICKET_MULT_ARM", "1.4")),
        tournament_clock_ticket_mult_full=float(_get("TOURNAMENT_CLOCK_TICKET_MULT_FULL", "2.0")),
        aegis_breakout_max_pct=float(_get("AEGIS_BREAKOUT_MAX_PCT", "0.10")),
        aegis_cooldown_seconds=int(_get("AEGIS_COOLDOWN_SECONDS", "5400")),
        regime_update_seconds=int(_get("REGIME_UPDATE_SECONDS", "3600")),
        regime_max_age_seconds=int(_get("REGIME_MAX_AGE_SECONDS", "7200")),
        macro_events_enabled=_get_bool("MACRO_EVENTS_ENABLED", "true"),
        macro_guard_days=int(_get("MACRO_GUARD_DAYS", "1")),
        min_gas_bnb=float(_get("MIN_GAS_BNB", "0.001")),
        aegis_require_volume_confirmation=_get("AEGIS_REQUIRE_VOLUME_CONFIRMATION", "true").lower() in ("1", "true", "yes"),
        aegis_fast_confirm_tier1=_get("AEGIS_FAST_CONFIRM_TIER1", "true").lower() in ("1", "true", "yes"),
        volume_source=_get("VOLUME_SOURCE", "binance_alpha_klines"),
        binance_alpha_api_base=_get("BINANCE_ALPHA_API_BASE", "https://www.binance.com"),
        binance_spot_api_base=_get("BINANCE_SPOT_API_BASE", "https://data-api.binance.vision"),
        alpha_kline_interval=_get("ALPHA_KLINE_INTERVAL", "5m"),
        alpha_baseline_candles=int(_get("ALPHA_BASELINE_CANDLES", "24")),
        alpha_freshness_seconds=int(_get("ALPHA_FRESHNESS_SECONDS", "600")),
        event_tick_seconds=int(_get("EVENT_TICK_SECONDS", "60")),
        event_tick_seconds_holding=int(_get("EVENT_TICK_SECONDS_HOLDING", "30")),
        track1_compliance_enabled=_get_bool("TRACK1_COMPLIANCE_ENABLED", "false"),
        track1_min_trades_per_day=int(_get("TRACK1_MIN_TRADES_PER_DAY", "1")),
        track1_min_trades_total=int(_get("TRACK1_MIN_TRADES_TOTAL", "7")),
        track1_compliance_mode=_get("TRACK1_COMPLIANCE_MODE", "dry_run_safe"),
        track1_compliance_after_hour_utc=int(_get("TRACK1_COMPLIANCE_AFTER_HOUR_UTC", "20")),
        track1_scoring_mode=_get("TRACK1_SCORING_MODE", "unconfirmed"),
        track1_settlement_asset=_get("TRACK1_SETTLEMENT_ASSET", "USDT"),
        track1_score_nav_assumption=_get("TRACK1_SCORE_NAV_ASSUMPTION", "unknown_do_not_hardcode"),
        execution_backend=_get("EXECUTION_BACKEND", "pancake"),
        twak_access_id=_get("TWAK_ACCESS_ID", ""),
        twak_hmac_secret=_get("TWAK_HMAC_SECRET", ""),
        binance_web3_enabled=_get_bool("BINANCE_WEB3_ENABLED"),
        binance_web3_api_key=_get("BINANCE_WEB3_API_KEY", ""),
        binance_web3_api_secret=_get("BINANCE_WEB3_API_SECRET", ""),
        binance_web3_base_url=(_get("BINANCE_WEB3_BASE_URL", "")
                               or _get("BINANCE_WEB3_API_BASE", "https://api.binance.com")),
        binance_web3_quote_enabled=_get_bool("BINANCE_WEB3_QUOTE_ENABLED"),
        binance_web3_execution_enabled=_get_bool("BINANCE_WEB3_EXECUTION_ENABLED"),
        binance_web3_broadcast_enabled=_get_bool("BINANCE_WEB3_BROADCAST_ENABLED"),
        binance_web3_mev_protection_enabled=_get_bool("BINANCE_WEB3_MEV_PROTECTION_ENABLED", "true"),
        binance_alpha_market_data_enabled=_get_bool("BINANCE_ALPHA_MARKET_DATA_ENABLED", "true"),
        binance_w3w_universe_enabled=_get_bool("BINANCE_W3W_UNIVERSE_ENABLED", "true"),
        binance_w3w_max_tax_rate=float(_get("BINANCE_W3W_MAX_TAX_RATE", "0.10")),
        catalyst_x_enabled=_get_bool("CATALYST_X_ENABLED"),
        dry_run=_get("DRY_RUN", "true").lower() in ("1", "true", "yes"),
    )


settings = get_settings()
