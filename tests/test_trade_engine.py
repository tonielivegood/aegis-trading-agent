import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.agent.copy_trade.budget import CopyTradeBudget
from src.agent.copy_trade.positions import CopyPosition, PositionStore
from src.agent.copy_trade.trade_engine import TradeEngine

T = "0x" + "a" * 40
W1, W2, W3, OUTSIDER = ("0x" + c * 40 for c in "1234")
CLUSTER = {"wallets": [W1, W2, W3], "first_ts": 0.0, "first_price_usd": 1.0}


def _engine(tmp_path, shadow=True, executors=None):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=executors,
                      shadow_mode=shadow, journal_path=tmp_path / "closed.jsonl")
    return eng, budget, store


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_shadow_open_never_touches_executors(_g, _p, _t, tmp_path):
    executors = MagicMock()
    eng, budget, store = _engine(tmp_path, shadow=True, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    pos = store.find_by_token(T)
    assert pos.simulated is True
    assert pos.entry_price_usd == 2.0 * (1 + 0.04 + 0.01)
    assert pos.cluster_wallets == [W1, W2, W3]
    assert pos.first_price_usd == 1.0                     # from CLUSTER fixture
    assert budget.available_usd == 16.14 - 3.0
    executors.assert_not_called()
    assert not executors.method_calls        # zero interaction, ever


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_first_price_usd_none_in_cluster_defaults_to_zero(_g, _p, _t, tmp_path):
    """cluster_signal.record() can report first_price_usd=None (no price at
    observation time) — that must not crash the fill or leak a None into the
    journal used by the shadow-mode entry-lateness report."""
    eng, _budget, store = _engine(tmp_path)
    cluster = {"wallets": [W1, W2, W3], "first_ts": 0.0, "first_price_usd": None}
    assert eng.open_cluster_position(T, "GEM", 18, cluster) is True
    assert store.find_by_token(T).first_price_usd == 0.0


@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(False, None))
def test_safety_gate_blocks_and_releases_budget(_s, tmp_path):
    eng, budget, store = _engine(tmp_path)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14
    assert store.all() == []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.04, 0.04))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=2.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_exit_needs_two_cluster_wallets_outsiders_ignored(_g, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.on_exit_signal(OUTSIDER, T)
    eng.on_exit_signal(W1, T)
    assert store.find_by_token(T) is not None            # 1 of 3 — still holding
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W1, T)                            # duplicate — still 1
    assert store.find_by_token(T).exited_by == [W1]
    eng.on_exit_signal(W2, T)                            # 2 of 3 — close
    assert store.find_by_token(T) is None
    assert budget.available_usd == 16.14                 # slice released


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_closes_at_70pct_drawdown(_s, price_mock, _t, tmp_path):
    price_mock.return_value = 10.0
    eng, budget, store = _engine(tmp_path)
    eng.open_cluster_position(T, "GEM", 18,
                              {"wallets": [W1, W2, W3], "first_ts": 0.0,
                               "first_price_usd": 10.0})
    entry = store.find_by_token(T).entry_price_usd
    price_mock.return_value = entry * 0.31               # -69% — hold
    eng.check_exits()
    assert store.find_by_token(T) is not None
    price_mock.return_value = entry * 0.29               # -71% — dump
    eng.check_exits()
    assert store.find_by_token(T) is None
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "valve" and row["simulated"] is True
    assert row["pnl_pct"] < -0.5
    assert row["first_price_usd"] == 10.0                 # ties entry to ví-1 buy price


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=None)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_valve_holds_when_price_unavailable(_s, _p, _t, tmp_path):
    eng, budget, store = _engine(tmp_path)
    # open with a known price first
    with patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=5.0):
        eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.check_exits()                                    # price None — do nothing
    assert store.find_by_token(T) is not None


# ---------- live-money path (Finding 1) ----------

