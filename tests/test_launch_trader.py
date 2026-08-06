import json

import pytest

from src.agent.copy_trade import launch_trader as lt
from src.agent.copy_trade.launch_trader import (
    LaunchPosition, LaunchTrader, TraderConfig, TraderState, exit_reason,
    load_state, read_new_film_rows, save_state, should_enter,
)

NOW = 1_800_000_000.0
T1 = "0x" + "1" * 40
T2 = "0x" + "2" * 40


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this suite may reach DexScreener."""
    monkeypatch.setattr(lt, "get_pair_stats",
                        lambda t: {"price_usd": 2.0, "liquidity_usd": 50_000.0})


def _trader(tmp_path, cfg=None, state=None, dry_run=True, executors=None):
    return LaunchTrader(cfg or TraderConfig(size_usd=2.0, max_concurrent=3,
                                            bankroll_usd=50.0,
                                            max_total_loss_usd=25.0),
                        state or TraderState(), executors,
                        tmp_path / "state.json", tmp_path / "trades.jsonl",
                        dry_run=dry_run)


def _sample(token=T1, price=2.0, liq=50_000.0):
    return {"event": "sample", "token_address": token, "price": price,
            "liq": liq, "symbol": "TKN"}


# ---------- entry rule ----------

def test_entry_needs_the_full_2x_not_a_whisker_under():
    assert should_enter(_sample(price=1.99), arm_price=1.0) is False
    assert should_enter(_sample(price=2.0), arm_price=1.0) is True


def test_entry_refuses_a_pool_that_is_already_draining():
    assert should_enter(_sample(price=2.0, liq=500.0), arm_price=1.0) is False


def test_entry_needs_an_arm_price_at_all():
    assert should_enter(_sample(price=2.0), arm_price=0.0) is False


# ---------- exit rule ----------

def test_take_profit_fires_at_6x_the_entry_not_the_signal_price():
    pos = LaunchPosition(T1, "TKN", entry_price=2.0, usd_size=2.0,
                         token_amount=1.0, opened_at=NOW)
    assert exit_reason(pos, price=11.9, liq=50_000.0, now=NOW) is None
    assert exit_reason(pos, price=12.0, liq=50_000.0, now=NOW) == "take_profit"


def test_there_is_no_stop_loss_however_far_the_price_falls():
    """24 of 25 measured deaths gave zero exit window — a stop cannot fill, so
    the strategy never places one. ~66% of trades are total losses by design."""
    pos = LaunchPosition(T1, "TKN", entry_price=2.0, usd_size=2.0,
                         token_amount=1.0, opened_at=NOW)
    assert exit_reason(pos, price=0.02, liq=50_000.0, now=NOW) is None


def test_a_drained_pool_is_reported_dead_before_anything_else():
    pos = LaunchPosition(T1, "TKN", entry_price=2.0, usd_size=2.0,
                         token_amount=1.0, opened_at=NOW)
    assert exit_reason(pos, price=99.0, liq=10.0, now=NOW) == "dead"


def test_the_position_is_closed_at_the_measured_holding_limit():
    """Expectancy was measured closing every open position at the 4h film end.
    Holding longer is untested behaviour, not a free option."""
    pos = LaunchPosition(T1, "TKN", entry_price=2.0, usd_size=2.0,
                         token_amount=1.0, opened_at=NOW)
    assert exit_reason(pos, 3.0, 50_000.0, NOW + lt.MAX_HOLD_S - 1) is None
    assert exit_reason(pos, 3.0, 50_000.0, NOW + lt.MAX_HOLD_S) == "max_hold"


# ---------- capital gates ----------

def test_a_token_is_never_traded_twice_even_after_it_closes(tmp_path):
    state = TraderState(traded_tokens={T1})
    ok, why = _trader(tmp_path, state=state).can_open(T1)
    assert (ok, why) == (False, "already_traded")


def test_no_more_positions_than_slots(tmp_path):
    state = TraderState(open_positions={
        f"0x{i}": LaunchPosition(f"0x{i}", "T", 1.0, 2.0, 1.0, NOW)
        for i in range(3)})
    ok, why = _trader(tmp_path, state=state).can_open(T1)
    assert (ok, why) == (False, "no_slot")


def test_exposure_can_never_exceed_the_bankroll(tmp_path):
    cfg = TraderConfig(size_usd=2.0, max_concurrent=10, bankroll_usd=4.0)
    state = TraderState(open_positions={
        f"0x{i}": LaunchPosition(f"0x{i}", "T", 1.0, 2.0, 1.0, NOW)
        for i in range(2)})
    ok, why = _trader(tmp_path, cfg=cfg, state=state).can_open(T1)
    assert (ok, why) == (False, "bankroll")


def test_realised_losses_hitting_the_limit_stop_all_new_entries(tmp_path):
    """A hard floor the strategy's own maths cannot argue past."""
    cfg = TraderConfig(size_usd=2.0, max_total_loss_usd=25.0)
    state = TraderState(realised_pnl_usd=-25.0)
    ok, why = _trader(tmp_path, cfg=cfg, state=state).can_open(T1)
    assert (ok, why) == (False, "loss_limit")


