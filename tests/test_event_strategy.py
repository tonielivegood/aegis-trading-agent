"""Layer B — event-driven strategy: entry gates, exit policy, two-layer orchestration.

Sizing/exit policy under test (per spec):
  - fixed $10 order, $10 per-token notional cap, max 3 positions, no pyramiding
  - stablecoin floor = max(STABLECOIN_FLOOR_USD, pct*equity); never breach it
  - exits: breaker > hard TP 2x > hard SL > max-hold 5h > volume 5x > FOMO > trailing
"""
from __future__ import annotations

from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.strategy import event_driven_alpha_momentum as edam
from src.agent.strategy.base_strategy import PortfolioState


def _state(equity=100.0, holdings=None, stable=None, **kw):
    holdings = holdings or {}
    risk = sum(v for s, v in holdings.items() if s != "USDT")
    stable = equity if stable is None else stable
    return PortfolioState(equity_usd=equity, risk_value_usd=risk,
                          stable_value_usd=stable, token_values_usd=holdings, **kw)


def _cand(sym="FOO", score=80.0, breakout=0.05, pump=0.0, eligible=True, tradable=True):
    return edam.Candidate(symbol=sym, contract="0x" + sym, event_score=score,
                          confirmation_score=0.0, risk_penalty=0.0,
                          breakout_pct=breakout, recent_pump_pct=pump,
                          eligible=eligible, tradable=tradable)


# ----------------------------- entries -----------------------------

def test_entry_opens_fixed_size_position():
    orders = edam.decide_entries([_cand()], _state(), PositionBook(),
                                 threshold=70, order_usd=10, max_position_usd=10)
    assert len(orders) == 1
    o = orders[0]
    assert o.token_in == "USDT" and o.token_out == "FOO" and o.amount_in_usd == 10.0


def test_order_size_capped_at_max_position_usd():
    orders = edam.decide_entries([_cand()], _state(), PositionBook(),
                                 threshold=70, order_usd=50, max_position_usd=10)
    assert orders[0].amount_in_usd == 10.0      # capped


def test_entry_skipped_below_threshold():
    assert edam.decide_entries([_cand(score=50)], _state(), PositionBook(), threshold=70) == []


def test_entry_skipped_when_price_not_confirmed():
    assert edam.decide_entries([_cand(breakout=0.0)], _state(), PositionBook(),
                               threshold=70, breakout_min=0.015) == []


def test_entry_skipped_when_already_pumped():
    assert edam.decide_entries([_cand(pump=0.30)], _state(), PositionBook(),
                               threshold=70, overpump_pct=0.15) == []


def test_entry_requires_eligible_and_tradable():
    assert edam.decide_entries([_cand(eligible=False)], _state(), PositionBook(), threshold=70) == []
    assert edam.decide_entries([_cand(tradable=False)], _state(), PositionBook(), threshold=70) == []


def test_entry_respects_max_positions():
    book = PositionBook()
    for s in ("A", "B", "C"):
        book.open(OpenPosition(symbol=s, contract="0x", entry_price=1.0, usd_size=10.0))
    assert edam.decide_entries([_cand()], _state(), book, threshold=70, max_positions=3) == []


def test_no_pyramiding_into_open_token():
    book = PositionBook()
    book.open(OpenPosition(symbol="FOO", contract="0x", entry_price=1.0, usd_size=10.0))
    assert edam.decide_entries([_cand("FOO")], _state(), book, threshold=70) == []


def test_entry_blocked_by_breaker():
    assert edam.decide_entries([_cand()], _state(drawdown_tripped=True), PositionBook(),
                               threshold=70) == []


def test_stablecoin_floor_blocks_entry():
    # equity 100 -> floor = max(6, 0.15*100)=15. stable 20 -> 20-10=10 < 15 -> skip.
    assert edam.decide_entries([_cand()], _state(stable=20.0), PositionBook(), threshold=70,
                               order_usd=10, floor_usd=6, floor_pct=0.15) == []
    # stable 30 -> 30-10=20 >= 15 -> allowed.
    assert len(edam.decide_entries([_cand()], _state(stable=30.0), PositionBook(), threshold=70,
                                   order_usd=10, floor_usd=6, floor_pct=0.15)) == 1


# ----------------------------- exits -----------------------------

