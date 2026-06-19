"""Catalyst-scoring tests for the Aegis event radar. Pure; no network."""
from __future__ import annotations

from src.agent.aegis import event_signal_scanner as scanner
from src.agent.aegis.events import Event, ProjectSource

C = "0x1111111111111111111111111111111111111111"


def _ev(text="listing soon", source="x", stype="unverified", contract=C, token="FOO", ts=1000.0):
    return Event(token=token, text=text, source=source, source_type=stype,
                 bsc_contract=contract, timestamp=ts)


def test_authority_mention_scores_40_plus_keyword():
    s = scanner.scan([_ev(source="binance", stype="authority")], now=1000.0)
    score = s[C.lower()]
    assert score.score == 40 + 10  # authority + strong keyword "listing"
    assert any("authority" in r for r in score.reasons)


def test_official_project_post_scores_30():
    s = scanner.scan([_ev(text="general update", source="proj", stype="project")], now=1000.0)
    assert s[C.lower()].score == 30


def test_multiple_high_signal_sources_add_20():
    evs = [_ev(text="x", source="aggA", stype="aggregator"),
           _ev(text="x", source="aggB", stype="aggregator")]
    # two distinct high-signal sources -> +20 (no authority/project/keyword)
    assert scanner.scan(evs, now=1000.0)[C.lower()].score == 20


def test_spam_is_penalised():
    s = scanner.scan([_ev(text="free crypto giveaway claim now", source="bot")], now=1000.0)
    assert s[C.lower()].score < 0


def test_unverified_only_penalty():
    s = scanner.scan([_ev(text="nothing special", source="rando", stype="unverified")], now=1000.0)
    assert s[C.lower()].score == -scanner.P_UNVERIFIED_ONLY


def test_recency_window_excludes_old_events():
    old = _ev(source="binance", stype="authority", ts=0.0)
    s = scanner.scan([old], now=10_000.0, window_s=3600)
    assert s == {}  # outside the window


def test_symbol_resolves_to_contract_via_project_sources():
    sources = {"FOO": ProjectSource(symbol="FOO", bsc_contract=C)}
    ev = Event(token="FOO", text="partnership", source="binance",
               source_type="authority", bsc_contract="", timestamp=1000.0)
    s = scanner.scan([ev], now=1000.0, project_sources=sources)
    assert C.lower() in s and s[C.lower()].contract == C.lower()


def test_score_capped_at_100():
    evs = [_ev(text="listing airdrop partnership", source="binance", stype="authority"),
           _ev(text="launch", source="proj", stype="project"),
           _ev(text="campaign", source="aggX", stype="aggregator")]
    assert scanner.scan(evs, now=1000.0)[C.lower()].score <= 100
