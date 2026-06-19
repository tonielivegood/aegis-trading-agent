"""Catalyst scoring + freshness-decay tests. Pure; no network."""
from __future__ import annotations

from src.agent.aegis import catalyst_score as cs
from src.agent.aegis.catalyst_score import (
    FRESH_FULL_S,
    P_UNVERIFIED_ONLY,
    STALE_S,
    W_AUTHORITY,
    W_KEYWORD,
    W_MULTI_SOURCE,
    W_PROJECT,
    CatalystEvent,
    aggregate,
    freshness_factor,
    tier_of,
)

C = "0x1111111111111111111111111111111111111111"


def _ev(text="listing soon", source="x", stype="unverified", tier=None, ts=1000.0, contract=C):
    tier = tier_of(stype, source) if tier is None else tier
    return CatalystEvent(
        event_id=f"{source}-{ts}", source_name=source, source_tier=tier, source_url="",
        source_type=stype, timestamp=ts, detected_at=ts, raw_text=text,
        normalized_text=text.lower(), mentioned_symbols=("FOO",), matched_contracts=(C,),
        keywords=cs.detected_keywords(text), is_official_source=(tier <= 2),
        is_verified_source=(tier <= 2))


# ----------------------------- tiering -----------------------------

def test_tier_of_classifies_sources():
    assert tier_of("authority", "binance") == cs.TIER_AUTHORITY
    assert tier_of("project", "someproj") == cs.TIER_PROJECT
    assert tier_of("unverified", "rando") == cs.TIER_UNVERIFIED
    assert tier_of("x", "cz_binance") == cs.TIER_AUTHORITY    # authority by name


# ----------------------------- scoring -----------------------------

def test_authority_plus_keyword():
    sig = aggregate([_ev(source="binance", stype="authority")], now=1000.0)
    assert sig.score == W_AUTHORITY + W_KEYWORD and sig.source_tier == 1 and sig.is_official


def test_official_project_post():
    sig = aggregate([_ev(text="update", source="proj", stype="project")], now=1000.0)
    assert sig.score == W_PROJECT and sig.source_tier == 2


def test_multiple_high_signal_sources_bonus():
    evs = [_ev(text="x", source="aggA", stype="aggregator", tier=2),
           _ev(text="x", source="aggB", stype="project")]
    assert aggregate(evs, now=1000.0).score == W_PROJECT + W_MULTI_SOURCE


def test_spam_penalised():
    assert aggregate([_ev(text="free crypto giveaway claim now", source="bot")], now=1000.0).score < 0


def test_unverified_only_penalty():
    sig = aggregate([_ev(text="nothing", source="rando", stype="unverified")], now=1000.0)
    assert sig.score == -P_UNVERIFIED_ONLY and sig.source_tier == 3


def test_score_capped_at_100():
    evs = [_ev(text="listing airdrop partnership", source="binance", stype="authority"),
           _ev(text="launch", source="proj", stype="project")]
    assert aggregate(evs, now=1000.0).score <= 100


def test_symbol_match_lower_confidence_than_contract():
    by_c = aggregate([_ev(source="binance", stype="authority")], now=1000.0, matched_by="contract")
    by_s = aggregate([_ev(source="binance", stype="authority")], now=1000.0, matched_by="symbol")
    assert by_s.confidence < by_c.confidence


# ----------------------------- freshness -----------------------------

def test_freshness_full_then_decays_then_stale():
    assert freshness_factor(0) == 1.0
    assert freshness_factor(FRESH_FULL_S) == 1.0
    mid = freshness_factor((FRESH_FULL_S + STALE_S) / 2)
    assert 0.0 < mid < 1.0
    assert freshness_factor(STALE_S) == 0.0
    assert freshness_factor(STALE_S + 1) == 0.0


def test_decay_reduces_score_for_old_catalyst():
    now = 1_000_000.0
    fresh = aggregate([_ev(source="binance", stype="authority", ts=now)], now=now)
    old = aggregate([_ev(source="binance", stype="authority", ts=now - 3 * 3600)], now=now)
    assert old.score < fresh.score and old.score > 0