def _book_with(symbol="FOO", entry=1.0, usd=10.0, peak=None, entry_time=1000.0, baseline_vol=0.0):
    book = PositionBook()
    book.open(OpenPosition(symbol=symbol, contract="0x", entry_price=entry, usd_size=usd,
                           entry_time=entry_time, peak_price=peak or entry,
                           entry_baseline_vol=baseline_vol))
    return book


def test_exit_hard_take_profit_2x():
    book = _book_with()
    orders = edam.decide_exits(book, {"FOO": 2.0}, {}, _state(holdings={"FOO": 20.0}),
                               hard_tp_mult=2.0, now=1000.0)
    assert orders and "hard TP" in orders[0].reason and not book.is_open("FOO")


def test_exit_hard_stop_loss():
    book = _book_with()
    orders = edam.decide_exits(book, {"FOO": 0.90}, {}, _state(holdings={"FOO": 9.0}),
                               hard_stop_pct=0.08, now=1000.0)
    assert orders and "hard stop" in orders[0].reason and not book.is_open("FOO")


def test_exit_max_hold_time():
    book = _book_with(entry_time=0.0)
    orders = edam.decide_exits(book, {"FOO": 1.0}, {}, _state(holdings={"FOO": 10.0}),
                               max_hold_min=300, now=300 * 60)   # exactly 5h old
    assert orders and "max hold" in orders[0].reason and not book.is_open("FOO")


def test_exit_no_progress_flat_position():
    # entered 16 min ago, still ~flat (+1%) => dead trade, cut near breakeven
    book = _book_with(entry_time=0.0)
    orders = edam.decide_exits(book, {"FOO": 1.01}, {}, _state(holdings={"FOO": 10.1}),
                               no_progress_min=15, no_progress_gain=0.02, now=16 * 60)
    assert orders and "no progress" in orders[0].reason and not book.is_open("FOO")


def test_no_progress_not_fired_when_rising():
    # +5% after 16 min => momentum present; loose trail so only no-progress could fire
    book = _book_with(entry_time=0.0, peak=1.05)
    orders = edam.decide_exits(book, {"FOO": 1.05}, {}, _state(holdings={"FOO": 10.5}),
                               no_progress_min=15, no_progress_gain=0.02, trailing_pct=0.5, now=16 * 60)
    assert orders == []


def test_no_progress_suppressed_before_window():
    book = _book_with(entry_time=0.0)
    orders = edam.decide_exits(book, {"FOO": 1.0}, {}, _state(holdings={"FOO": 10.0}),
                               no_progress_min=15, now=5 * 60)   # 5 min < 15
    assert orders == []


def test_exit_volume_death_in_profit():
    # in profit but 5m volume dropped below its baseline (inflow gone) => bank it
    book = _book_with(entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=60.0, baseline_vol=100.0,
                          price_now=1.06, price_5m_ago=1.05)
    orders = edam.decide_exits(book, {"FOO": 1.06}, {"FOO": snap}, _state(holdings={"FOO": 10.6}),
                               volume_death_mult=1.0, min_hold_vol_min=15, trailing_pct=0.5, now=20 * 60)
    assert orders and "volume died" in orders[0].reason and not book.is_open("FOO")


def test_volume_death_not_fired_when_losing():
    # below baseline but DOWN -3% after 20m => no-progress cuts it, not volume-death
    book = _book_with(entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=60.0, baseline_vol=100.0,
                          price_now=0.97, price_5m_ago=0.98)
    orders = edam.decide_exits(book, {"FOO": 0.97}, {"FOO": snap}, _state(holdings={"FOO": 9.7}),
                               no_progress_min=15, volume_death_mult=1.0, min_hold_vol_min=15, now=20 * 60)
    assert orders and "no progress" in orders[0].reason


def test_volume_5x_with_price_stall_triggers_fomo_defense_exit():
    # 5x volume AND price stalling (below 5m-ago) -> FOMO-defense exit.
    book = _book_with(entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=600.0, baseline_vol=100.0,
                          price_now=1.05, price_5m_ago=1.10)
    orders = edam.decide_exits(book, {"FOO": 1.05}, {"FOO": snap}, _state(holdings={"FOO": 10.5}),
                               vol_exit_mult=5.0, min_hold_vol_min=15, now=20 * 60)
    assert orders and "FOMO defense" in orders[0].reason and not book.is_open("FOO")


