import json

from scripts.simulate_takeprofit_expectancy import (
    Config, _terminal, find_entry, is_unsellable, load_token_samples,
    simulate_token, summarize,
)

# Fill immediately and charge nothing, so each test isolates the behaviour it
# names instead of measuring the cost model.
FREE = Config(size_usd=100.0, fill_delay=0, dex_fee=0.0, gas_usd=0.0)


def _samples(prices, liqs=None, dominance=None, t0=1_000_000.0):
    liqs = liqs if liqs is not None else [1e9] * len(prices)
    dominance = dominance or [True] * len(prices)
    return [{"price": p, "liq": l, "ts": t0 + 30.0 * i,
             "buys_m5": 10 if d else 0, "sells_m5": 0 if d else 10}
            for i, (p, l, d) in enumerate(zip(prices, liqs, dominance))]


def test_every_outcome_carries_both_timestamps():
    """The bankroll walk sorts on entry_ts. When the total-loss paths omitted it
    they sorted to the front, stacking ~75% of the losses ahead of every winner
    and reporting -100% at every position size — a bug that reads exactly like a
    finding."""
    cases = [
        (_samples([1.0, 2.0, 3.0]), None),                       # win
        (_samples([1.0, 2.0, 0.1], liqs=[1e9, 1e9, 0.5]), None),  # rugged
        (_samples([1.0, 2.0, 2.1]), None),                       # open at end
        (_samples([1.0, 2.0, 2.1], liqs=[1e9, 0.5, 0.5]), None),  # dead on arrival
        (_samples([1.0, 2.0, 9.0]), {"is_honeypot": "1"}),       # unsellable
    ]
    seen = set()
    for samples, goplus in cases:
        r = simulate_token(samples, goplus, target=1.5, cfg=FREE)
        assert r is not None
        assert r["entry_ts"] is not None, r["outcome"]
        assert r["exit_ts"] is not None, r["outcome"]
        assert r["exit_ts"] >= r["entry_ts"], r["outcome"]
        seen.add(r["outcome"])
    assert seen == {"win", "rugged", "open_closed_at_end", "dead_on_arrival",
                    "unsellable"}


def test_find_entry_is_the_first_sample_at_or_above_2x_arm_price():
    assert find_entry(_samples([1.0, 1.5, 1.9, 2.0, 3.0])) == 3


def test_find_entry_is_none_when_2x_is_never_reached():
    assert find_entry(_samples([1.0, 1.2, 1.5, 1.9])) is None


def test_find_entry_with_dominance_skips_a_2x_sample_where_sells_lead():
    s = _samples([1.0, 2.0, 2.5], dominance=[True, False, True])
    assert find_entry(s, require_dominance=True) == 2


def test_a_target_reached_before_the_drain_returns_the_target():
    s = _samples([1.0, 2.0, 3.0, 0.1], liqs=[1e9, 1e9, 1e9, 0.5])
    out = simulate_token(s, None, target=1.5, cfg=FREE)
    assert out["outcome"] == "win"
    assert round(out["net_multiple"], 6) == 1.5


def test_a_drain_before_the_target_is_a_total_loss_not_a_stop_loss():
    """24 of 25 measured deaths gave no exit window at all, so the loss is the
    whole position — modelling it as a stop-loss percentage would invent an
    exit that does not exist."""
    s = _samples([1.0, 2.0, 2.1, 0.05], liqs=[1e9, 1e9, 1e9, 0.5])
    out = simulate_token(s, None, target=3.0, cfg=FREE)
    assert out["outcome"] == "rugged" and out["net_multiple"] == 0.0


def test_a_position_still_open_at_the_end_is_SOLD_not_excluded():
    """Excluding these flatters the result: they are disproportionately tokens
    that went nowhere, and a real bot still holds something it must sell."""
    s = _samples([1.0, 2.0, 2.2])          # entered at 2.0, never hit 3x, alive
    out = simulate_token(s, None, target=3.0, cfg=FREE)
    assert out["outcome"] == "open_closed_at_end"
    assert round(out["net_multiple"], 6) == 1.1     # 2.2 / 2.0