@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_buy_success_opens_real_position(_s, rank_mock, tmp_path):
    buy_result = MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
    pancake = MagicMock()
    pancake.swap.return_value = buy_result
    executors = {"pancake": pancake}
    rank_mock.return_value = ["pancake"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    pos = store.find_by_token(T)
    assert pos.simulated is False
    assert pos.token_amount == 5.0                        # 5e18 wei / 10**18
    assert pos.entry_price_usd == 3.0 / 5.0                # usd_size / token_amount
    pancake.swap.assert_called_once_with("USDT", "GEM", 3.0)
    assert budget.available_usd == 16.14 - 3.0


@patch("src.agent.copy_trade.trade_engine.rank_backends", return_value=[])
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_buy_no_route_releases_budget(_s, _r, tmp_path):
    executors = {"pancake": MagicMock()}
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert budget.available_usd == 16.14                  # slice released, not leaked
    assert store.all() == []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_succeeds_on_first_ranked_backend(_s, rank_mock, _p, _t, tmp_path):
    pancake = MagicMock()
    pancake.swap.return_value = MagicMock(received_out_wei=5 * 10 ** 18,
                                          expected_out_wei=0)
    executors = {"pancake": pancake}
    rank_mock.return_value = ["pancake"]                  # used for buy AND sell
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    pancake.swap.reset_mock()

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # 2 of 3 — triggers close
    assert store.find_by_token(T) is None
    pancake.swap.assert_called_once_with("GEM", "USDT", 5.0)
    assert budget.available_usd == 16.14                  # slice released on close


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_fails_over_to_second_backend(_s, rank_mock, _p, _t, tmp_path):
    def _swap_a(token_in, token_out, amount):
        if token_in == "USDT":                            # the buy leg
            return MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
        raise RuntimeError("route dried up")                # the sell leg fails

    backend_a = MagicMock()
    backend_a.swap.side_effect = _swap_a
    backend_b = MagicMock()
    backend_b.swap.return_value = MagicMock()
    executors = {"a": backend_a, "b": backend_b}
    rank_mock.return_value = ["a", "b"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # triggers close: a fails, b wins
    assert store.find_by_token(T) is None                  # closed despite first failure
    backend_b.swap.assert_called_once_with("GEM", "USDT", 5.0)
    assert budget.available_usd == 16.14


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_sell_all_backends_fail_keeps_position_open(_s, rank_mock, _p, _t,
                                                         tmp_path):
    def _swap(token_in, token_out, amount):
        if token_in == "USDT":                            # the buy leg
            return MagicMock(received_out_wei=5 * 10 ** 18, expected_out_wei=0)
        raise RuntimeError("no route")                      # every sell leg fails

    backend_a = MagicMock()
    backend_a.swap.side_effect = _swap
    backend_b = MagicMock()
    backend_b.swap.side_effect = _swap
    executors = {"a": backend_a, "b": backend_b}
    rank_mock.return_value = ["a", "b"]
    eng, budget, store = _engine(tmp_path, shadow=False, executors=executors)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    assert budget.available_usd == 16.14 - 3.0

    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # all backends fail
    assert store.find_by_token(T) is not None              # position stays open
    assert store.find_by_token(T).exited_by == [W1, W2]    # votes recorded, not lost
    assert budget.available_usd == 16.14 - 3.0              # NOT released — not leaked


# ---------- Finding 2: shadow/real wiring-bug guard ----------

@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=0.1)
def test_shadow_engine_refuses_to_close_a_real_position(_p, tmp_path):
    """Defense-in-depth: if a shadow-mode engine ever ends up holding a
    simulated=False position (a hypothetical future wiring bug), _close() must
    refuse to call _sell_live — never touching self._executors — rather than
    crash (executors may be None) or risk a real swap during shadow mode."""
    executors = MagicMock()
    eng, budget, store = _engine(tmp_path, shadow=True, executors=executors)
    bad_pos = CopyPosition(
        token_symbol="GEM", token_address=T, token_decimals=18,
        source_wallet="", usd_size=3.0, token_amount=5.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        cluster_wallets=[W1, W2, W3], entry_price_usd=1.0,
        simulated=False, first_price_usd=1.0)
    store.open_position(bad_pos)                          # bypass _paper_fill

    eng.check_exits()                                     # price 0.1 vs entry 1.0 → -90%

    assert store.find_by_token(T) is not None              # NOT removed — fail safe
    executors.assert_not_called()
    assert not executors.method_calls                      # zero interaction, ever


# ---------- v3 entry gates ----------
import time as _time


def _engine_v3(tmp_path, **kw):
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=None,
                      shadow_mode=True, journal_path=tmp_path / "closed.jsonl",
                      signals_path=tmp_path / "signals.jsonl", **kw)
    return eng, budget, store