# ---------- the loop ----------

def test_an_arm_row_supplies_the_baseline_a_later_sample_is_judged_against(tmp_path):
    t = _trader(tmp_path)
    assert t.on_sample(_sample(price=2.0), now=NOW) is None   # no arm price yet
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    assert t.on_sample(_sample(price=2.0), now=NOW) == "opened"


def test_a_position_opens_then_takes_profit_through_the_sample_stream(tmp_path):
    t = _trader(tmp_path)
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    assert t.on_sample(_sample(price=2.0), now=NOW) == "opened"
    # get_pair_stats is stubbed to $2.00, so entry is 2.0 and the target is 12.0
    assert t.on_sample(_sample(price=11.0), now=NOW + 60) is None
    assert t.on_sample(_sample(price=12.0), now=NOW + 90) == "take_profit"
    assert t._state.open_positions == {}
    assert t._state.realised_pnl_usd == pytest.approx(2.0 * 6 - 2.0)


def test_a_dead_pool_books_a_total_loss_and_never_tries_to_sell(tmp_path):
    sold = []
    t = _trader(tmp_path)
    t._sell = lambda pos: sold.append(pos) or True
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=2.0), now=NOW)
    assert t.on_sample(_sample(price=1e-9, liq=0.5), now=NOW + 60) == "dead"
    assert sold == []                                  # no buyer exists
    assert t._state.realised_pnl_usd == pytest.approx(-2.0)


def test_a_failed_sell_keeps_the_position_open_for_a_retry(tmp_path):
    t = _trader(tmp_path)
    t._sell = lambda pos: False
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=2.0), now=NOW)
    assert t.on_sample(_sample(price=12.0), now=NOW + 60) == "sell_failed"
    assert T1 in t._state.open_positions
    assert t._state.realised_pnl_usd == 0.0


def test_a_stale_film_price_cannot_commit_money_on_its_own(tmp_path, monkeypatch):
    """A film row can be 30s old. The trade is priced off a live quote, and a
    pool that has drained in the meantime must abort the entry."""
    monkeypatch.setattr(lt, "get_pair_stats",
                        lambda t: {"price_usd": 0.01, "liquidity_usd": 100.0})
    t = _trader(tmp_path)
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    assert t.on_sample(_sample(price=2.0), now=NOW) is None
    assert t._state.open_positions == {}


def test_timeouts_are_swept_even_when_the_film_stream_goes_silent(tmp_path):
    """The collector disarms a token at 4h, so the samples that would trigger the
    exit stop arriving exactly when they are needed."""
    t = _trader(tmp_path)
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=2.0), now=NOW)
    assert t.sweep_timeouts(now=NOW + 100) == 0
    assert t.sweep_timeouts(now=NOW + lt.MAX_HOLD_S + 1) == 1
    assert t._state.open_positions == {}


# ---------- durability ----------