def test_an_unsellable_token_is_a_total_loss_however_high_the_price_goes():
    s = _samples([1.0, 2.0, 20.0])
    assert simulate_token(s, {"is_honeypot": "1"}, 3.0, FREE)["net_multiple"] == 0.0
    assert simulate_token(s, {"cannot_sell_all": "1"}, 3.0, FREE)["net_multiple"] == 0.0
    assert is_unsellable({"is_honeypot": "1"}) is True
    assert is_unsellable(None) is False


def test_taxes_and_fees_come_off_both_legs():
    s = _samples([1.0, 2.0, 4.0])
    cfg = Config(size_usd=100.0, fill_delay=0, dex_fee=0.01, gas_usd=0.0)
    out = simulate_token(s, {"buy_tax": "0.05", "sell_tax": "0.10"}, 2.0, cfg)
    # 2x gross, minus 1% fee and 5% buy tax in, minus 1% fee and 10% sell tax out
    assert round(out["net_multiple"], 6) == round(
        2.0 * 0.99 * 0.95 * 0.99 * 0.90, 6)


def test_a_missing_goplus_record_is_treated_as_no_tax_not_as_unsellable():
    s = _samples([1.0, 2.0, 4.0])
    assert round(simulate_token(s, None, 2.0, FREE)["net_multiple"], 6) == 2.0


def test_price_impact_scales_with_position_against_pool_size():
    s = _samples([1.0, 2.0, 4.0], liqs=[10_000.0] * 3)
    big = simulate_token(s, None, 2.0, Config(size_usd=1_000.0, fill_delay=0,
                                              dex_fee=0.0, gas_usd=0.0))
    small = simulate_token(s, None, 2.0, Config(size_usd=10.0, fill_delay=0,
                                                dex_fee=0.0, gas_usd=0.0))
    assert big["net_multiple"] < small["net_multiple"] < 2.0


def test_buying_into_an_already_drained_pool_returns_nothing():
    s = _samples([1.0, 2.0, 2.1], liqs=[1e9, 0.5, 0.5])
    out = simulate_token(s, None, 3.0, FREE)
    assert out["outcome"] == "dead_on_arrival" and out["net_multiple"] == 0.0


def test_never_reaching_2x_is_not_a_trade_at_all():
    assert simulate_token(_samples([1.0, 1.1, 1.2]), None, 3.0, FREE) is None


def _trade(entry_ts, exit_ts, mult):
    return {"outcome": "x", "net_multiple": mult,
            "entry_ts": entry_ts, "exit_ts": exit_ts}


def test_capital_stays_locked_until_a_position_actually_exits():
    """With one slot, the second trade cannot be taken while the first is open,
    so its payout must not appear."""
    cfg = Config(size_usd=100.0, fill_delay=0)
    rows = [_trade(0, 1000, 6.0), _trade(10, 20, 6.0)]
    # 400 left + 600 from the one position taken. Taking both would give 1500,
    # which is exactly what the next test asserts once the slot frees in time.
    assert _terminal(rows, cfg, bankroll=500.0, max_concurrent=1) == 1000.0


def test_a_slot_frees_once_its_position_has_exited():
    cfg = Config(size_usd=100.0, fill_delay=0)
    rows = [_trade(0, 5, 6.0), _trade(10, 20, 6.0)]
    assert _terminal(rows, cfg, bankroll=500.0, max_concurrent=1) == 1500.0


def test_trades_are_skipped_once_the_bankroll_cannot_cover_one():
    """Losing everything on ~75% of trades means sizing decides survival, so a
    bankroll walk that let trades happen on credit would be meaningless."""
    cfg = Config(size_usd=100.0, fill_delay=0)
    rows = [_trade(i * 10, i * 10 + 5, 0.0) for i in range(10)]
    assert _terminal(rows, cfg, bankroll=250.0, max_concurrent=5) == 50.0


