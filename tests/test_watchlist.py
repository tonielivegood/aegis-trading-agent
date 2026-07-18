"""Dossier store for the phase-2 stakeout. Critical behaviors: cap on concurrent
dossiers, armer-sold disarm, 6h expiry, every event persisted as one JSONL line."""
import json

from src.agent.copy_trade.watchlist import Watchlist

T1, T2 = "0x" + "a" * 40, "0x" + "b" * 40
W1, W2 = "0x" + "1" * 40, "0x" + "2" * 40


def _wl(tmp_path, **kw):
    return Watchlist(films_path=tmp_path / "films.jsonl", **kw)


def _lines(tmp_path):
    return [json.loads(l) for l in (tmp_path / "films.jsonl").read_text().splitlines()]


def test_arm_note_buy_sample_lifecycle(tmp_path):
    wl = _wl(tmp_path)
    assert wl.arm(T1, W1, price=1.0, liquidity=50_000, now=1000.0) is True
    assert wl.arm(T1, W2, price=1.1, liquidity=50_000, now=1010.0) is False  # already armed
    wl.note_buy(T1, W2)                                   # second wallet joins armers
    d = wl.get(T1)
    assert d.armers == [W1, W2] and d.arm_price == 1.0
    wl.add_sample(T1, {"price": 1.05, "liq": 51_000})
    assert len(d.samples) == 1
    events = [l["event"] for l in _lines(tmp_path)]
    assert events == ["arm", "sample"]


def test_armer_sell_disarms_and_persists_reason(tmp_path):
    wl = _wl(tmp_path)
    wl.arm(T1, W1, price=1.0, liquidity=1, now=0.0)
    wl.note_sell(T1, W2, now=5.0)                         # non-armer: no effect
    assert wl.get(T1).disarmed is None
    wl.note_sell(T1, W1, now=9.0)                         # armer sold -> signal dead
    assert wl.get(T1) is None                             # no longer active
    assert _lines(tmp_path)[-1] == {"event": "disarm", "token_address": T1,
                                    "reason": "armer_sold", "ts": 9.0}


def test_cap_and_expiry(tmp_path):
    wl = _wl(tmp_path, max_dossiers=1, max_age_s=100)
    assert wl.arm(T1, W1, price=1, liquidity=1, now=0.0) is True
    assert wl.arm(T2, W1, price=1, liquidity=1, now=1.0) is False   # cap reached
    wl.expire(now=101.0)
    assert wl.get(T1) is None
    assert _lines(tmp_path)[-1]["reason"] == "expired"
    assert wl.arm(T2, W1, price=1, liquidity=1, now=102.0) is True  # slot freed
