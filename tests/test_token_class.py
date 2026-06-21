"""TDD: two-tier CONFIRMED-MOMENTUM + RIDE design (redesign 21/6).

Both tiers ride (no time exit); MEME is primary (lower bar, bigger ride), MAJOR is
very rare (higher bar). Exit is TP / hard-stop / trailing only.
"""
from src.agent.aegis import token_class as tc
from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.aegis.volume_breakout import scan_breakouts
from src.agent.strategy import event_driven_alpha_momentum as edam
from src.agent.strategy.base_strategy import PortfolioState


def _state(holdings):
    return PortfolioState(equity_usd=30, risk_value_usd=sum(holdings.values()),
                          stable_value_usd=10, token_values_usd=holdings)


def _book(symbol, entry, cls):
    b = PositionBook()
    b.open(OpenPosition(symbol=symbol, contract="0x", entry_price=entry, usd_size=6.0,
                        entry_time=0.0, token_class=cls))
    return b


def test_params_both_ride_no_time_exit():
    maj, meme = tc.params("major"), tc.params("meme")
    # No time-based exit on either tier — rides exit on TP/stop/trail only.
    assert maj.no_progress_min == 0 and meme.no_progress_min == 0
    # MEME = primary asymmetric ride: bigger cap, wider trail + stop than the rare major.
    assert meme.hard_tp_mult == 3.0 and meme.trailing_pct == 0.25 and meme.hard_stop_pct == 0.12
    assert maj.hard_tp_mult == 2.0 and maj.trailing_pct == 0.10 and maj.hard_stop_pct == 0.07
    # MAJOR is RARER than meme: a strictly higher volume bar.
    assert maj.vol_mult > meme.vol_mult
    assert tc.params("unknown").hard_tp_mult == 3.0            # unknown → meme default


# --- exits: both RIDE; cap is far, stop diverges, NO time exit ---

def test_both_ride_through_a_small_gain():
    # +11% after 60 min: neither tier exits. 60 min is well past the OLD 20/25-min
    # no-progress timer — proving the time-based exit is gone; the ride continues.
    for cls in ("major", "meme"):
        out = edam.decide_exits(_book("FOO", 1.0, cls), {"FOO": 1.11}, {},
                                _state({"FOO": 6.66}), class_aware=True, now=60 * 60)
        assert out == []          # no TP yet, no stop, and crucially NO time exit


def test_take_profit_caps_diverge():
    # +120%: MAJOR hits its +100% cap; MEME still rides toward +200%.
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 2.2}, {},
                            _state({"FOO": 13.2}), class_aware=True, now=60)
    assert maj and "hard TP" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 2.2}, {},
                             _state({"FOO": 13.2}), class_aware=True, now=60)
    assert meme == []


def test_major_stop_tighter_than_meme():
    # −8%: MAJOR cut (−7% stop); MEME holds (−12% stop) to give the ride room.
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 0.92}, {},
                            _state({"FOO": 5.52}), class_aware=True, now=60)
    assert maj and "hard stop" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 0.92}, {},
                             _state({"FOO": 5.52}), class_aware=True, now=60)
    assert meme == []


# --- entry: BOTH require a CONFIRMED move (>=3%); MEME bar lower, MAJOR rarer ---

def _snap(sym, vol_5m, baseline, now_p, ago_p):
    return MarketSnapshot(symbol=sym, contract="0x" + sym, vol_5m=vol_5m, baseline_vol=baseline,
                          price_now=now_p, price_5m_ago=ago_p, has_route=True, liquidity_ok=True)


def test_meme_enters_easier_than_major():
    # a 4.5x-volume, +5% confirmed move: a MEME breakout (>=4x), but NOT a major one (needs 5x).
    snaps = {"M": _snap("M", vol_5m=450, baseline=100, now_p=1.05, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(snaps, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                              breakout_max=ep.breakout_max)) == 1
    assert scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                          breakout_max=mp.breakout_max) == []


def test_unconfirmed_move_rejected():
    # 6x volume but only +1% (below the +3% confirmation floor) → noise, no entry on either tier.
    snaps = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.01, ago_p=1.0)}
    for p in (tc.params("major"), tc.params("meme")):
        assert scan_breakouts(snaps, vol_mult=p.vol_mult, breakout_min=p.breakout_min,
                              breakout_max=p.breakout_max) == []


def test_confirmed_move_enters_but_blowoff_rejected():
    # +8% confirmed move on 6x vol → ENTERS (within both caps now).
    ok = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.08, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(ok, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert len(scan_breakouts(ok, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                              breakout_max=ep.breakout_max)) == 1
    # +25% is already blown off past both caps (major 15%, meme 20%) → no entry.
    blown = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.25, ago_p=1.0)}
    for p in (mp, ep):
        assert scan_breakouts(blown, vol_mult=p.vol_mult, breakout_min=p.breakout_min,
                              breakout_max=p.breakout_max) == []
