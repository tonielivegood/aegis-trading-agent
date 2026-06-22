"""TDD for the beta core — the regime-gated momentum-major basket (barbell Phase-2).

Pure decision logic: state/prices/momentum/book/regime injected, `allow` injected to
avoid touching the token registry. No network/chain."""
from __future__ import annotations

from src.agent.aegis import beta_core as bc
from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.aegis.regime import Regime
from src.agent.strategy.base_strategy import PortfolioState

ALLOW = lambda s: True  # noqa: E731 — every symbol tradable in these unit tests


def _state(*, equity=100.0, stable=100.0, holdings=None, tripped=False, cap=False):
    return PortfolioState(
        equity_usd=equity, risk_value_usd=equity - stable, stable_value_usd=stable,
        token_values_usd=holdings or {}, drawdown_tripped=tripped, cap_breached=cap)


def _book(*positions):
    b = PositionBook()
    for p in positions:
        b.open(p)
    return b


def _major(sym, entry, *, peak=None, usd=20.0, entry_time=None):
    kw = {} if entry_time is None else {"entry_time": entry_time}
    return OpenPosition(symbol=sym, contract="0x", entry_price=entry, usd_size=usd,
                        peak_price=peak or entry, token_class="major", **kw)


# --- momentum + selection ---

def test_momentum_score_blends_1h_and_24h():
    q = {"percent_change_1h": 2.0, "percent_change_24h": 10.0}
    assert bc.momentum_score(q, w_1h=0.5, w_24h=1.0) == 11.0


def test_select_basket_ranks_and_caps():
    mom = {"AAA": 12.0, "BBB": 8.0, "CCC": 3.0, "DDD": -1.0}
    out = bc.select_basket(mom, max_names=2, min_momentum=2.0, allow=ALLOW)
    assert out == ["AAA", "BBB"]


def test_select_basket_filters_weak_and_negative():
    mom = {"AAA": 1.0, "BBB": -5.0}
    assert bc.select_basket(mom, max_names=3, min_momentum=2.0, allow=ALLOW) == []


# --- entries ---

def test_risk_on_enters_top_names_into_empty_slots():
    mom = {"AAA": 12.0, "BBB": 8.0, "CCC": 3.0}
    book = _book()
    prices = {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0}
    orders, mode = bc.decide_beta(_state(), prices, mom, book=book, regime_flag=Regime.RISK_ON,
                                  now=0.0, max_names=2, position_usd=20.0, floor_usd=6.0,
                                  min_momentum=2.0, allow=ALLOW)
    bought = {o.token_out for o in orders if o.token_in == "USDT"}
    assert bought == {"AAA", "BBB"} and mode == "beta"
    assert book.is_open("AAA") and book.is_open("BBB") and not book.is_open("CCC")


def test_cautious_deploys_light_basket():
    # Graduated: CAUTIOUS still deploys, but the caller passes a SMALL max_names (light).
    mom = {"AAA": 12.0, "BBB": 8.0, "CCC": 3.0}
    book = _book()
    prices = {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0}
    orders, _ = bc.decide_beta(_state(), prices, mom, book=book, regime_flag=Regime.CAUTIOUS,
                               now=0.0, max_names=1, position_usd=20.0, floor_usd=6.0, allow=ALLOW)
    bought = {o.token_out for o in orders if o.token_in == "USDT"}
    assert bought == {"AAA"}                 # only the single strongest leader
    assert book.is_open("AAA") and not book.is_open("BBB")


def test_entry_respects_stable_floor():
    mom = {"AAA": 12.0}
    # stable just above floor: a 20.0 buy would drop below the 6.0 floor → blocked.
    orders, _ = bc.decide_beta(_state(stable=20.0), {"AAA": 1.0}, mom, book=_book(),
                               regime_flag=Regime.RISK_ON, now=0.0, max_names=2,
                               position_usd=20.0, floor_usd=6.0, allow=ALLOW)
    assert orders == []


# --- exits ---

