"""Catalyst scanner tests: aggregation, dedup, contract mapping, fail-safe. No network."""
from __future__ import annotations

from src.agent.aegis import catalyst_score as cs
from src.agent.aegis.catalyst_scanner import CatalystScanner
from src.agent.aegis.catalyst_score import CatalystEvent
from src.agent.aegis.events import ProjectSource
from src.agent.data import token_list

C = "0x1111111111111111111111111111111111111111"
TWT = token_list.get_token("TWT").contract


def _cev(text="listing", source="binance", stype="authority", *, symbols=(), contracts=(),
         ts=1000.0, eid=None):
    tier = cs.tier_of(stype, source)
    return CatalystEvent(
        event_id=eid or f"{source}-{text}-{ts}", source_name=source, source_tier=tier,
        source_url="", source_type=stype, timestamp=ts, detected_at=ts, raw_text=text,
        normalized_text=text.lower(), mentioned_symbols=tuple(symbols),
        matched_contracts=tuple(contracts), keywords=cs.detected_keywords(text),
        is_official_source=(tier <= 2), is_verified_source=(tier <= 2))


class _Source:
    def __init__(self, events, name="src"):
        self._events, self.name = events, name

    def fetch(self):
        return self._events


class _BadSource:
    name = "bad"

    def fetch(self):
        raise RuntimeError("boom")


def _scanner(sources, project_sources=None):
    return CatalystScanner(sources=sources, project_sources=project_sources or {})


# ----------------------------- mapping -----------------------------

def test_binance_authority_mapped_by_contract_is_tier1():
    sigs = _scanner([_Source([_cev(symbols=("FOO",), contracts=(C,))])]).scan(now=1000.0)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.contract == C.lower() and s.source_tier == 1 and s.matched_by == "contract"


def test_symbol_via_project_sources_is_contract_match():
    ps = {"FOO": ProjectSource(symbol="FOO", bsc_contract=C)}
    sigs = _scanner([_Source([_cev(symbols=("FOO",))])], project_sources=ps).scan(now=1000.0)
    assert sigs[0].contract == C.lower() and sigs[0].matched_by == "contract"


def test_symbol_only_match_is_lower_confidence():
    # TWT resolves via token_list but with no project/contract mapping => "symbol"
    sigs = _scanner([_Source([_cev(source="binance", symbols=("TWT",))])]).scan(now=1000.0)
    assert sigs and sigs[0].matched_by == "symbol"


def test_unmappable_event_is_skipped():
    sigs = _scanner([_Source([_cev(symbols=("NOTATOKEN",))])]).scan(now=1000.0)
    assert sigs == []


# ----------------------------- dedup + multi-source -----------------------------

def test_duplicate_events_deduplicated():
    dup = _cev(symbols=("FOO",), contracts=(C,), eid="same")
    sigs = _scanner([_Source([dup], "a"), _Source([dup], "b")]).scan(now=1000.0)
    assert sigs[0].n_events == 1


def test_two_sources_same_token_add_multi_source_bonus():
    e1 = _cev(source="binance", stype="authority", contracts=(C,), eid="1")
    e2 = _cev(source="cz_binance", stype="authority", contracts=(C,), eid="2")
    sig = _scanner([_Source([e1]), _Source([e2])]).scan(now=1000.0)[0]
    assert sig.score >= cs.W_AUTHORITY + cs.W_MULTI_SOURCE


# ----------------------------- freshness + fail-safe -----------------------------

def test_stale_event_excluded():
    old = _cev(contracts=(C,), ts=0.0)
    assert _scanner([_Source([old])]).scan(now=cs.STALE_S + 10_000) == []


def test_bad_source_fails_safe_and_others_still_scan():
    good = _cev(symbols=("FOO",), contracts=(C,))
    sigs = _scanner([_BadSource(), _Source([good])]).scan(now=1000.0)
    assert len(sigs) == 1 and sigs[0].contract == C.lower()


def test_empty_sources_yield_no_signals():
    assert _scanner([]).scan(now=1000.0) == []
