"""TDD: two-class (major scalp vs meme ride) entry/exit divergence."""
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


def test_params_unified_asymmetric_ride_exit():
    # Both classes now RIDE with the SAME asymmetric exit (no more major scalp).
    for cls in ("major", "meme"):
        p = tc.params(cls)
        assert p.hard_tp_mult == 3.0          # +200% cap — let winners run
        assert p.trailing_pct == 0.15         # wide trail
        assert p.hard_stop_pct == 0.07        # cut losers fast at −7%
        assert p.no_progress_min == 25        # patience for a wave to form
    assert tc.params("unknown").hard_tp_mult == 3.0          # unknown → meme default


# --- exits are now UNIFIED (both ride): cut losers fast, let winners run ---

def test_both_classes_ride_small_gains():
    # +5% is a winner-in-progress for BOTH — neither scalps out (we ride).
    for cls in ("major", "meme"):
        out = edam.decide_exits(_book("FOO", 1.0, cls), {"FOO": 1.05}, {},
                                _state({"FOO": 6.3}), class_aware=True, now=60)
        assert out == [], f"{cls} should keep riding +5%"


def test_both_classes_hold_small_dip_but_cut_at_7pct():
    # −5% is within the −7% stop for BOTH → hold; −8% cuts BOTH.
    for cls in ("major", "meme"):
        hold = edam.decide_exits(_book("FOO", 1.0, cls), {"FOO": 0.95}, {},
                                 _state({"FOO": 5.7}), class_aware=True, now=60)
        assert hold == [], f"{cls} should hold a −5% dip"
        cut = edam.decide_exits(_book("FOO", 1.0, cls), {"FOO": 0.92}, {},
                                _state({"FOO": 5.52}), class_aware=True, now=60)
        assert cut and "hard stop" in cut[0].reason, f"{cls} should cut at −8%"


# --- entry thresholds still diverge by class (two-speed entry) ---

def _snap(sym, vol_5m, baseline, now_p, ago_p):
    return MarketSnapshot(symbol=sym, contract="0x" + sym, vol_5m=vol_5m, baseline_vol=baseline,
                          price_now=now_p, price_5m_ago=ago_p, has_route=True, liquidity_ok=True)


def test_major_entry_looser_than_meme():
    # a 2.2x-volume, +1% move: a MAJOR breakout (≥2x), but NOT a meme one (needs 3x)
    snaps = {"M": _snap("M", vol_5m=220, baseline=100, now_p=1.01, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert scan_breakouts(snaps, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                          breakout_max=ep.breakout_max) == []


def test_major_requires_minimum_rise():
    # 2.7x volume but essentially flat (+0.1%) → below major's +0.3% floor → no entry
    snaps = {"M": _snap("M", vol_5m=270, baseline=100, now_p=1.001, ago_p=1.0)}
    mp = tc.params("major")
    assert scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                          breakout_max=mp.breakout_max) == []


def test_entry_catches_early_not_chasing():
    # a +8% move is already spent for BOTH classes (major cap +5%, meme +6%) → no entry
    snaps = {"M": _snap("M", vol_5m=400, baseline=100, now_p=1.08, ago_p=1.0)}
    for p in (tc.params("major"), tc.params("meme")):
        assert scan_breakouts(snaps, vol_mult=p.vol_mult, breakout_min=p.breakout_min,
                              breakout_max=p.breakout_max) == []