def test_risk_off_flattens_basket():
    book = _book(_major("AAA", 1.0), _major("BBB", 2.0))
    orders, mode = bc.decide_beta(_state(holdings={"AAA": 20.0, "BBB": 20.0}),
                                  {"AAA": 1.0, "BBB": 2.0}, {}, book=book,
                                  regime_flag=Regime.RISK_OFF, now=0.0, max_names=3,
                                  position_usd=20.0, floor_usd=6.0, allow=ALLOW)
    assert mode == "beta-flat" and len(orders) == 2 and not book.positions
    assert all(o.token_out == "USDT" for o in orders)


def test_breaker_flattens_basket():
    book = _book(_major("AAA", 1.0))
    orders, mode = bc.decide_beta(_state(holdings={"AAA": 20.0}, tripped=True),
                                  {"AAA": 1.0}, {"AAA": 12.0}, book=book,
                                  regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                                  position_usd=20.0, floor_usd=6.0, allow=ALLOW)
    assert mode == "beta-flat" and "breaker" in orders[0].reason and not book.positions


def test_hard_stop_exit():
    book = _book(_major("AAA", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"AAA": 18.0}), {"AAA": 0.88}, {"AAA": 5.0},
                               book=book, regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                               position_usd=20.0, floor_usd=6.0, hard_stop_pct=0.10, allow=ALLOW)
    assert any("hard stop" in o.reason for o in orders) and not book.is_open("AAA")


def test_breakeven_exit_after_pop():
    book = _book(_major("AAA", 1.0, peak=1.08))   # ran +8%
    orders, _ = bc.decide_beta(_state(holdings={"AAA": 20.0}), {"AAA": 1.0}, {"AAA": 12.0},
                               book=book, regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                               position_usd=20.0, floor_usd=6.0,
                               breakeven_trigger=0.05, breakeven_buffer=0.005, allow=ALLOW)
    assert any("breakeven" in o.reason for o in orders) and not book.is_open("AAA")


def test_trailing_exit():
    book = _book(_major("AAA", 1.0, peak=1.20))
    orders, _ = bc.decide_beta(_state(holdings={"AAA": 21.0}), {"AAA": 1.05}, {"AAA": 12.0},
                               book=book, regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                               position_usd=20.0, floor_usd=6.0, trail_pct=0.12, allow=ALLOW)
    assert any("trailing" in o.reason for o in orders) and not book.is_open("AAA")


def test_block_entries_suppresses_new_without_flattening():
    # Daily soft breaker: block_entries=True opens nothing new but does NOT flatten holds.
    mom = {"AAA": 12.0, "BBB": 10.0}
    book = _book(_major("AAA", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"AAA": 20.0}), {"AAA": 1.0}, mom, book=book,
                               regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                               position_usd=20.0, floor_usd=6.0, block_entries=True, allow=ALLOW)
    assert orders == [] and book.is_open("AAA")          # held, not flattened
    book2 = _book()
    orders2, _ = bc.decide_beta(_state(), {"AAA": 1.0, "BBB": 2.0}, mom, book=book2,
                                regime_flag=Regime.RISK_ON, now=0.0, max_names=3,
                                position_usd=20.0, floor_usd=6.0, block_entries=True, allow=ALLOW)
    assert orders2 == [] and not book2.positions         # no new entries


def test_momentum_lost_exit():
    # Held name's own momentum collapsed below the exit floor → rotate out (weak).
    mom = {"AAA": 12.0, "BBB": 10.0, "CCC": 8.0, "OLD": -2.0}
    book = _book(_major("OLD", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"OLD": 20.0}), {"OLD": 1.0}, mom, book=book,
                               regime_flag=Regime.RISK_ON, now=0.0, max_names=2,
                               position_usd=20.0, floor_usd=6.0, allow=ALLOW)
    assert any("momentum lost" in o.reason for o in orders) and not book.is_open("OLD")


# --- hysteresis: don't churn a held name on a MARGINAL momentum slip ---