def test_open_positions_and_offset_survive_a_restart(tmp_path):
    p = tmp_path / "state.json"
    state = TraderState(
        open_positions={T1: LaunchPosition(T1, "TKN", 2.0, 2.0, 1.0, NOW)},
        traded_tokens={T1, T2}, realised_pnl_usd=-3.5, film_offset=4096)
    save_state(p, state)
    back = load_state(p)
    assert back.open_positions[T1].entry_price == 2.0
    assert back.traded_tokens == {T1, T2}
    assert back.realised_pnl_usd == -3.5 and back.film_offset == 4096


def test_a_torn_last_line_is_left_for_the_next_pass(tmp_path):
    """The collector appends while this reads, so a half-written line is normal.
    Consuming it would drop a sample and shift the offset past real data."""
    p = tmp_path / "films.jsonl"
    p.write_text('{"event":"sample","token_address":"0xa"}\n{"event":"sam',
                 encoding="utf-8")
    rows, offset = read_new_film_rows(p, 0)
    assert len(rows) == 1
    p.write_text('{"event":"sample","token_address":"0xa"}\n'
                 '{"event":"sample","token_address":"0xb"}\n', encoding="utf-8")
    rows2, _ = read_new_film_rows(p, offset)
    assert [r["token_address"] for r in rows2] == ["0xb"]


def test_a_truncated_film_file_rereads_from_the_start(tmp_path):
    p = tmp_path / "films.jsonl"
    p.write_text('{"event":"sample","token_address":"0xa"}\n', encoding="utf-8")
    rows, _ = read_new_film_rows(p, 999_999)
    assert len(rows) == 1


def test_unparseable_lines_do_not_stop_the_stream(tmp_path):
    p = tmp_path / "films.jsonl"
    p.write_text('not json\n{"event":"sample","token_address":"0xa"}\n',
                 encoding="utf-8")
    rows, _ = read_new_film_rows(p, 0)
    assert [r["token_address"] for r in rows] == ["0xa"]


def test_replay_prices_the_entry_from_the_film_not_a_live_quote(tmp_path):
    """Replaying an old film through a live quote pairs a historical exit price
    against an entry quoted at TODAY's price. The first replay did that and
    reported $26 of profit on a $2 position whose ceiling is $10."""
    cfg = TraderConfig(size_usd=2.0, use_live_quote=False)
    t = _trader(tmp_path, cfg=cfg)          # stubbed live quote says $2.00
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=8.0), now=NOW)
    assert t._state.open_positions[T1].entry_price == 8.0    # film, not $2.00


def test_replay_runs_on_the_films_clock_not_the_wall_clock(tmp_path):
    """Film timestamps are days old. On wall-clock time every replayed position
    is instantly past the 4h hold limit and closes before it can do anything."""
    films = tmp_path / "films.jsonl"
    films.write_text("\n".join(json.dumps(r) for r in [
        {"event": "arm", "token_address": T1, "price": 1.0, "ts": NOW},
        {"event": "sample", "token_address": T1, "price": 2.0, "liq": 50_000.0,
         "ts": NOW + 30},
        {"event": "sample", "token_address": T1, "price": 3.0, "liq": 50_000.0,
         "ts": NOW + 60},
    ]) + "\n", encoding="utf-8")
    lt.run(films, tmp_path / "s.json", tmp_path / "j.jsonl",
           TraderConfig(size_usd=2.0, use_live_quote=False), None,
           dry_run=True, once=True)
    state = load_state(tmp_path / "s.json")
    assert list(state.open_positions) == [T1]      # still open, not timed out


def test_slots_are_freed_as_the_clock_advances_through_one_batch(tmp_path):
    """A batch spanning days would otherwise hold its first positions open for
    the whole span and refuse every later signal for "no_slot" — the first full
    replay took 7 trades instead of ~1000 that way."""
    rows = [{"event": "arm", "token_address": T1, "price": 1.0, "ts": NOW},
            {"event": "sample", "token_address": T1, "price": 2.0,
             "liq": 50_000.0, "ts": NOW + 30}]
    # A second token arriving long after the first must still find a free slot.
    later = NOW + 3 * lt.MAX_HOLD_S
    rows += [{"event": "arm", "token_address": T2, "price": 1.0, "ts": later},
             {"event": "sample", "token_address": T2, "price": 2.0,
              "liq": 50_000.0, "ts": later + 30}]
    films = tmp_path / "films.jsonl"
    films.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                     encoding="utf-8")
    lt.run(films, tmp_path / "s.json", tmp_path / "j.jsonl",
           TraderConfig(size_usd=2.0, max_concurrent=1, use_live_quote=False),
           None, dry_run=True, once=True)
    state = load_state(tmp_path / "s.json")
    assert state.traded_tokens == {T1, T2}      # T1 timed out, freeing the slot


