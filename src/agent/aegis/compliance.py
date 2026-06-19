"""Track 1 minimum-trade compliance layer (additive; does not alter the strategy).

Track 1 requires a minimum trade count to qualify: at least 1 valid trade per
contest day and 7 over the trading week. A trade is **valid only if the traded
token is in the official 149 BEP-20 allowlist (matched by CONTRACT ADDRESS)** —
trades outside the list do not count.

This module:
  - records valid trades (eligible-by-contract) with full metadata,
  - tracks per-UTC-day and total counts (persisted under data/runtime/),
  - as a late-day safety net, proposes ONE minimum-size, fully risk-gated trade
    in the safest liquid eligible token if a day is otherwise about to pass with
    no valid trade — never bypassing risk gates, and safe-skipping if no safe
    route exists.

Scoring honesty: the exact organizer scoring (total NAV vs eligible holdings vs
PnL-from-valid-trades) is NOT fully confirmed, so nothing here hard-codes a
stablecoin-NAV assumption. We only guarantee trade *activity* stays inside the
official allowlist; stablecoin is treated as configurable settlement/risk parking.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..data import token_list
from ..monitor.logger import get_logger
from ..strategy.base_strategy import PortfolioState, TradeOrder

log = get_logger(__name__)

STABLE = "USDT"
MIN_ORDER_USD = 2.0
COMPLIANCE_REASON = "MIN_TRADE_COMPLIANCE"
SAFE_SKIP_REASON = "COMPLIANCE_UNMET_SAFE_SKIP"


def is_valid_trade_contract(contract: str) -> bool:
    """A trade counts only if its token is in the official allowlist by address."""
    return bool(contract) and token_list.is_eligible(contract)


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class TradeRecord:
    timestamp: float
    symbol: str
    contract: str
    notional_usd: float
    side: str            # "buy" | "sell"
    source: str          # "event" | "compliance" | ...
    reason: str


@dataclass
class ComplianceReport:
    date: str
    valid_trades_today: int
    valid_trades_total: int
    remaining_today: int
    remaining_total: int
    last_valid_trade: dict | None
    invalid_trades_ignored: int
    reason: str = ""


@dataclass
class ComplianceTracker:
    records: list[TradeRecord] = field(default_factory=list)
    invalid_ignored: int = 0

    # --- counting ---
    def valid_today(self, now_ts: float) -> int:
        today = _utc_date(now_ts)
        return sum(1 for r in self.records if _utc_date(r.timestamp) == today)

    def valid_total(self) -> int:
        return len(self.records)

    def record_executed(self, *, symbol: str, contract: str, notional_usd: float, side: str,
                        source: str, reason: str, now_ts: float) -> bool:
        """Record a trade IFF its token is eligible by contract. Returns counted."""
        if not is_valid_trade_contract(contract):
            self.invalid_ignored += 1
            log.info("compliance_invalid_trade_ignored", symbol=symbol, contract=contract)
            return False
        self.records.append(TradeRecord(now_ts, symbol, contract, float(notional_usd),
                                        side, source, reason))
        return True

    # --- reporting ---
    def report(self, now_ts: float, *, reason: str = "") -> ComplianceReport:
        today = _utc_date(now_ts)
        vt = self.valid_today(now_ts)
        total = self.valid_total()
        last = asdict(self.records[-1]) if self.records else None
        return ComplianceReport(
            date=today, valid_trades_today=vt, valid_trades_total=total,
            remaining_today=max(0, settings.track1_min_trades_per_day - vt),
            remaining_total=max(0, settings.track1_min_trades_total - total),
            last_valid_trade=last, invalid_trades_ignored=self.invalid_ignored, reason=reason)

    # --- persistence ---
    def to_dict(self) -> dict:
        return {"records": [asdict(r) for r in self.records], "invalid_ignored": self.invalid_ignored}

    @classmethod
    def from_dict(cls, d: dict) -> ComplianceTracker:
        recs = [TradeRecord(**r) for r in (d or {}).get("records", [])]
        return cls(records=recs, invalid_ignored=int((d or {}).get("invalid_ignored", 0)))

    @classmethod
    def load(cls, path: Path) -> ComplianceTracker:
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")


def _stable_floor(state: PortfolioState) -> float:
    return max(settings.stablecoin_floor_usd, state.equity_usd * settings.stablecoin_floor_pct)


def pick_compliance_trade(state: PortfolioState, prices: dict[str, float], feed, *,
                          order_usd: float | None = None, held: set[str] | None = None
                          ) -> tuple[TradeOrder | None, str]:
    """Find the SAFEST eligible liquid token for a minimum-trade-compliance buy.

    Iterates the liquid tradable subset (already ordered by slippage), and returns
    the first token that passes the SAME safety gates as a real entry: live route +
    liquidity + slippage, and the stablecoin floor. Returns (None, SAFE_SKIP) if
    nothing is safe — never forces a bad trade. Never bypasses risk gates.
    """
    if state.drawdown_tripped or state.cap_breached:
        return None, SAFE_SKIP_REASON                # breaker: never force a trade
    order_usd = settings.default_order_usd if order_usd is None else order_usd
    order_usd = min(order_usd, settings.max_position_usd)
    held = held or set()
    floor = _stable_floor(state)
    if order_usd < MIN_ORDER_USD or state.stable_value_usd - order_usd < floor:
        return None, SAFE_SKIP_REASON                # would breach floor / too small

    for tok in token_list.tradable_alpha_tokens():
        if tok.symbol in held:
            continue
        if not token_list.is_eligible(tok.contract):  # must be in official allowlist
            continue
        snap = feed.snapshot(tok.symbol, price=prices.get(tok.symbol))
        if not snap.has_route or not snap.liquidity_ok:
            continue                                  # route/liquidity/slippage gate
        order = TradeOrder(STABLE, tok.symbol, order_usd, COMPLIANCE_REASON)
        log.info("compliance_trade_selected", symbol=tok.symbol, notional=order_usd,
                 slippage=round(snap.slippage_est, 4))
        return order, COMPLIANCE_REASON

    log.info("compliance_unmet_safe_skip")
    return None, SAFE_SKIP_REASON
