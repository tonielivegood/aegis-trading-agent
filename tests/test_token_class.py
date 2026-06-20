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


def test_params_two_tier_active_major_vs_ride_meme():
    maj, meme = tc.params("major"), tc.params("meme")
    # MAJOR = active/modest: small TP, tight trail, fast recycle.
    assert maj.hard_tp_mult == 1.10 and maj.trailing_pct == 0.05 and maj.hard_stop_pct == 0.05
    # MEME = rare/ride: +100% cap, wide trail, wider stop, more patience.
    assert meme.hard_tp_mult == 2.0 and meme.trailing_pct == 0.15 and meme.hard_stop_pct == 0.08
    assert maj.no_progress_min < meme.no_progress_min          # majors recycle faster
    assert tc.params("unknown").hard_tp_mult == 2.0            # unknown → meme default


# --- exits diverge by tier: MAJOR harvests modest, MEME rides big ---

def test_major_takes_modest_profit_while_meme_rides():
    # +11%: MAJOR hits its +10% take-profit; MEME keeps riding toward +200%.
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 1.11}, {},
                            _state({"FOO": 6.66}), class_aware=True, now=60)
    assert maj and "hard TP" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 1.11}, {},
                             _state({"FOO": 6.66}), class_aware=True, now=60)
    assert meme == []


def test_major_stop_tighter_than_meme():
    # −6%: MAJOR cut (−5% stop); MEME holds (−8% stop) to give the big move room.
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 0.94}, {},
                            _state({"FOO": 5.64}), class_aware=True, now=60)
    assert maj and "hard stop" in maj[0].reason
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 0.94}, {},
                             _state({"FOO": 5.64}), class_aware=True, now=60)
    assert meme == []


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