def _signals(tmp_path):
    p = tmp_path / "signals.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_staleness_gate_skips_when_cluster_already_sold_in_batch(_s, _p, _t, tmp_path):
    eng, budget, store = _engine_v3(tmp_path)
    ok = eng.open_cluster_position(T, "GEM", 18, CLUSTER,
                                   batch_sellers={W1.lower(), W2.lower()})
    assert ok is False and store.all() == []
    assert budget.available_usd == 16.14                  # nothing allocated
    assert _signals(tmp_path)[-1]["decision"] == "skipped_stale"
    # only 1 cluster wallet selling is NOT stale (need >= exit_wallets=2)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER,
                                     batch_sellers={W1.lower()}) is True


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_already_open_gate_refuses_second_position_same_token(_s, _p, _t, tmp_path):
    """Root-cause guard: whichever path calls open_cluster_position a second
    time for a token already in the store (cluster-vote re-fire, or a fresh
    phase2 dossier re-arming after 'entered') must be refused — no second
    position, no budget consumed, no double concentration on one token."""
    eng, budget, store = _engine_v3(tmp_path)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    available_after_first = budget.available_usd

    other_cluster = {"wallets": [W2, W3], "first_ts": 1.0, "first_price_usd": 1.5}
    assert eng.open_cluster_position(T, "GEM", 18, other_cluster) is False

    assert len(store.all()) == 1                          # still just one position
    assert budget.available_usd == available_after_first   # second attempt cost nothing
    assert _signals(tmp_path)[-1]["decision"] == "skipped_already_open"


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_cooldown_blocks_reopen_and_seeds_from_journal(_s, _p, _t, tmp_path):
    eng, _b, store = _engine_v3(tmp_path, cooldown_minutes=60)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                             # full close (partial off)
    assert store.find_by_token(T) is None
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False   # cooling down
    assert _signals(tmp_path)[-1]["decision"] == "skipped_cooldown"
    # a NEW engine on the same journal (simulates restart) still refuses
    eng2, _b2, _s2 = _engine_v3(tmp_path, cooldown_minutes=60)
    assert eng2.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    # zero-cooldown engine (v2 behavior) is unaffected
    eng3, _b3, _s3 = _engine_v3(tmp_path)
    assert eng3.open_cluster_position(T, "GEM", 18, CLUSTER) is True


def test_cooldown_seed_skips_malformed_row_missing_token_address(tmp_path):
    """A journal row with valid JSON + valid closed_at but no token_address
    (hand-edit, truncated concurrent write) must be skipped like any other
    malformed row, never crash TradeEngine() construction."""
    journal = tmp_path / "closed.jsonl"
    now_iso = datetime.now(timezone.utc).isoformat()
    journal.write_text(json.dumps({"closed_at": now_iso}) + "\n", encoding="utf-8")
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "shadow_positions.json")
    store.load()
    eng = TradeEngine(budget=budget, store=store, executors=None,
                      shadow_mode=True, journal_path=journal,
                      cooldown_minutes=60)
    assert eng._cooldown_until == {}


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
@patch("src.agent.copy_trade.trade_engine.get_pair_stats")
def test_gem_filter_age_mcap_liquidity(stats_mock, _s, _p, _t, tmp_path):
    young = _time.time() * 1000 - 2 * 86400_000           # 2 days old
    old = _time.time() * 1000 - 40 * 86400_000            # 40 days old
    gates = dict(max_token_age_days=14, max_market_cap_usd=5_000_000,
                 min_liquidity_usd=20_000)
    good = {"price_usd": 1.0, "liquidity_usd": 50_000.0,
            "market_cap_usd": 800_000.0, "pair_created_at_ms": young,
            "pair_address": "0x" + "c" * 40}

    for bad, expect in [
        (dict(good, market_cap_usd=95_000_000.0), "mcap"),       # the O trade
        (dict(good, pair_created_at_ms=old), "age"),
        (dict(good, liquidity_usd=3_000.0), "liquidity"),
        (dict(good, pair_created_at_ms=None), "age"),            # unknown age = skip
        (None, "no_pair_stats"),                                 # API down = skip
    ]:
        stats_mock.return_value = bad
        eng, budget, store = _engine_v3(tmp_path, **gates)
        assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
        assert store.all() == [] and budget.available_usd == 16.14
        assert expect in (_signals(tmp_path)[-1]["decision"]
                          + _signals(tmp_path)[-1]["detail"])

    stats_mock.return_value = good
    eng, _b, store = _engine_v3(tmp_path, **gates)
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True
    assert _signals(tmp_path)[-1]["decision"] == "opened"
    # engine with all gates None never calls get_pair_stats (v2 behavior)
    stats_mock.reset_mock()
    eng2, _b2, _s2 = _engine_v3(tmp_path)
    eng2.open_cluster_position("0x" + "d" * 40, "GEM2", 18, CLUSTER)
    stats_mock.assert_not_called()