def _write_film(tmp_path, rows):
    p = tmp_path / "films.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _arm(tok):
    return {"event": "arm", "token_address": tok}


def _s(tok, ts, price, liq=1e9):
    return {"event": "sample", "token_address": tok, "ts": ts,
            "price": price, "liq": liq}


def test_only_the_first_film_of_a_re_armed_token_is_used(tmp_path):
    """A restart clears the collector's in-memory `seen` set, so tokens get
    armed twice. Concatenating both films by address stitches two separate price
    histories together and invents a move that never happened."""
    tok = "0xaaa"
    path = _write_film(tmp_path, [
        _arm(tok), _s(tok, 100, 1.0), _s(tok, 130, 1.1),
        _arm(tok), _s(tok, 900, 9.0), _s(tok, 930, 9.5),
    ])
    by_token, rejected = load_token_samples(path)
    assert [s["price"] for s in by_token[tok]] == [1.0, 1.1]
    assert rejected["re_armed"] == 1


def test_a_film_with_an_impossible_price_jump_is_thrown_out(tmp_path):
    """One 'winner' jumped x22,996 between two 30s samples (5/8). Real winners
    climb gradually — the median biggest single-sample jump is x1.18."""
    good, bad = "0xgood", "0xbad"
    path = _write_film(tmp_path, [
        _arm(good), _s(good, 100, 1.0), _s(good, 130, 1.2), _s(good, 160, 1.5),
        _arm(bad), _s(bad, 100, 1.0), _s(bad, 130, 23_000.0),
    ])
    by_token, rejected = load_token_samples(path)
    assert good in by_token and bad not in by_token
    assert rejected["bad_print"] == 1


def test_a_large_but_believable_climb_is_kept(tmp_path):
    """Real 30s moves in this data reach x12. The threshold sits two orders
    above that so it only ever catches the corrupt class."""
    tok = "0xaaa"
    path = _write_film(tmp_path, [
        _arm(tok), _s(tok, 100, 1.0), _s(tok, 130, 12.45), _s(tok, 160, 14.0),
    ])
    by_token, rejected = load_token_samples(path)
    assert tok in by_token and rejected["bad_print"] == 0


def test_a_rug_is_KEPT_however_violently_the_price_falls(tmp_path):
    """24 of 25 deaths go from healthy liquidity to under $1k inside one 30s
    sample, so a violent fall is the strategy's real loss mode. Rejecting those
    as bad prints removed 80% of the dataset — almost all of it losers — and
    lifted expectancy from +0.32 to +0.79. A filter that deletes the losing half
    is more dangerous than the glitch it was written to remove."""
    tok = "0xaaa"
    path = _write_film(tmp_path, [
        _arm(tok), _s(tok, 100, 1000.0), _s(tok, 130, 0.001, liq=0.5),
    ])
    by_token, rejected = load_token_samples(path)
    assert tok in by_token and rejected["bad_print"] == 0


def test_samples_before_any_arm_row_are_ignored(tmp_path):
    tok = "0xaaa"
    path = _write_film(tmp_path, [
        _s(tok, 50, 5.0), _arm(tok), _s(tok, 100, 1.0), _s(tok, 130, 1.1),
    ])
    by_token, _ = load_token_samples(path)
    assert [s["price"] for s in by_token[tok]] == [1.0, 1.1]


def test_summarize_averages_every_position_including_the_zeros():
    rows = ([{"outcome": "win", "net_multiple": 6.0}] * 2
            + [{"outcome": "rugged", "net_multiple": 0.0}] * 8)
    st = summarize(rows, 6.0)
    assert st["n"] == 10 and st["wins"] == 2 and st["total_losses"] == 8
    assert st["profit_rate"] == 0.2
    assert round(st["expectancy"], 6) == 0.2      # (2*6 + 8*0)/10 - 1
