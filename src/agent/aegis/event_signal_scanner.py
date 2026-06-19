"""Catalyst/event scoring (0–100) over ingested events.

Pure scoring: no network, no chain. Groups events per token (by contract when
known, else symbol) within a short recency window and scores the catalyst
strength per the radar rubric. The strategy layer applies the allowlist /
liquidity / risk gates — the scanner only judges *signal*.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import Event, ProjectSource

# Top authorities whose mention is the strongest catalyst.
AUTHORITIES = {
    "binance", "binance_announcements", "bnbchain", "bnb chain", "bnb_chain",
    "coinmarketcap", "cmc", "trust wallet", "trustwallet", "trust_wallet",
    "cz", "cz_binance", "czbinance",
}

STRONG_KEYWORDS = (
    "listing", "binance alpha", "campaign", "airdrop", "integration",
    "partnership", "launch", "mainnet", "reward", "staking", "burn",
    "buyback", "migration",
)

SPAM_KEYWORDS = (
    "giveaway", "double your", "guaranteed", "free crypto", "claim now",
    "dm me", "send 0", "1000x guaranteed", "elon", "pump it",
)

HIGH_SIGNAL_TYPES = {"authority", "project", "aggregator"}

DEFAULT_WINDOW_S = 3600          # "short window" for corroboration / recency

# Score weights (rubric).
W_AUTHORITY = 40
W_PROJECT = 30
W_MULTI_SOURCE = 20
W_KEYWORD = 10
P_SPAM = 40                      # penalty
P_UNVERIFIED_ONLY = 15           # penalty when the ONLY signal is unverified


@dataclass(frozen=True)
class EventScore:
    token: str
    contract: str
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    n_events: int = 0


def _has_kw(text: str, kws) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _resolve_key(ev: Event, sources: dict[str, ProjectSource]) -> tuple[str, str]:
    """Return (group_key, contract). Contract is authoritative when known."""
    contract = ev.bsc_contract.strip().lower()
    if not contract and ev.token:
        ps = sources.get(ev.token.upper())
        if ps and ps.bsc_contract:
            contract = ps.bsc_contract.lower()
    key = contract or ev.token.upper()
    return key, contract


def scan(events: list[Event], *, now: float | None = None,
         window_s: int = DEFAULT_WINDOW_S,
         project_sources: dict[str, ProjectSource] | None = None) -> dict[str, EventScore]:
    sources = project_sources or {}
    if now is None:
        now = max((e.timestamp for e in events), default=0.0)

    grouped: dict[str, list[Event]] = {}
    contracts: dict[str, str] = {}
    for ev in events:
        if now - ev.timestamp > window_s:
            continue                       # outside the recency window
        key, contract = _resolve_key(ev, sources)
        grouped.setdefault(key, []).append(ev)
        if contract:
            contracts[key] = contract

    out: dict[str, EventScore] = {}
    for key, evs in grouped.items():
        score = 0.0
        reasons: list[str] = []

        if any(e.source_type == "authority" or e.source.lower() in AUTHORITIES for e in evs):
            score += W_AUTHORITY
            reasons.append(f"+{W_AUTHORITY} authority mention")
        if any(e.source_type == "project" for e in evs):
            score += W_PROJECT
            reasons.append(f"+{W_PROJECT} official project post")

        high_sources = {e.source.lower() for e in evs if e.source_type in HIGH_SIGNAL_TYPES}
        if len(high_sources) >= 2:
            score += W_MULTI_SOURCE
            reasons.append(f"+{W_MULTI_SOURCE} multiple high-signal sources")

        if any(_has_kw(e.text, STRONG_KEYWORDS) for e in evs):
            score += W_KEYWORD
            reasons.append(f"+{W_KEYWORD} strong catalyst keyword")

        if any(_has_kw(e.text, SPAM_KEYWORDS) for e in evs):
            score -= P_SPAM
            reasons.append(f"-{P_SPAM} spam/scam language")

        # If the only signal is unverified (no authority/project/aggregator), penalise.
        if not high_sources and all(e.source_type == "unverified" for e in evs):
            score -= P_UNVERIFIED_ONLY
            reasons.append(f"-{P_UNVERIFIED_ONLY} unverified-only source")

        score = min(100.0, score)          # cap upside at 100; downside uncapped
        token_label = evs[0].token or key
        out[key] = EventScore(token_label, contracts.get(key, ""), score,
                              tuple(reasons), len(evs))
    return out
