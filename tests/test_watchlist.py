"""Dossier store for the phase-2 stakeout. Critical behaviors: cap on concurrent
dossiers, armer-sold disarm, 6h expiry, every event persisted as one JSONL line."""
import json

from src.agent.copy_trade.watchlist import Dossier, Watchlist, phase2_score

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


# ---------- phase2_score ----------

def _film(n=20, price=1.0, holders_start=100, holders_end=120, liq=51_000.0,
          top_pct=0.03, top5_pct=0.1):
    """Plain-green film: n samples, mild-to-flat price, holders growing,
    healthy liquidity/concentration throughout."""
    out = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 0
        holders = round(holders_start + (holders_end - holders_start) * frac)
        out.append({"ts": float(i), "price": price, "liq": liq,
                    "buys_h1": 1, "sells_h1": 1, "buys_m5": 1, "sells_m5": 0,
                    "chg_m5": 1.0, "holders": holders,
                    "top_pct": top_pct, "top5_pct": top5_pct})
    return out


def _dossier(samples, armers=(W1, W2), arm_price=1.0, arm_liquidity=51_000.0):
    return Dossier(token_address=T1, armed_at=0.0, arm_price=arm_price,
                   arm_liquidity=arm_liquidity, armers=list(armers), samples=samples)


def test_phase2_score_fully_green_passes(tmp_path):
    d = _dossier(_film())
    assert phase2_score(d, {}, voting={W1, W2}) == (True, "")


def test_phase2_score_needs_two_voting_armers_even_with_perfect_film():
    d = _dossier(_film(), armers=[W1])
    assert phase2_score(d, {}, voting={W1}) == (False, "need_2_voting_armers")


def test_phase2_score_min_voting_armers_configurable_to_one():
    d = _dossier(_film(), armers=[W1])
    assert phase2_score(d, {"phase2_min_voting_armers": 1}, voting={W1}) == (True, "")


def test_phase2_score_film_too_short():
    d = _dossier(_film(n=10))
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "film_too_short")


def test_phase2_score_chasing_regression_18_7_shape():
    # price already 1.5x arm_price for the whole (short) film we have on hand —
    # internally flat so base/holders/liq/concentration all read healthy, but
    # it's still 50% above where the signal armed: the reflex-buy shape that
    # cost -$6.15 on 18/7.
    d = _dossier(_film(price=1.5), arm_price=1.0)
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "chasing")


def test_phase2_score_holders_flat():
    d = _dossier(_film(holders_start=100, holders_end=100))
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "holders_flat")


def test_phase2_score_holders_unknown_when_all_none():
    samples = _film()
    for s in samples:
        s["holders"] = None
    d = _dossier(samples)
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "holders_unknown")


def test_phase2_score_whale_risk():
    d = _dossier(_film(top_pct=0.20))
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "whale_risk")


def test_phase2_score_no_base_price_spike():
    # armers(2 voting)/samples(20>=15) pass untouched; one spike in an
    # otherwise-flat film pushes max/min to 2.0, over the 1.35 default ratio,
    # firing check 3 before holders/liq/conc/chasing are ever looked at.
    samples = _film()
    samples[-1]["price"] = 2.0
    d = _dossier(samples)
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "no_base")


def test_phase2_score_liq_draining():
    # base ratio stays 1.0 (flat price) and holders still grow 100->120, so
    # checks 3-4 pass; only the last sample's liq drops below 0.9x the
    # 51_000 arm_liquidity (45_900), firing check 5 ahead of concentration.
    samples = _film()
    samples[-1]["liq"] = 40_000.0
    d = _dossier(samples)
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "liq_draining")


def test_phase2_score_holders_unknown_via_concentration_check():
    # holders are present and growing (100->120), so check 4 passes and we
    # reach check 6 — a genuinely different fixture from
    # test_phase2_score_holders_unknown_when_all_none, which sets holders=None
    # and never gets past check 4. Here top_pct/top5_pct are all None instead,
    # so `conc` stays None and check 6 fires its own fail-closed branch.
    samples = _film()
    for s in samples:
        s["top_pct"] = None
        s["top5_pct"] = None
    d = _dossier(samples)
    assert phase2_score(d, {}, voting={W1, W2}) == (False, "holders_unknown")
