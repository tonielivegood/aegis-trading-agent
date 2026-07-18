"""Pure-function tests for the film report analyzer. Network calls stay untested
(thin wrapper reused from gem_report, None on failure); the grouping/fingerprint
math is what must not lie."""
import time

from scripts.film_report import film_fingerprints, load_films

TOK_A, TOK_B = "0x" + "a" * 40, "0x" + "b" * 40
WALLET = "0x" + "1" * 40


def _arm(token, ts, price=1.0, liq=10000.0):
    return {"event": "arm", "token_address": token, "ts": ts, "wallet": WALLET,
            "price": price, "liquidity": liq}


def _sample(token, ts, price=1.0, liq=10000.0, holders=None, top_pct=None):
    return {"event": "sample", "token_address": token, "ts": ts, "price": price,
            "liq": liq, "buys_h1": 1, "sells_h1": 0, "buys_m5": 1, "sells_m5": 0,
            "chg_m5": None, "holders": holders, "top_pct": top_pct, "top5_pct": None}


def _disarm(token, ts, reason="expired"):
    return {"event": "disarm", "token_address": token, "reason": reason, "ts": ts}


def test_load_films_groups_rearmed_token_into_two_films():
    now = time.time()
    rows = [
        _arm(TOK_A, now),
        _sample(TOK_A, now + 60),
        _disarm(TOK_A, now + 120, reason="armer_sold"),
        _arm(TOK_A, now + 200),
        _sample(TOK_A, now + 260),
        _disarm(TOK_A, now + 320, reason="expired"),
    ]
    films = load_films(rows)
    assert len(films[TOK_A]) == 2
    assert films[TOK_A][0]["disarmed"] == "armer_sold"
    assert films[TOK_A][1]["disarmed"] == "expired"
    assert films[TOK_A][0]["armed_at"] == now
    assert films[TOK_A][1]["armed_at"] == now + 200
    assert len(films[TOK_A][0]["samples"]) == 1
    assert len(films[TOK_A][1]["samples"]) == 1


def test_load_films_leaves_undisarmed_film_open_and_included():
    now = time.time()
    rows = [_arm(TOK_B, now), _sample(TOK_B, now + 60)]
    films = load_films(rows)
    assert len(films[TOK_B]) == 1
    assert films[TOK_B][0]["disarmed"] is None
    assert len(films[TOK_B][0]["samples"]) == 1


def test_film_fingerprints_base_ratio_tight_vs_wide():
    tight = {"arm_price": 1.0, "arm_liquidity": 10000.0,
              "samples": [{"price": 1.0}, {"price": 1.02}, {"price": 0.99}]}
    wide = {"arm_price": 1.0, "arm_liquidity": 10000.0,
             "samples": [{"price": 1.0}, {"price": 5.0}, {"price": 0.5}]}
    tight_ratio = film_fingerprints(tight)["base_ratio"]
    wide_ratio = film_fingerprints(wide)["base_ratio"]
    assert tight_ratio < 1.1
    assert wide_ratio > 5


def test_film_fingerprints_skips_none_holders_and_top_pct():
    film = {"arm_price": 1.0, "arm_liquidity": 10000.0,
            "samples": [
                {"price": 1.0, "holders": None, "top_pct": None},
                {"price": 1.0, "holders": 100, "top_pct": 0.2},
                {"price": 1.0, "holders": None, "top_pct": None},
                {"price": 1.0, "holders": 150, "top_pct": 0.3},
            ]}
    fp = film_fingerprints(film)
    assert fp["holder_growth_pct"] == 0.5          # 150/100 - 1
    assert fp["max_top_pct"] == 0.3


def test_film_fingerprints_none_for_insufficient_data():
    empty = {"arm_price": 1.0, "arm_liquidity": 10000.0, "samples": []}
    fp = film_fingerprints(empty)
    assert fp["n_samples"] == 0
    assert fp["base_ratio"] is None
    assert fp["holder_growth_pct"] is None
    assert fp["liq_ratio"] is None
    assert fp["max_top_pct"] is None

    one_sample = {"arm_price": 1.0, "arm_liquidity": 10000.0,
                  "samples": [{"price": 1.0, "holders": 100, "top_pct": 0.1}]}
    assert film_fingerprints(one_sample)["base_ratio"] is None  # needs >= 2 prices

    all_none_holders = {"arm_price": 1.0, "arm_liquidity": 10000.0,
                        "samples": [{"price": 1.0, "holders": None, "top_pct": None},
                                    {"price": 1.0, "holders": None, "top_pct": None}]}
    assert film_fingerprints(all_none_holders)["holder_growth_pct"] is None


def test_film_fingerprints_liq_ratio_uses_last_sample_over_arm_liquidity():
    film = {"arm_price": 1.0, "arm_liquidity": 1000.0,
            "samples": [{"price": 1.0, "liq": 1000.0}, {"price": 1.0, "liq": 2000.0}]}
    assert film_fingerprints(film)["liq_ratio"] == 2.0
