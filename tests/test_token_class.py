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


def test_params_per_class():
    assert tc.params("major").hard_tp_mult == 1.04 and tc.params("major").hard_stop_pct == 0.035
    assert tc.params("meme").hard_tp_mult == 3.0 and tc.params("meme").trailing_pct == 0.15
    assert tc.params("unknown").hard_tp_mult == 3.0          # unknown → meme default


# --- exits diverge by class ---

def test_major_scalps_at_plus_4pct_but_meme_rides():
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 1.05}, {},
                            _state({"FOO": 6.3}), class_aware=True, now=60)
    assert maj and "hard TP" in maj[0].reason            # +5% ≥ major +4% → scalp out
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 1.05}, {},
                             _state({"FOO": 6.3}), class_aware=True, now=60)
    assert meme == []                                    # +5% ≪ meme +200% → keep riding


def test_major_stops_tighter_than_meme():
    maj = edam.decide_exits(_book("FOO", 1.0, "major"), {"FOO": 0.96}, {},
                            _state({"FOO": 5.76}), class_aware=True, now=60)
    assert maj and "hard stop" in maj[0].reason          # −4% ≥ major −3.5% → cut
    meme = edam.decide_exits(_book("FOO", 1.0, "meme"), {"FOO": 0.96}, {},
                             _state({"FOO": 5.76}), class_aware=True, now=60)
    assert meme == []                                    # −4% within meme −8% → hold


# --- entry thresholds diverge by class ---

def _snap(sym, vol_5m, baseline, now_p, ago_p):
    return MarketSnapshot(symbol=sym, contract="0x" + sym, vol_5m=vol_5m, baseline_vol=baseline,
                          price_now=now_p, price_5m_ago=ago_p, has_route=True, liquidity_ok=True)


def test_major_entry_looser_than_meme():
    # a 2x-volume, +1% move: a MAJOR breakout, but NOT a meme one (needs 3x)
    snaps = {"M": _snap("M", vol_5m=220, baseline=100, now_p=1.01, ago_p=1.0)}
    mp, ep = tc.params("major"), tc.params("meme")
    assert len(scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                              breakout_max=mp.breakout_max)) == 1
    assert scan_breakouts(snaps, vol_mult=ep.vol_mult, breakout_min=ep.breakout_min,
                          breakout_max=ep.breakout_max) == []


def test_major_requires_minimum_rise():
    # 2x volume but essentially flat (+0.1%) → below major's +0.3% floor → no entry
    snaps = {"M": _snap("M", vol_5m=220, baseline=100, now_p=1.001, ago_p=1.0)}
    mp = tc.params("major")
    assert scan_breakouts(snaps, vol_mult=mp.vol_mult, breakout_min=mp.breakout_min,
                          breakout_max=mp.breakout_max) == []
