from scripts.simulate_takeprofit_expectancy import (
    expectancy, find_entry, simulate_token,
)


def _samples(prices, liqs=None, dominance=None):
    liqs = liqs or [50_000.0] * len(prices)
    dominance = dominance or [True] * len(prices)
    return [{"price": p, "liq": l,
             "buys_m5": 10 if d else 0, "sells_m5": 0 if d else 10}
            for p, l, d in zip(prices, liqs, dominance)]


def test_find_entry_is_the_first_sample_at_or_above_2x_arm_price():
    s = _samples([1.0, 1.5, 1.9, 2.0, 3.0])
    assert find_entry(s, require_dominance=False) == 3


def test_find_entry_is_none_when_2x_is_never_reached():
    s = _samples([1.0, 1.2, 1.5, 1.9])
    assert find_entry(s, require_dominance=False) is None


def test_find_entry_with_dominance_skips_a_2x_sample_where_sells_lead():
    s = _samples([1.0, 2.0, 2.5], dominance=[True, False, True])
    assert find_entry(s, require_dominance=True) == 2


def test_a_target_hit_before_death_is_a_win():
    # entry at 2.0x arm; from there 3.0x (=1.5x entry) is hit before liq dies.
    s = _samples([1.0, 2.0, 3.0, 0.1], liqs=[50_000, 50_000, 50_000, 0.5])
    out = simulate_token(s, require_dominance=False)
    assert out["entered"] is True
    assert out["results"][1.5] == "win"    # 3.0 / 2.0 = 1.5x entry


def test_death_before_the_target_is_hit_is_a_loss():
    s = _samples([1.0, 2.0, 2.1, 0.05], liqs=[50_000, 50_000, 50_000, 0.5])
    out = simulate_token(s, require_dominance=False)
    assert out["results"][3.0] == "loss"    # never reached 3x entry, then died


def test_the_film_ending_with_neither_outcome_is_open():
    s = _samples([1.0, 2.0, 2.1, 2.2])       # never dies, never hits 3x
    out = simulate_token(s, require_dominance=False)
    assert out["results"][3.0] == "open"


def test_a_tie_between_win_and_death_favors_the_win():
    # A limit-sell watching this tick would see the target price satisfied in
    # the same sample the pool goes empty — the fill happens before the drain.
    s = _samples([1.0, 2.0, 3.0], liqs=[50_000, 50_000, 0.5])
    out = simulate_token(s, require_dominance=False)
    assert out["results"][1.5] == "win"      # 3.0/2.0 = 1.5x, same sample as death


def test_never_entering_is_excluded_from_the_token_but_not_a_crash():
    s = _samples([1.0, 1.1, 1.2])
    out = simulate_token(s, require_dominance=False)
    assert out == {"entered": False}


def test_expectancy_matches_a_hand_worked_example():
    # 2 wins at 2x (+1.0 each), 3 losses (-1.0 each), 1 still open (excluded).
    outcomes = (
        [{"entered": True, "results": {2.0: "win"}}] * 2
        + [{"entered": True, "results": {2.0: "loss"}}] * 3
        + [{"entered": True, "results": {2.0: "open"}}]
    )
    e = expectancy(outcomes, 2.0)
    assert e == {"wins": 2, "losses": 3, "open": 1, "resolved": 5,
                "win_rate": 0.4, "expectancy_per_unit": 0.4 * 1.0 - 0.6 * 1.0}