def test_volume_5x_without_stall_does_not_blind_sell():
    # 5x volume but price still rising -> NOT a blind sell; position held (trailing
    # is merely tightened). Proves we don't dump into a continuing pump.
    book = _book_with(entry=1.0, peak=1.05, entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=600.0, baseline_vol=100.0,
                          price_now=1.05, price_5m_ago=1.0)
    orders = edam.decide_exits(book, {"FOO": 1.05}, {"FOO": snap}, _state(holdings={"FOO": 10.5}),
                               vol_exit_mult=5.0, min_hold_vol_min=15,
                               fomo_trailing_pct=0.015, now=20 * 60)
    assert orders == [] and book.is_open("FOO")


def test_volume_exit_suppressed_before_min_hold():
    book = _book_with(entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=600.0, baseline_vol=100.0,
                          price_now=1.02, price_5m_ago=1.10)
    # only 5 minutes old -> below min hold -> FOMO defense not armed -> no exit
    orders = edam.decide_exits(book, {"FOO": 1.02}, {"FOO": snap}, _state(holdings={"FOO": 10.2}),
                               vol_exit_mult=5.0, min_hold_vol_min=15, now=5 * 60)
    assert orders == [] and book.is_open("FOO")


def test_no_volume_exit_when_source_unavailable():
    # Position had a baseline at entry, but the live volume source is now
    # unavailable (vol_5m=0) -> the 5x volume exit must NOT fire.
    book = _book_with(entry_time=0.0, baseline_vol=100.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=0.0, baseline_vol=0.0, price_now=1.02)
    orders = edam.decide_exits(book, {"FOO": 1.02}, {"FOO": snap}, _state(holdings={"FOO": 10.2}),
                               vol_exit_mult=5.0, min_hold_vol_min=15, trailing_pct=0.99,
                               now=60 * 60)   # 1h old, well past min hold
    assert orders == [] and book.is_open("FOO")


def test_fomo_defense_needs_real_entry_baseline_volume():
    # No entry baseline volume recorded => FOMO defense can't arm even if price
    # stalls (we never invent volume). Position is held.
    book = _book_with(entry=1.0, entry_time=0.0, baseline_vol=0.0)
    snap = MarketSnapshot(symbol="FOO", vol_5m=600, baseline_vol=0.0,
                          price_now=1.02, price_5m_ago=1.10)
    orders = edam.decide_exits(book, {"FOO": 1.02}, {"FOO": snap}, _state(holdings={"FOO": 10.2}),
                               vol_exit_mult=5.0, min_hold_vol_min=15, trailing_pct=0.99, now=20 * 60)
    assert orders == [] and book.is_open("FOO")


def test_exit_trailing_stop():
    book = _book_with(entry=1.0, peak=1.10)
    orders = edam.decide_exits(book, {"FOO": 1.06}, {}, _state(holdings={"FOO": 10.6}),
                               trailing_pct=0.03, now=1000.0)
    assert orders and "trailing" in orders[0].reason and not book.is_open("FOO")


def test_exit_breaker_flattens_all():
    book = _book_with()
    orders = edam.decide_exits(book, {"FOO": 1.0}, {},
                               _state(holdings={"FOO": 10.0}, drawdown_tripped=True), now=1000.0)
    assert orders and "breaker" in orders[0].reason and not book.is_open("FOO")


# ----------------------------- orchestration -----------------------------

def test_orchestration_falls_back_to_basket_when_no_catalyst():
    _, mode = edam.decide(candidates=[], state=_state(), book=PositionBook(),
                          prices={}, snapshots={}, basket_symbols=["A", "B"])
    assert mode == "baseline-basket"


def test_orchestration_uses_event_layer_on_high_confidence():
    book = PositionBook()
    _, mode = edam.decide(candidates=[_cand(score=85)], state=_state(), book=book,
                          prices={"FOO": 1.0}, snapshots={}, basket_symbols=["A"], threshold=70)
    assert mode == "aegis-event"
    assert book.is_open("FOO")


def test_orchestration_breaker_derisks_and_clears_book():
    book = _book_with()
    _, mode = edam.decide(candidates=[], state=_state(holdings={"FOO": 10.0}, drawdown_tripped=True),
                          book=book, prices={"FOO": 1.0}, snapshots={}, basket_symbols=["A"])
    assert mode == "breaker-derisk" and not book.positions