def test_marginal_challenger_does_not_churn_incumbent():
    # Reproduces the live HOME→BAT whipsaw: incumbent at 3.4%, challenger at 4.8% (only
    # +1.4pp). With rotation_margin=3 the challenger must beat the held name by >=3pp to
    # displace it → incumbent KEEPS its slot, no churn.
    mom = {"HELD": 3.4, "CHAL": 4.8}
    book = _book(_major("HELD", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"HELD": 20.0}), {"HELD": 1.0, "CHAL": 2.0},
                               mom, book=book, regime_flag=Regime.RISK_ON, now=0.0, max_names=1,
                               position_usd=20.0, floor_usd=6.0, min_momentum=4.0,
                               exit_min_momentum=2.0, rotation_margin=3.0, allow=ALLOW)
    assert orders == [] and book.is_open("HELD")          # held, not rotated


def test_strong_challenger_rotates_incumbent():
    # A CLEARLY stronger leader (12.3pp gap) DOES displace the incumbent → rotate.
    mom = {"HELD": 3.7, "CHAL": 16.0}
    book = _book(_major("HELD", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"HELD": 20.0}), {"HELD": 1.0, "CHAL": 2.0},
                               mom, book=book, regime_flag=Regime.RISK_ON, now=0.0, max_names=1,
                               position_usd=20.0, floor_usd=6.0, min_momentum=4.0,
                               exit_min_momentum=2.0, rotation_margin=3.0, allow=ALLOW)
    assert any("momentum lost" in o.reason and o.token_in == "HELD" for o in orders)
    assert any(o.token_out == "CHAL" for o in orders) and not book.is_open("HELD")


def test_weak_incumbent_below_exit_floor_rotates_to_cash():
    # No challenger, but the held name's own momentum fell below the exit floor → sell.
    mom = {"HELD": 1.0}
    book = _book(_major("HELD", 1.0))
    orders, _ = bc.decide_beta(_state(holdings={"HELD": 20.0}), {"HELD": 1.0}, mom, book=book,
                               regime_flag=Regime.RISK_ON, now=0.0, max_names=1,
                               position_usd=20.0, floor_usd=6.0, min_momentum=4.0,
                               exit_min_momentum=2.0, rotation_margin=3.0, allow=ALLOW)
    assert any("momentum lost" in o.reason for o in orders) and not book.is_open("HELD")


def test_min_hold_blocks_fresh_rotation_then_allows():
    # A fresh entry (age < min_hold) is NOT rotated even by a strong challenger; once it
    # has aged past min_hold, the same challenger displaces it.
    mom = {"HELD": 3.7, "CHAL": 16.0}
    prices = {"HELD": 1.0, "CHAL": 2.0}
    fresh = _book(_major("HELD", 1.0, entry_time=1000.0))
    orders, _ = bc.decide_beta(_state(holdings={"HELD": 20.0}), prices, mom, book=fresh,
                               regime_flag=Regime.RISK_ON, now=1000.0 + 600, max_names=1,
                               position_usd=20.0, floor_usd=6.0, min_momentum=4.0,
                               exit_min_momentum=2.0, rotation_margin=3.0, min_hold_sec=1800.0,
                               allow=ALLOW)
    assert orders == [] and fresh.is_open("HELD")         # too fresh to rotate

    aged = _book(_major("HELD", 1.0, entry_time=1000.0))
    orders2, _ = bc.decide_beta(_state(holdings={"HELD": 20.0}), prices, mom, book=aged,
                                regime_flag=Regime.RISK_ON, now=1000.0 + 2000, max_names=1,
                                position_usd=20.0, floor_usd=6.0, min_momentum=4.0,
                                exit_min_momentum=2.0, rotation_margin=3.0, min_hold_sec=1800.0,
                                allow=ALLOW)
    assert any("momentum lost" in o.reason for o in orders2) and not aged.is_open("HELD")


def test_min_hold_never_blocks_hard_stop():
    # Risk exits ALWAYS fire regardless of min_hold — a fresh position that craters still stops out.
    book = _book(_major("HELD", 1.0, entry_time=1000.0))
    orders, _ = bc.decide_beta(_state(holdings={"HELD": 18.0}), {"HELD": 0.88}, {"HELD": 5.0},
                               book=book, regime_flag=Regime.RISK_ON, now=1000.0 + 60, max_names=1,
                               position_usd=20.0, floor_usd=6.0, hard_stop_pct=0.10,
                               min_hold_sec=1800.0, allow=ALLOW)
    assert any("hard stop" in o.reason for o in orders) and not book.is_open("HELD")