def test_a_timed_out_position_is_sold_at_its_last_price_not_written_off(tmp_path):
    """The film goes silent at exactly 4h because the collector disarms there.
    Valuing the position at zero booked a total loss on every max_hold close."""
    t = _trader(tmp_path, cfg=TraderConfig(size_usd=2.0, use_live_quote=False))
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=2.0), now=NOW)
    t.on_sample(_sample(price=3.0), now=NOW + 60)          # last seen price
    assert t.sweep_timeouts(now=NOW + lt.MAX_HOLD_S + 1) == 1
    # bought 1 token at $2, sold at $3
    assert t._state.realised_pnl_usd == pytest.approx(1.0)


def test_a_missing_film_stream_is_fatal_not_silent(tmp_path):
    """A missing film file reads identically to "no new samples yet". The first
    live run pointed one directory too high and sat there reading nothing, fully
    healthy-looking, with film_offset stuck at 0."""
    with pytest.raises(FileNotFoundError):
        lt.run(tmp_path / "absent.jsonl", tmp_path / "s.json",
               tmp_path / "j.jsonl", TraderConfig(), None, dry_run=True,
               once=True)


def test_the_default_film_path_points_inside_the_project():
    assert (lt.ROOT / "src" / "agent" / "copy_trade" / "launch_trader.py").exists()


def test_every_close_is_journalled(tmp_path):
    t = _trader(tmp_path)
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    t.on_sample(_sample(price=2.0), now=NOW)
    t.on_sample(_sample(price=12.0), now=NOW + 60)
    rows = [json.loads(x) for x in
            (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reason"] == "take_profit" and rows[0]["pnl_usd"] == 10.0


# ---------- live-money safety ----------

def test_a_live_buy_sizes_the_position_with_the_tokens_real_decimals(tmp_path):
    """token_amount = wei / 10**decimals. Film samples carry no decimals, and
    defaulting to 18 would record a 6-decimal token a TRILLION times too small —
    entry price, the 6x target and the sell quantity all wrong."""
    class Result:
        received_out_wei = 5_000_000            # 5.0 tokens at 6 decimals

    class Exec:
        def swap(self, *a, **k):
            return Result()

    class Rpc:
        def call(self, method, params):
            sig = params[0]["data"]
            if sig == "0x313ce567":             # decimals()
                return hex(6)
            return None                          # symbol() falls back

    t = LaunchTrader(TraderConfig(size_usd=2.0), TraderState(), {"x": Exec()},
                     tmp_path / "s.json", tmp_path / "j.jsonl",
                     dry_run=False, rpc_pool=Rpc())
    import src.agent.copy_trade.launch_trader as mod
    monkey = mod.rank_backends
    mod.rank_backends = lambda ex, a, b, s: ["x"]
    try:
        pos = t._buy(T1, "IGNORED", price=1.0, now=NOW)
    finally:
        mod.rank_backends = monkey
    assert pos.decimals == 6
    assert pos.token_amount == 5.0              # not 5e-12
    assert pos.entry_price == pytest.approx(0.4)


def test_dry_run_never_touches_an_executor(tmp_path):
    class Boom:
        def swap(self, *a, **k):
            raise AssertionError("dry run must not place an order")

    t = _trader(tmp_path, executors={"1inch": Boom()}, dry_run=True)
    t.on_sample({"event": "arm", "token_address": T1, "price": 1.0}, now=NOW)
    assert t.on_sample(_sample(price=2.0), now=NOW) == "opened"
    assert t.on_sample(_sample(price=12.0), now=NOW + 60) == "take_profit"
