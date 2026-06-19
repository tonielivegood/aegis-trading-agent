"""Catalyst scoring + the normalized CatalystEvent schema.

Pure scoring (no network). Turns ingested catalyst events — already mapped to a
token — into a 0–100 score with source-tier weighting, multi-source corroboration,
strong-keyword detection, spam/unverified penalties, and time-decay:

  - strongest in the first 0–90 minutes after the catalyst,
  - linear decay 90 min → 5 h,
  - stale (score 0, no new entry) after 5 h.

Tiers: 1 = high-authority (Binance / BNB Chain / CMC / Trust Wallet / CZ),
2 = official project source, 3 = unverified / manual / social (penalised, and
never sufficient alone to enter — that gate lives in the strategy layer).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .event_signal_scanner import AUTHORITIES, SPAM_KEYWORDS

# Strong catalyst keywords (superset of the base scanner's list).
STRONG_KEYWORDS = (
    "listing", "binance alpha", "campaign", "airdrop", "integration", "partnership",
    "launch", "mainnet", "reward", "staking", "burn", "buyback", "migration",
    "trust wallet", "bnb chain", "pancakeswap",
)

# Source tiers.
TIER_AUTHORITY = 1
TIER_PROJECT = 2
TIER_UNVERIFIED = 3

# Weights (rubric).
W_AUTHORITY = 40
W_PROJECT = 30
W_MULTI_SOURCE = 20
W_KEYWORD = 10
P_SPAM = 40
P_UNVERIFIED_ONLY = 15

# Freshness windows.
FRESH_FULL_S = 90 * 60          # full strength for the first 90 minutes
STALE_S = 5 * 3600              # dead after 5 hours
DEFAULT_WINDOW_S = STALE_S      # only events newer than this are scored


@dataclass(frozen=True)
class CatalystEvent:
    event_id: str
    source_name: str
    source_tier: int
    source_url: str
    source_type: str            # "authority" | "project" | "aggregator" | "unverified"
    timestamp: float            # catalyst time (epoch s)
    detected_at: float
    raw_text: str
    normalized_text: str
    mentioned_symbols: tuple[str, ...] = ()
    matched_contracts: tuple[str, ...] = ()
    confidence: float = 0.0
    keywords: tuple[str, ...] = ()
    event_score: float = 0.0
    is_official_source: bool = False
    is_verified_source: bool = False
    freshness_seconds: float = 0.0
    unavailable_reason: str = ""


@dataclass(frozen=True)
class CatalystSignal:
    """Aggregated catalyst standing for ONE token (keyed by contract when known)."""
    symbol: str
    contract: str
    score: float
    source_tier: int            # best (lowest) tier seen
    is_official: bool
    is_verified: bool
    confidence: float
    matched_by: str             # "contract" | "symbol"
    reasons: tuple[str, ...]
    freshness_seconds: float
    n_events: int
    status: str = "WATCHLIST"   # WATCHLIST until market gates pass downstream
    reasons_block: tuple[str, ...] = field(default_factory=tuple)


def tier_of(source_type: str, source_name: str) -> int:
    if source_type == "authority" or source_name.lower() in AUTHORITIES:
        return TIER_AUTHORITY
    if source_type == "project":
        return TIER_PROJECT
    return TIER_UNVERIFIED


def detected_keywords(text: str) -> tuple[str, ...]:
    t = text.lower()
    return tuple(k for k in STRONG_KEYWORDS if k in t)


def is_spam(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in SPAM_KEYWORDS)


def freshness_factor(age_s: float) -> float:
    """1.0 within 90 min, linear decay to 0 at 5 h, 0 after."""
    if age_s <= FRESH_FULL_S:
        return 1.0
    if age_s >= STALE_S:
        return 0.0
    return 1.0 - (age_s - FRESH_FULL_S) / (STALE_S - FRESH_FULL_S)


def aggregate(events: list[CatalystEvent], *, now: float | None = None,
              matched_by: str = "contract") -> CatalystSignal:
    """Combine a token's recent events into a single decayed catalyst score."""
    now = time.time() if now is None else now
    tiers = [e.source_tier for e in events]
    best_tier = min(tiers) if tiers else TIER_UNVERIFIED
    has_authority = any(t == TIER_AUTHORITY for t in tiers)
    has_project = any(t == TIER_PROJECT for t in tiers)

    score = 0.0
    reasons: list[str] = []
    if has_authority:
        score += W_AUTHORITY
        reasons.append(f"+{W_AUTHORITY} Tier-1 authority mention")
    if has_project:
        score += W_PROJECT
        reasons.append(f"+{W_PROJECT} official project post")

    high_sources = {e.source_name.lower() for e in events if e.source_tier <= TIER_PROJECT}
    if len(high_sources) >= 2:
        score += W_MULTI_SOURCE
        reasons.append(f"+{W_MULTI_SOURCE} multiple high-signal sources")

    if any(e.keywords for e in events):
        score += W_KEYWORD
        reasons.append(f"+{W_KEYWORD} strong catalyst keyword")
    if any(is_spam(e.raw_text) for e in events):
        score -= P_SPAM
        reasons.append(f"-{P_SPAM} spam/scam language")
    if best_tier == TIER_UNVERIFIED:
        score -= P_UNVERIFIED_ONLY
        reasons.append(f"-{P_UNVERIFIED_ONLY} unverified-only source")

    newest_age = min((now - e.timestamp for e in events), default=float("inf"))
    f = freshness_factor(newest_age)
    if score > 0:
        score *= f                              # decay only the positive catalyst
        if f < 1.0:
            reasons.append(f"x{f:.2f} freshness decay ({newest_age/60:.0f}m old)")
    score = max(-100.0, min(100.0, score))

    # Confidence: contract-matched + fresh + higher tier => higher confidence.
    conf = (0.9 if matched_by == "contract" else 0.6) * (0.5 + 0.5 * f)
    sym = events[0].mentioned_symbols[0] if events and events[0].mentioned_symbols else ""
    contract = events[0].matched_contracts[0] if events and events[0].matched_contracts else ""

    return CatalystSignal(
        symbol=sym, contract=contract, score=score, source_tier=best_tier,
        is_official=has_authority or has_project,
        is_verified=any(e.is_verified_source for e in events),
        confidence=round(conf, 3), matched_by=matched_by, reasons=tuple(reasons),
        freshness_seconds=newest_age if newest_age != float("inf") else 0.0,
        n_events=len(events),
    )
