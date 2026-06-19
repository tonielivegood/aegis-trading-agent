"""Loop-level wiring tests for the Aegis radar via the orchestrator seam, plus the
DRY_RUN no-broadcast guarantee. Scanner + feed are injected — no network.

Uses TWT, which is in BOTH the official allowlist and the liquid tradable subset,
so candidates are eligible + tradable (matched by contract address).
"""
from __future__ import annotations

from src.agent.aegis import orchestrator
from src.agent.aegis.catalyst_score import CatalystSignal
from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.data import token_list
from src.agent.strategy.base_strategy import PortfolioState

TWT = token_list.get_token("TWT").contract


def _state(equity=100.0, stable=100.0, **kw):
    return PortfolioState(equity_usd=equity, risk_value_usd=0.0,
                          stable_value_usd=stable, token_values_usd=kw.pop("holdings", {}), **kw)


class _Scanner:
    def __init__(self, signals):
        self._signals = signals

    def scan(self, now=None):
        return self._signals


class _Feed:
    def __init__(self, snaps):
        self._snaps = snaps

    def build_snapshots(self, symbols, prices=None):
        return {s: self._snaps[s] for s in symbols if s in self._snaps}


def _sig(symbol="TWT", contract=TWT, score=100.0, tier=1, official=True):
    return CatalystSignal(symbol=symbol, contract=contract, score=score, source_tier=tier,
                          is_official=official, is_verified=official, confidence=0.9,
                          matched_by="contract", reasons=("+40 Tier-1 authority",),
                          freshness_seconds=10.0, n_events=1)


def _twt_snap(**kw):
    base = dict(symbol="TWT", contract=TWT, vol_5m=400, baseline_vol=100,
                price_now=1.03, price_5m_ago=1.0, recent_pump_pct=0.0,
                slippage_est=0.0, has_route=True, liquidity_ok=True)
    base.update(kw)
    return MarketSnapshot(**base)


def test_loop_uses_layer_b_on_high_confidence_catalyst():
    book = PositionBook()
    orders, mode = orchestrator.run(
        _state(), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": _twt_snap()}),
        scanner=_Scanner([_sig()]), basket_symbols=["TWT"], now=1000.0)
    assert mode == "aegis-event"
    buys = [o for o in orders if o.token_in == "USDT" and o.token_out == "TWT"]
    assert len(buys) == 1 and buys[0].amount_in_usd == 10.0   # fixed $10 order
    assert book.is_open("TWT")


def test_loop_falls_back_to_basket_when_no_catalyst():
    book = PositionBook()
    _, mode = orchestrator.run(
        _state(), {}, book=book, feed=_Feed({}), scanner=_Scanner([]),
        basket_symbols=["A", "B"], now=1000.0)
    assert mode == "baseline-basket"


def test_loop_respects_max_three_positions():
    book = PositionBook()
    for s in ("A", "B", "C"):
        book.open(OpenPosition(symbol=s, contract="0x", entry_price=1.0, usd_size=10.0))
    orders, mode = orchestrator.run(
        _state(), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": _twt_snap()}),
        scanner=_Scanner([_sig()]), basket_symbols=["TWT"], now=1000.0)
    assert mode == "aegis-event"
    assert not book.is_open("TWT")                 # no 4th position opened


def test_loop_stablecoin_floor_blocks_new_entry():
    # equity 100 -> floor max(6, 15)=15; stable 20 -> 20-10=10 < 15 -> no entry
    book = PositionBook()
    orders, _ = orchestrator.run(
        _state(stable=20.0), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": _twt_snap()}),
        scanner=_Scanner([_sig()]), basket_symbols=["TWT"], now=1000.0)
    assert [o for o in orders if o.token_out == "TWT"] == []
    assert not book.is_open("TWT")


def test_catalyst_plus_valid_5m_volume_creates_candidate():
    # Tier-1 catalyst + real-style 5m volume spike + price breakout -> entry
    book = PositionBook()
    snap = _twt_snap(vol_5m=600, baseline_vol=100)
    orders, mode = orchestrator.run(
        _state(), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": snap}),
        scanner=_Scanner([_sig()]), basket_symbols=["TWT"], now=1000.0)
    assert mode == "aegis-event" and book.is_open("TWT")


def test_tier3_only_catalyst_cannot_enter_alone():
    # Unverified/social (Tier 3) even with volume + breakout must stay WATCHLIST.
    book = PositionBook()
    snap = _twt_snap(vol_5m=600, baseline_vol=100)
    orders, _ = orchestrator.run(
        _state(), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": snap}),
        scanner=_Scanner([_sig(tier=3, official=False)]), basket_symbols=["TWT"], now=1000.0)
    assert not book.is_open("TWT")
    assert [o for o in orders if o.token_out == "TWT"] == []


def test_tier2_catalyst_without_volume_stays_watchlist():
    # Tier-2 (official project) with NO volume confirmation: not the Tier-1 fast
    # path, so it must not enter until real volume confirms.
    book = PositionBook()
    snap = _twt_snap(vol_5m=0, baseline_vol=0)        # volume unavailable
    orders, _ = orchestrator.run(
        _state(), {"TWT": 1.03}, book=book, feed=_Feed({"TWT": snap}),
        scanner=_Scanner([_sig(tier=2)]), basket_symbols=["TWT"], now=1000.0)
    assert not book.is_open("TWT")
    assert [o for o in orders if o.token_out == "TWT"] == []


def test_dry_run_execution_never_broadcasts(mocker):
    from src.agent.execution.pancakeswap import PancakeSwap
    ps = PancakeSwap(w3=mocker.Mock(), account=None, dry_run=True)
    mocker.patch.object(ps, "get_amounts_out", return_value=[10 ** 18, 5 * 10 ** 18])
    sign = mocker.patch.object(ps, "_sign_and_send")
    res = ps.swap("USDT", "TWT", 10.0)
    assert res.simulated is True and res.tx_hash is None
    sign.assert_not_called()                       # DRY_RUN hard-gates broadcasting
