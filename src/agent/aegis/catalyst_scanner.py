"""Catalyst scanner — aggregates tiered sources into per-token catalyst signals.

Pipeline: collect events from all enabled sources -> dedup by event_id -> drop
events older than the 5h window -> map each event to an eligible token by BSC
CONTRACT ADDRESS (preferred) or symbol (lower confidence) -> group per contract
-> score with freshness decay (catalyst_score.aggregate). Output is a list of
CatalystSignal, strongest first. The strategy layer applies the allowlist /
liquidity / volume / risk gates; a signal alone is only a WATCHLIST entry.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from ..data import token_list
from ..monitor.logger import get_logger
from . import catalyst_score
from .catalyst_score import STALE_S, CatalystEvent, CatalystSignal
from .catalyst_sources import CatalystSource, enabled_sources
from .events import ProjectSource, load_project_sources

log = get_logger(__name__)


class CatalystScanner:
    def __init__(self, sources: list[CatalystSource] | None = None,
                 project_sources: dict[str, ProjectSource] | None = None, *,
                 manual_path: Path | None = None, window_s: int = STALE_S) -> None:
        self.sources = sources if sources is not None else enabled_sources(manual_path=manual_path)
        self.project_sources = project_sources if project_sources is not None else load_project_sources()
        self.window_s = window_s

    def _collect(self) -> list[CatalystEvent]:
        events: list[CatalystEvent] = []
        seen: set[str] = set()
        for src in self.sources:
            try:
                fetched = src.fetch()
            except Exception as e:  # noqa: BLE001 — a bad source must not break the scan
                log.debug("catalyst_source_error", source=getattr(src, "name", "?"), error=type(e).__name__)
                continue
            for ev in fetched:
                if ev.event_id in seen:
                    continue                     # dedup
                seen.add(ev.event_id)
                events.append(ev)
        return events

    def _resolve_contract(self, ev: CatalystEvent) -> tuple[str, str]:
        """Map an event to (contract, matched_by). Contract match is authoritative;
        a symbol-only match is flagged lower-confidence."""
        if ev.matched_contracts:
            return ev.matched_contracts[0].lower(), "contract"
        for sym in ev.mentioned_symbols:
            ps = self.project_sources.get(sym.upper())
            if ps and ps.bsc_contract:
                return ps.bsc_contract.lower(), "contract"   # project mapping confirms
            try:
                return token_list.get_token(sym).contract.lower(), "symbol"
            except KeyError:
                continue
        return "", ""

    def scan(self, now: float | None = None) -> list[CatalystSignal]:
        now = time.time() if now is None else now
        groups: dict[str, list[CatalystEvent]] = {}
        matched_by: dict[str, str] = {}
        symbols: dict[str, str] = {}

        for ev in self._collect():
            if now - ev.timestamp > self.window_s:
                continue                          # stale (>5h) — no scoring
            contract, how = self._resolve_contract(ev)
            if not contract:
                continue                          # can't map to an eligible token
            groups.setdefault(contract, []).append(ev)
            if how == "contract" or contract not in matched_by:
                matched_by[contract] = how
            if ev.mentioned_symbols and contract not in symbols:
                symbols[contract] = ev.mentioned_symbols[0]

        signals: list[CatalystSignal] = []
        for contract, evs in groups.items():
            sig = catalyst_score.aggregate(evs, now=now, matched_by=matched_by[contract])
            sig = replace(sig, contract=contract, symbol=sig.symbol or symbols.get(contract, ""))
            signals.append(sig)

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals
