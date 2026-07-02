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
    # MEME re-tune (1/7): +40% cap, 6% trail, −6% stop — bank consistent hits instead
    # of chasing a rare +80% moonshot (see token_class.py comment).
    assert meme.hard_tp_mult == 1.40 and meme.trailing_pct == 0.06 and meme.hard_stop_pct == 0.06
    # MAJOR retuned (2/7, user call, alongside disabling beta_core): +20% cap, 5% trail,
    # −5% stop — tighter risk per name now that majors enter on volume+price like memes.
    assert maj.hard_tp_mult == 1.20 and maj.trailing_pct == 0.05 and maj.hard_stop_pct == 0.05
    # MAJOR fires EASIER than meme now (lower bar): cheaper to trade, catch major days.
    assert maj.vol_mult < meme.vol_mult
    # MAJOR confirms on +3%; MEME demands a stronger +5% ignition (retuned 2/7, narrowed
    # from +6%) — still a higher floor than major, fewer/higher-conviction meme tickets.
    assert maj.breakout_min == 0.03 and meme.breakout_min == 0.05
    assert tc.params("unknown").hard_tp_mult == 1.40           # unknown → meme default


# --- exits: both RIDE; cap is far, stop diverges, NO time exit ---

def test_both_ride_through_a_small_gain():
    # +11% after 60 min: neither tier exits. 60 min is well past the OLD 20/25-min
    # no-progress timer — proving the time-based exit is gone; the ride continues.
    for cls in ("major", "meme"):
        out = edam.decide_exits(_book("FOO", 1.0, cls), {"FOO": 1.11}, {},
                                _state({"FOO": 6.66}), class_aware=True, now=60 * 60)
        assert out == []          # no TP yet, no stop, and crucially NO time exit


def test_take_profit_caps_diverge():
    # +35%: MAJOR already past its +30% cap; MEME's new +40% cap not reached yet.
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 1.35}, {},
                            _state({"FOO": 8.1}), class_aware=True, now=60)
    assert maj and "hard TP" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 1.35}, {},
                             _state({"FOO": 8.1}), class_aware=True, now=60)
    assert meme == []


def test_major_stop_tighter_than_meme_post_rewire():
    # Re-tune (2/7, alongside disabling beta_core): MAJOR's −5% stop is now TIGHTER
    # than MEME's −6% — majors are the more disciplined, "confirmed move" tier now.
    # −5.5%: MAJOR cuts (past its −5% stop); MEME holds (within its −6% stop).
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 0.945}, {},
                            _state({"FOO": 5.67}), class_aware=True, now=60)
    assert maj and "hard stop" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 0.945}, {},
                             _state({"FOO": 5.67}), class_aware=True, now=60)
    assert meme == []


# --- entry: MAJOR confirms on >=3%; MEME needs a stronger >=6% (anti-noise) ---


def test_meme_rejects_marginal_move_major_accepts():
    # A 5x-volume, +4% move: clears the MAJOR floor (+3%) but NOT the raised MEME floor (+6%).
    # This is the anti-noise change — a +4% wiggle on volume no longer buys a meme lottery slot.
    snaps = {"M": _snap("M", vol_5m=500, baseline=100, now_p=1.04, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert scan_breakouts(snaps, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                          breakout_max=ep.breakout_max) == []

def _snap(sym, vol_5m, baseline, now_p, ago_p):
    return MarketSnapshot(symbol=sym, contract="0x" + sym, vol_5m=vol_5m, baseline_vol=baseline,
                          price_now=now_p, price_5m_ago=ago_p, has_route=True, liquidity_ok=True)


def test_major_enters_easier_than_meme():
    # a 3x-volume, +5% confirmed move: a MAJOR breakout (>=2x), but NOT a meme one (needs 4x).
    snaps = {"M": _snap("M", vol_5m=300, baseline=100, now_p=1.05, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert scan_breakouts(snaps, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                          breakout_max=ep.breakout_max) == []


def test_unconfirmed_move_rejected():
    # 6x volume but only +1% (below the +3% confirmation floor) → noise, no entry on either tier.
    snaps = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.01, ago_p=1.0)}
    for p in (tc.params("major"), tc.params("meme")):
        assert scan_breakouts(snaps, vol_mult=p.vol_mult, breakout_min=p.breakout_min,
                              breakout_max=p.breakout_max) == []


def test_confirmed_move_enters_but_blowoff_rejected():
    # +7% confirmed move on 6x vol → ENTERS on both tiers (major cap 8%, meme cap 10%).
    ok = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.07, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(ok, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert len(scan_breakouts(ok, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                              breakout_max=ep.breakout_max)) == 1
    # +25% is already blown off past both caps (major 8%, meme 10%) → no entry.
    blown = {"M": _snap("M", vol_5m=600, baseline=100, now_p=1.25, ago_p=1.0)}
    for p in (mp, ep):
        assert scan_breakouts(blown, vol_mult=p.vol_mult, breakout_min=p.breakout_min,
                              breakout_max=p.breakout_max) == []
