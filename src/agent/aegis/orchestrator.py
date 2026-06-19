"""Aegis orchestrator — the testable seam between the agent loop and the radar.

Given the portfolio state + live prices, it: runs the catalyst scanner (tiered
news/announcement intelligence), builds market snapshots (only for tokens with a
catalyst or an open position — to bound calls), fuses catalyst + market into gated
candidates, and delegates the two-layer decision to event_driven_alpha_momentum.

Scanner and feed are injected, so the loop wiring is unit-testable without any
network. Never signs or broadcasts.
"""
from __future__ import annotations

from ..strategy import event_driven_alpha_momentum as edam
from ..strategy.base_strategy import PortfolioState, TradeOrder
from .volume_anomaly_detector import MarketSnapshot, assess


def run(state: PortfolioState, prices: dict[str, float], *, book, feed, scanner,
        basket_symbols: list[str], now: float | None = None) -> tuple[list[TradeOrder], str]:
    signals = scanner.scan(now=now)

    # Build snapshots only where needed: catalyst tokens + open positions.
    need = {s.symbol for s in signals if s.symbol} | set(book.positions)
    snapshots = feed.build_snapshots(sorted(need), prices) if need else {}

    candidates = []
    for sig in signals:
        snap = snapshots.get(sig.symbol) or MarketSnapshot(symbol=sig.symbol, contract=sig.contract)
        candidates.append(edam.make_candidate_from_signal(sig, assess(snap), snap))

    return edam.decide(candidates=candidates, state=state, book=book, prices=prices,
                       snapshots=snapshots, basket_symbols=basket_symbols, now=now)