# ---------- v3 exits: trailing stop + cluster partial ----------

@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_trailing_stop_lets_winner_run_then_closes(_s, price_mock, _t, tmp_path):
    price_mock.return_value = 1.0
    eng, budget, store = _engine_v3(tmp_path, trail_pct=0.30)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    entry = store.find_by_token(T).entry_price_usd       # 1.0 * 1.01 impact
    price_mock.return_value = 5.0                        # 5x run
    eng.check_exits()
    assert store.find_by_token(T) is not None            # still riding
    assert store.find_by_token(T).high_water_usd == 5.0  # HWM persisted
    price_mock.return_value = 5.0 * 0.71                 # -29% from top — hold
    eng.check_exits()
    assert store.find_by_token(T) is not None
    price_mock.return_value = 5.0 * 0.69                 # -31% from top — out
    eng.check_exits()
    assert store.find_by_token(T) is None
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "trail"
    assert row["pnl_usd"] > 0                            # exited far above entry
    assert budget.available_usd == 16.14


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_trailing_stop_cuts_dud_at_trail_not_valve(_s, price_mock, _t, tmp_path):
    price_mock.return_value = 1.0
    eng, _b, store = _engine_v3(tmp_path, trail_pct=0.30)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    entry = store.find_by_token(T).entry_price_usd
    price_mock.return_value = entry * 0.68               # never pumped, -32%
    eng.check_exits()
    assert store.find_by_token(T) is None                # trail cut it (not -70% valve)
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "trail"


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_cluster_sell_closes_half_once_rest_rides(_s, _p, _t, tmp_path):
    eng, budget, store = _engine_v3(tmp_path, trail_pct=0.30, partial_fraction=0.5)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    full_amount = store.find_by_token(T).token_amount
    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)                            # 2-of-3 → HALF close
    pos = store.find_by_token(T)
    assert pos is not None                               # still open!
    assert pos.token_amount == full_amount / 2
    assert pos.usd_size == 1.5
    assert pos.cluster_partial_done is True
    assert budget.available_usd == 16.14 - 1.5           # half released
    row = json.loads((tmp_path / "closed.jsonl").read_text().splitlines()[-1])
    assert row["reason"] == "cluster_partial" and row["usd_size"] == 1.5
    eng.on_exit_signal(W3, T)                            # 3rd vote — no double partial
    assert store.find_by_token(T).token_amount == full_amount / 2


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.rank_backends")
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_live_partial_sells_half_amount(_s, rank_mock, _p, _t, tmp_path):
    pancake = MagicMock()
    pancake.swap.return_value = MagicMock(received_out_wei=6 * 10 ** 18,
                                          expected_out_wei=0)
    budget = CopyTradeBudget(total_usd=16.14, slice_usd=3.0)
    store = PositionStore(tmp_path / "positions.json")
    store.load()
    rank_mock.return_value = ["pancake"]
    eng = TradeEngine(budget=budget, store=store, executors={"pancake": pancake},
                      shadow_mode=False, journal_path=tmp_path / "closed.jsonl",
                      trail_pct=0.30, partial_fraction=0.5)
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    pancake.swap.reset_mock()
    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)
    pancake.swap.assert_called_once_with("GEM", "USDT", 3.0)   # half of 6.0
    assert store.find_by_token(T).token_amount == 3.0
    # live sell failure on partial: nothing mutates
    pancake.swap.reset_mock()
    pancake.swap.side_effect = RuntimeError("no route")
    rank_mock.return_value = ["pancake"]
    store.find_by_token(T).cluster_partial_done = False        # force retry path
    eng.on_exit_signal(W3, T)
    assert store.find_by_token(T).token_amount == 3.0          # unchanged
    assert store.find_by_token(T).cluster_partial_done is False


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_partial_off_keeps_v2_full_close(_s, _p, _t, tmp_path):
    eng, budget, store = _engine_v3(tmp_path)              # partial_fraction=None
    eng.open_cluster_position(T, "GEM", 18, CLUSTER)
    eng.on_exit_signal(W1, T)
    eng.on_exit_signal(W2, T)
    assert store.find_by_token(T) is None                  # v2: full close
    assert budget.available_usd == 16.14


# ---------- Task 3: circuit breaker + concentration gate ----------

@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
def test_circuit_breaker_ignores_stale_losses_blocks_on_today(_s, _p, _t, tmp_path):
    journal = tmp_path / "closed.jsonl"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    journal.write_text(json.dumps({"closed_at": f"{yesterday}T02:00:00+00:00",
                                    "pnl_usd": -100.0, "simulated": False}) + "\n",
                       encoding="utf-8")
    eng, budget, store = _engine_v3(tmp_path, daily_loss_limit_usd=2.0)
    # yesterday's huge loss must NOT count toward today's breaker
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is True

    today = datetime.now(timezone.utc).date().isoformat()
    with open(journal, "a", encoding="utf-8") as f:
        for _ in range(2):
            f.write(json.dumps({"closed_at": f"{today}T01:00:00+00:00",
                                "pnl_usd": -1.5, "simulated": False}) + "\n")
    T2 = "0x" + "b" * 40
    assert eng.open_cluster_position(T2, "GEM2", 18, CLUSTER) is False
    assert _signals(tmp_path)[-1]["decision"] == "skipped_circuit_breaker"
    assert store.find_by_token(T2) is None


@patch("src.agent.copy_trade.trade_engine.get_taxes", return_value=(0.0, 0.0))
@patch("src.agent.copy_trade.trade_engine.get_price_usd", return_value=1.0)
@patch("src.agent.copy_trade.trade_engine.passes_safety_check",
       return_value=(True, 18))
@patch("src.agent.copy_trade.trade_engine.get_holder_stats")
def test_concentration_gate_blocks_whales_and_fails_closed(holder_mock, _s, _p, _t,
                                                            tmp_path):
    eng, budget, store = _engine_v3(tmp_path, max_single_holder_pct=0.15,
                                    max_top5_holder_pct=0.5)
    T2, T3, T4 = ("0x" + c * 40 for c in "bcd")

    holder_mock.return_value = {"holder_count": 100, "top_pct": 0.205, "top5_pct": 0.3}
    assert eng.open_cluster_position(T, "GEM", 18, CLUSTER) is False
    assert _signals(tmp_path)[-1]["decision"] == "skipped_concentration"

    holder_mock.return_value = None
    assert eng.open_cluster_position(T2, "GEM2", 18, CLUSTER) is False       # fail closed
    assert _signals(tmp_path)[-1]["detail"] == "no_holder_data"

    holder_mock.return_value = {"holder_count": 100, "top_pct": 0.03, "top5_pct": 0.6}
    assert eng.open_cluster_position(T3, "GEM3", 18, CLUSTER) is False       # top5 over
    assert _signals(tmp_path)[-1]["decision"] == "skipped_concentration"

    holder_mock.return_value = {"holder_count": 100, "top_pct": 0.03, "top5_pct": 0.05}
    assert eng.open_cluster_position(T4, "GEM4", 18, CLUSTER) is True        # under both
    assert store.find_by_token(T4) is not None
    assert budget.available_usd == 16.14 - 3.0                              # only T4 opened
