from datetime import datetime, timezone

import pytest

from src.agent.copy_trade.launch_collector import (
    ARM_MAX_AGE_S, ARM_MIN_AGE_S, MIN_RESERVE_USD, arm_candidates, new_pools,
    parse_pools,
)

NOW = 1_785_000_000.0
T1 = "0x" + "1" * 40
T2 = "0x" + "2" * 40


def _pool(token=T1, age_s=30 * 60, reserve=50_000.0, pool="0x" + "9" * 40):
    """A parsed pool dict as parse_pools() emits it."""
    return {"pool_address": pool, "token_address": token, "name": "TOK / WBNB",
            "created_ts": NOW - age_s, "reserve_usd": reserve}


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


# ---------- T1: arm_candidates ----------

def test_arm_candidates_accepts_pool_inside_window():
    out = arm_candidates([_pool(age_s=30 * 60)], now=NOW, seen=set())
    assert [p["token_address"] for p in out] == [T1]


def test_arm_candidates_rejects_one_second_too_young():
    # 14:59 is out, 15:00 is in — the boundary is where an off-by-one hides.
    assert arm_candidates([_pool(age_s=ARM_MIN_AGE_S - 1)], now=NOW, seen=set()) == []
    assert len(arm_candidates([_pool(age_s=ARM_MIN_AGE_S)], now=NOW, seen=set())) == 1


def test_arm_candidates_rejects_one_second_too_old():
    assert arm_candidates([_pool(age_s=ARM_MAX_AGE_S + 1)], now=NOW, seen=set()) == []
    assert len(arm_candidates([_pool(age_s=ARM_MAX_AGE_S)], now=NOW, seen=set())) == 1


def test_arm_candidates_rejects_thin_liquidity():
    assert arm_candidates([_pool(reserve=MIN_RESERVE_USD - 1)], now=NOW, seen=set()) == []
    assert len(arm_candidates([_pool(reserve=MIN_RESERVE_USD)], now=NOW, seen=set())) == 1


def test_arm_candidates_skips_already_seen_tokens():
    assert arm_candidates([_pool(token=T1)], now=NOW, seen={T1}) == []


def test_arm_candidates_seen_check_is_case_insensitive():
    # DexScreener/GeckoTerminal hand back checksummed addresses; the dedupe set
    # must not let the same token in twice under different casing.
    assert arm_candidates([_pool(token=T1.upper())], now=NOW, seen={T1}) == []


def test_arm_candidates_dedupes_within_one_batch():
    # The same token can have two pools; only film it once.
    out = arm_candidates([_pool(pool="0xaaa"), _pool(pool="0xbbb")], now=NOW, seen=set())
    assert len(out) == 1


def test_arm_candidates_survives_missing_or_malformed_fields():
    bad = [{"token_address": T1},                                   # no created_ts
           {"pool_address": "0x1", "created_ts": None},             # no token
           {"token_address": T2, "created_ts": "not-a-number",
            "reserve_usd": 99_000.0},
           {"token_address": T2, "created_ts": NOW - 1800,
            "reserve_usd": None}]
    assert arm_candidates(bad, now=NOW, seen=set()) == []


# ---------- T2: parse_pools / new_pools ----------

def _gt_pool(token=T1, created="2026-07-30T09:00:00Z", reserve="50000.0",
             addr="0x" + "9" * 40):
    return {"id": f"bsc_{addr}", "type": "pool",
            "attributes": {"address": addr, "name": "TOK / WBNB",
                           "pool_created_at": created, "reserve_in_usd": reserve},
            "relationships": {"base_token": {"data": {"id": f"bsc_{token}"}}}}


def test_parse_pools_extracts_the_fields_arm_candidates_needs():
    expected_ts = datetime(2026, 7, 30, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    out = parse_pools({"data": [_gt_pool(created="2026-07-30T09:00:00Z")]})
    assert len(out) == 1
    p = out[0]
    assert p["token_address"] == T1
    assert p["reserve_usd"] == 50_000.0
    assert p["created_ts"] == expected_ts
    assert p["pool_address"] == "0x" + "9" * 40


def test_parse_pools_skips_rows_without_a_base_token_relationship():
    row = _gt_pool()
    row["relationships"] = {}
    assert parse_pools({"data": [row]}) == []


def test_parse_pools_skips_rows_with_unparseable_created_at():
    assert parse_pools({"data": [_gt_pool(created="never")]}) == []
    assert parse_pools({"data": [_gt_pool(created=None)]}) == []


def test_parse_pools_tolerates_empty_or_malformed_payload():
    assert parse_pools({}) == []
    assert parse_pools({"data": []}) == []
    assert parse_pools({"data": [{"attributes": {}}]}) == []


def test_new_pools_requests_each_page_and_merges(monkeypatch):
    seen_urls = []

    def fake_get(url, **kw):
        seen_urls.append(url)
        page = int(url.rsplit("=", 1)[1])
        return FakeResp({"data": [_gt_pool(token=f"0x{page:040x}",
                                           addr=f"0x{page:040x}")]})

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fake_get)
    out = new_pools(pages=3)
    assert len(seen_urls) == 3
    assert [u.rsplit("=", 1)[1] for u in seen_urls] == ["1", "2", "3"]
    assert len(out) == 3


def test_new_pools_stops_early_when_a_page_is_empty(monkeypatch):
    # Live shape 2026-07-30: BSC new_pools has 4 pages, page 5 is empty.
    # Continuing past the end just burns the 30 req/min GeckoTerminal budget.
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        page = int(url.rsplit("=", 1)[1])
        return FakeResp({"data": [] if page >= 3 else [_gt_pool(addr=f"0x{page:040x}")]})

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fake_get)
    out = new_pools(pages=10)
    assert len(calls) == 3          # 1, 2, then the empty 3 stops it
    assert len(out) == 2


def test_new_pools_retries_once_on_429_then_gives_up(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp({}, status=429, headers={"Retry-After": "0"})

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fake_get)
    monkeypatch.setattr("src.agent.copy_trade.launch_collector.time.sleep",
                        lambda s: None)
    assert new_pools(pages=1) == []
    assert len(calls) == 2          # first try + one retry, then yield


def test_new_pools_returns_empty_on_network_error_never_raises(monkeypatch):
    def boom(url, **kw):
        raise ConnectionError("down")

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", boom)
    monkeypatch.setattr("src.agent.copy_trade.launch_collector.time.sleep",
                        lambda s: None)
    assert new_pools(pages=2) == []


# ---------- T4: batch fetchers + demux (highest-risk: a demux bug silently
# attributes one token's film to another, poisoning the dataset without error) ----------

from src.agent.copy_trade.launch_collector import (  # noqa: E402
    DEX_BATCH, GOPLUS_BATCH, fetch_pairs_batch, goplus_batch,
)

CHECKSUMMED = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"


def _ds_pair(base, quote="0x" + "b" * 40, liq=1000.0):
    return {"chainId": "bsc", "baseToken": {"address": base},
            "quoteToken": {"address": quote}, "priceUsd": "1.0",
            "liquidity": {"usd": liq}}


def test_fetch_pairs_batch_demux_is_case_insensitive(monkeypatch):
    # Probed live 2026-07-30: DexScreener echoes CHECKSUMMED addresses even
    # though we asked in lowercase. A case-sensitive lookup silently returns
    # nothing for every token.
    monkeypatch.setattr(
        "src.agent.copy_trade.launch_collector.requests.get",
        lambda url, **kw: FakeResp({"pairs": [_ds_pair(CHECKSUMMED)]}))
    out = fetch_pairs_batch([CHECKSUMMED.lower()])
    assert out[CHECKSUMMED.lower()], "checksummed response must map back to the lowercase key"


def test_fetch_pairs_batch_ignores_pairs_where_token_is_only_the_quote(monkeypatch):
    # Probed live: asking for N tokens returns pairs where a requested token sits
    # in quoteToken. Treating those as the token's own pair would read the WRONG
    # side's price and liquidity into its film.
    monkeypatch.setattr(
        "src.agent.copy_trade.launch_collector.requests.get",
        lambda url, **kw: FakeResp({"pairs": [_ds_pair(base="0x" + "f" * 40,
                                                       quote=T1)]}))
    assert fetch_pairs_batch([T1]) == {T1: []}


def test_fetch_pairs_batch_token_with_no_pairs_maps_to_empty_not_missing(monkeypatch):
    monkeypatch.setattr(
        "src.agent.copy_trade.launch_collector.requests.get",
        lambda url, **kw: FakeResp({"pairs": [_ds_pair(T1)]}))
    out = fetch_pairs_batch([T1, T2])
    assert out[T1] and out[T2] == []          # T2 present as [], never a KeyError


def test_fetch_pairs_batch_keeps_only_bsc_pairs(monkeypatch):
    eth = dict(_ds_pair(T1), chainId="ethereum")
    monkeypatch.setattr(
        "src.agent.copy_trade.launch_collector.requests.get",
        lambda url, **kw: FakeResp({"pairs": [eth]}))
    assert fetch_pairs_batch([T1]) == {T1: []}


def test_fetch_pairs_batch_chunks_at_the_documented_limit(monkeypatch):
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        return FakeResp({"pairs": []})

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fake_get)
    tokens = [f"0x{i:040x}" for i in range(DEX_BATCH + 1)]
    fetch_pairs_batch(tokens)
    assert len(urls) == 2
    assert urls[0].rsplit("/", 1)[1].count(",") == DEX_BATCH - 1   # DEX_BATCH addrs
    assert urls[1].rsplit("/", 1)[1].count(",") == 0               # the 1 leftover


def test_goplus_batch_demux_and_missing_records(monkeypatch):
    # Probed live 2026-07-30: GoPlus keys are lowercase, and it returns a record
    # for only a fraction of brand-new tokens (1 of 5 in the probe) because it
    # analyses asynchronously. A token with no record must be absent, not faked.
    monkeypatch.setattr(
        "src.agent.copy_trade.launch_collector.requests.get",
        lambda url, **kw: FakeResp({"result": {T1: {"is_mintable": "0"}}}))
    out = goplus_batch([CHECKSUMMED, T1])
    assert out[T1] == {"is_mintable": "0"}
    assert CHECKSUMMED.lower() not in out


def test_goplus_batch_chunks_at_the_documented_limit(monkeypatch):
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        return FakeResp({"result": {}})

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fake_get)
    monkeypatch.setattr("src.agent.copy_trade.launch_collector.time.sleep", lambda s: None)
    goplus_batch([f"0x{i:040x}" for i in range(GOPLUS_BATCH + 1)])
    assert len(urls) == 2


def test_batch_fetchers_yield_on_failure_instead_of_raising(monkeypatch):
    def boom(url, **kw):
        raise ConnectionError("down")

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", boom)
    monkeypatch.setattr("src.agent.copy_trade.launch_collector.time.sleep", lambda s: None)
    assert fetch_pairs_batch([T1]) == {T1: []}
    assert goplus_batch([T1]) == {}


def test_fetch_pairs_batch_empty_input_makes_no_calls(monkeypatch):
    def fail(url, **kw):
        raise AssertionError("should not call the network for an empty batch")

    monkeypatch.setattr("src.agent.copy_trade.launch_collector.requests.get", fail)
    assert fetch_pairs_batch([]) == {}
    assert goplus_batch([]) == {}


# ---------- T5: row builders ----------

from src.agent.copy_trade.launch_collector import (  # noqa: E402
    goplus_complete, launch_snapshot, sample_row, socials_of,
)

_SEVEN = ["is_mintable", "can_take_back_ownership", "hidden_owner",
          "owner_change_balance", "transfer_pausable", "slippage_modifiable",
          "is_proxy"]
_CLEAN_GP = {f: "0" for f in _SEVEN}


def _rich_pair(base=T1, liq=41_000.0, info=None):
    p = _ds_pair(base, liq=liq)
    p.update({"pairAddress": "0xpair", "pairCreatedAt": 1_784_998_000_000,
              "marketCap": 250_000, "priceUsd": "0.00012",
              "baseToken": {"address": base, "symbol": "CZ", "name": "CZ"},
              "txns": {"m5": {"buys": 41, "sells": 9},
                       "h1": {"buys": 388, "sells": 102}},
              "volume": {"m5": 18_400.0, "h1": 210_000.0, "h6": 0.0, "h24": 0.0},
              "priceChange": {"m5": 6.2, "h1": 58.0}})
    if info is not None:
        p["info"] = info
    return p


def test_socials_of_extracts_types_and_urls_and_tolerates_absence():
    inf = {"socials": [{"type": "twitter", "url": "https://x.com/a"}],
           "websites": [{"label": "Site", "url": "https://a.io"}]}
    got = socials_of(_rich_pair(info=inf))
    assert got["socials"] == [{"type": "twitter", "url": "https://x.com/a"}]
    assert got["websites"] == [{"label": "Site", "url": "https://a.io"}]
    assert got["info_present"] is True
    # Probed live: 4 of 5 brand-new tokens have NO info block at all.
    bare = socials_of(_rich_pair())
    assert bare == {"socials": [], "websites": [], "info_present": False}


def test_goplus_complete_is_false_when_a_gated_flag_is_unanalysed():
    assert goplus_complete(_CLEAN_GP) is True
    assert goplus_complete({**_CLEAN_GP, "is_mintable": ""}) is False
    assert goplus_complete({}) is False
    assert goplus_complete(None) is False


def test_launch_snapshot_captures_what_dies_with_the_token():
    inf = {"socials": [{"type": "twitter", "url": "https://x.com/cz"}], "websites": []}
    snap = launch_snapshot(
        pool={"pool_address": "0xpool", "token_address": T1, "name": "CZ / WBNB",
              "created_ts": 1_784_998_000.0, "reserve_usd": 41_000.0},
        pairs=[_rich_pair(info=inf)], goplus=_CLEAN_GP, now=1_785_000_000.0)
    assert snap["event"] == "launch"
    assert snap["token_address"] == T1
    assert snap["symbol"] == "CZ"
    assert snap["pool_age_s_at_arm"] == 2000.0
    assert snap["arm_liquidity_usd"] == 41_000.0
    assert snap["socials"][0]["type"] == "twitter"
    assert snap["goplus"] == _CLEAN_GP          # stored RAW and whole
    assert snap["goplus_complete"] is True


def test_launch_snapshot_records_null_goplus_rather_than_faking_clean():
    # The common case for a fresh token — must be distinguishable from "clean".
    snap = launch_snapshot(
        pool={"pool_address": "0xp", "token_address": T1, "name": "x",
              "created_ts": 1.0, "reserve_usd": 21_000.0},
        pairs=[_rich_pair()], goplus=None, now=2.0)
    assert snap["goplus"] is None
    assert snap["goplus_complete"] is False


def test_sample_row_carries_volume_which_the_old_bot_never_recorded():
    row = sample_row(T1, [_rich_pair()], holders=None, now=1_785_000_000.0,
                     armed_at=1_784_998_000.0)
    assert row["age_s"] == 2000.0
    assert (row["buys_m5"], row["sells_m5"]) == (41, 9)
    assert (row["vol_m5"], row["vol_h1"]) == (18_400.0, 210_000.0)
    assert row["price"] == 0.00012
    assert row["holders"] is None and row["top_pct"] is None


def test_sample_row_merges_holder_stats_when_present():
    row = sample_row(T1, [_rich_pair()],
                     holders={"holder_count": 412, "top_pct": 0.061,
                              "top5_pct": 0.184},
                     now=1.0, armed_at=0.0)
    assert (row["holders"], row["top_pct"], row["top5_pct"]) == (412, 0.061, 0.184)


def test_sample_row_returns_none_when_the_token_has_no_pairs():
    assert sample_row(T1, [], holders=None, now=1.0, armed_at=0.0) is None


# ---------- T6/T7/T8: loops, entrypoint, and the no-trading guard ----------

import json as _json  # noqa: E402
from pathlib import Path  # noqa: E402

import src.agent.copy_trade.launch_collector as lc  # noqa: E402
from src.agent.copy_trade.watchlist import Watchlist  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_collector_module_state():
    """The change-detection dicts are module-level, so without this a token
    filmed in one test suppresses the info/security rows of the next one —
    which is exactly how the security-row test first failed. Same pattern the
    prices tests use for _holder_cache.
    """
    lc._last_info.clear()
    lc._last_goplus.clear()
    lc.STATS["arms_skipped_cap"] = 0
    yield
    lc._last_info.clear()
    lc._last_goplus.clear()


def _collector(tmp_path):
    """A Watchlist wired to a temp films file, as run_collector builds it."""
    return Watchlist(tmp_path / "films.jsonl", max_dossiers=lc.MAX_FILMS,
                     max_age_s=lc.FILM_MAX_AGE_S)


def _read(path):
    if not Path(path).exists():
        return []
    text = Path(path).read_text(encoding="utf-8")
    return [_json.loads(line) for line in text.splitlines() if line]


def test_discover_and_arm_writes_one_launch_row_per_arm(tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    snap = tmp_path / "snapshots.jsonl"
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: [
        {"pool_address": "0xp", "token_address": T1, "name": "CZ / WBNB",
         "created_ts": NOW - 1800, "reserve_usd": 41_000.0}])
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {T1: _CLEAN_GP})

    seen = set()
    armed = lc.discover_and_arm(wl, seen, snapshots_path=snap, now=NOW)
    assert armed == 1
    assert seen == {T1}
    rows = _read(snap)
    assert [r["event"] for r in rows] == ["launch"]
    assert rows[0]["token_address"] == T1
    # The film arm row and the snapshot must name the SAME token — a mismatch
    # here is the demux bug reaching the dataset.
    films = _read(tmp_path / "films.jsonl")
    assert films[0]["event"] == "arm" and films[0]["token_address"] == T1


def test_discover_and_arm_respects_the_cap_and_counts_the_miss(tmp_path, monkeypatch):
    wl = Watchlist(tmp_path / "films.jsonl", max_dossiers=1,
                   max_age_s=lc.FILM_MAX_AGE_S)
    pools = [{"pool_address": f"0xp{i}", "token_address": f"0x{i:040x}",
              "name": "x", "created_ts": NOW - 1800, "reserve_usd": 41_000.0}
             for i in range(3)]
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: pools)
    monkeypatch.setattr(lc, "fetch_pairs_batch",
                        lambda t: {tok: [_rich_pair(base=tok)] for tok in t})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})

    lc.STATS["arms_skipped_cap"] = 0
    armed = lc.discover_and_arm(wl, set(), snapshots_path=tmp_path / "s.jsonl", now=NOW)
    assert armed == 1                          # only one slot
    # Sample bias is the biggest threat to this dataset, so the miss rate is
    # itself data and must be visible, not silently dropped.
    assert lc.STATS["arms_skipped_cap"] == 2


def test_discover_and_arm_skips_a_pool_dexscreener_says_is_empty(tmp_path,
                                                                 monkeypatch):
    """GeckoTerminal's reserve got 4 of 29 live arms (2026-07-30) through the
    filter while DexScreener reported $0.01 for the same pool at the same
    moment; all 4 read zero for their entire film. Filming an empty pool burns a
    slot and four hours of quota to record zeros."""
    wl = _collector(tmp_path)
    snap = tmp_path / "snapshots.jsonl"
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: [
        {"pool_address": "0xp", "token_address": T1, "name": "x",
         "created_ts": NOW - 1800, "reserve_usd": 41_000.0}])
    monkeypatch.setattr(lc, "fetch_pairs_batch",
                        lambda t: {T1: [_rich_pair(liq=0.01)]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})

    lc.STATS["arms_skipped_empty_pool"] = 0
    assert lc.discover_and_arm(wl, set(), snap, NOW) == 0
    assert lc.STATS["arms_skipped_empty_pool"] == 1
    assert _read(snap) == []


def test_discover_and_arm_still_trusts_geckoterminal_when_dexscreener_is_silent(
        tmp_path, monkeypatch):
    """No pairs at all means DexScreener has not indexed the token yet — the
    normal early state, not an empty pool. Dropping those would throw away good
    launches, so GeckoTerminal's reserve stands and the arm goes ahead."""
    wl = _collector(tmp_path)
    snap = tmp_path / "snapshots.jsonl"
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: [
        {"pool_address": "0xp", "token_address": T1, "name": "x",
         "created_ts": NOW - 1800, "reserve_usd": 41_000.0}])
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: []})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})

    assert lc.discover_and_arm(wl, set(), snap, NOW) == 1
    row = _read(snap)[0]
    # Both liquidity readings are on the record: the filter gated on the GT one.
    assert row["arm_reserve_usd_gt"] == 41_000.0
    assert row["arm_liquidity_usd"] == 41_000.0


def test_discover_and_arm_never_double_arms_a_token(tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    pool = {"pool_address": "0xp", "token_address": T1, "name": "x",
            "created_ts": NOW - 1800, "reserve_usd": 41_000.0}
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: [pool])
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})
    seen = set()
    assert lc.discover_and_arm(wl, seen, tmp_path / "s.jsonl", NOW) == 1
    assert lc.discover_and_arm(wl, seen, tmp_path / "s.jsonl", NOW + 60) == 0


def test_sample_tick_calls_goplus_only_every_nth_tick(tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    wl.arm(T1, wallet="pool", price=1.0, liquidity=41_000.0, now=NOW)
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    gp_calls = []

    def fake_goplus(tokens):
        gp_calls.append(tokens)
        return {T1: _CLEAN_GP}

    monkeypatch.setattr(lc, "goplus_batch", fake_goplus)

    lc.sample_tick(wl, tick=1, snapshots_path=tmp_path / "s.jsonl", now=NOW + 30)
    assert gp_calls == []
    lc.sample_tick(wl, tick=lc.HOLDER_SAMPLE_EVERY_N,
                   snapshots_path=tmp_path / "s.jsonl", now=NOW + 60)
    assert len(gp_calls) == 1


def test_sample_tick_still_records_price_when_goplus_fails(tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    wl.arm(T1, wallet="pool", price=1.0, liquidity=41_000.0, now=NOW)
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})   # nothing analysed yet
    n = lc.sample_tick(wl, tick=lc.HOLDER_SAMPLE_EVERY_N,
                       snapshots_path=tmp_path / "s.jsonl", now=NOW + 30)
    assert n == 1
    samples = [r for r in _read(tmp_path / "films.jsonl") if r["event"] == "sample"]
    assert samples[0]["price"] == 0.00012 and samples[0]["holders"] is None


def test_sample_tick_appends_info_row_only_when_socials_change(tmp_path, monkeypatch):
    # The whole point of the collector: "Twitter appeared at T+38min" is a signal
    # that retrospective research literally cannot see.
    wl = _collector(tmp_path)
    wl.arm(T1, wallet="pool", price=1.0, liquidity=41_000.0, now=NOW)
    snap = tmp_path / "s.jsonl"
    inf = {"socials": [{"type": "twitter", "url": "https://x.com/a"}], "websites": []}

    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})
    lc.sample_tick(wl, tick=1, snapshots_path=snap, now=NOW + 30)
    assert _read(snap) == []                       # no info block yet, no row

    monkeypatch.setattr(lc, "fetch_pairs_batch",
                        lambda t: {T1: [_rich_pair(info=inf)]})
    lc.sample_tick(wl, tick=2, snapshots_path=snap, now=NOW + 60)
    rows = _read(snap)
    assert [r["event"] for r in rows] == ["info"]
    assert rows[0]["socials"][0]["type"] == "twitter"

    lc.sample_tick(wl, tick=3, snapshots_path=snap, now=NOW + 90)
    assert len(_read(snap)) == 1                   # unchanged -> no duplicate row


def test_sample_tick_appends_security_row_when_goplus_finishes_analysing(
        tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    wl.arm(T1, wallet="pool", price=1.0, liquidity=41_000.0, now=NOW)
    snap = tmp_path / "s.jsonl"
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})
    lc.sample_tick(wl, tick=lc.HOLDER_SAMPLE_EVERY_N, snapshots_path=snap,
                   now=NOW + 30)
    assert _read(snap) == []

    monkeypatch.setattr(lc, "goplus_batch", lambda t: {T1: _CLEAN_GP})
    lc.sample_tick(wl, tick=lc.HOLDER_SAMPLE_EVERY_N * 2, snapshots_path=snap,
                   now=NOW + 60)
    rows = _read(snap)
    assert [r["event"] for r in rows] == ["security"]
    assert rows[0]["goplus_complete"] is True


def test_sample_tick_expires_films_past_the_window(tmp_path, monkeypatch):
    wl = _collector(tmp_path)
    wl.arm(T1, wallet="pool", price=1.0, liquidity=41_000.0, now=NOW)
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {})
    lc.sample_tick(wl, tick=1, snapshots_path=tmp_path / "s.jsonl",
                   now=NOW + lc.FILM_MAX_AGE_S + 1)
    assert wl.active() == []
    assert any(r["event"] == "disarm" for r in _read(tmp_path / "films.jsonl"))


def test_run_collector_once_creates_every_output(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "FILMS_PATH", tmp_path / "films.jsonl")
    monkeypatch.setattr(lc, "SNAPSHOTS_PATH", tmp_path / "snapshots.jsonl")
    monkeypatch.setattr(lc, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(lc, "new_pools", lambda pages=4: [
        {"pool_address": "0xp", "token_address": T1, "name": "CZ / WBNB",
         "created_ts": lc.time.time() - 1800, "reserve_usd": 41_000.0}])
    monkeypatch.setattr(lc, "fetch_pairs_batch", lambda t: {T1: [_rich_pair()]})
    monkeypatch.setattr(lc, "goplus_batch", lambda t: {T1: _CLEAN_GP})

    lc.run_collector(once=True)
    assert [r["event"] for r in _read(tmp_path / "snapshots.jsonl")] == ["launch"]
    assert {r["event"] for r in _read(tmp_path / "films.jsonl")} == {"arm", "sample"}
    state = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["active_films"] == 1 and "last_tick_at" in state


def test_collector_module_never_imports_the_trading_stack():
    # "Must not trade" is a safety property, and safety properties get a test
    # rather than a code review. Parsed from the AST rather than grepped as
    # text, so the module is free to EXPLAIN in prose why it avoids the trading
    # stack without the guard mistaking the explanation for a violation.
    import ast

    tree = ast.parse(Path(lc.__file__).read_text(encoding="utf-8"))
    banned_modules = {"trade_engine", "positions", "budget", "monitor"}
    banned_names = {"TradeEngine", "PositionStore", "CopyTradeBudget"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            leaf = (node.module or "").rsplit(".", 1)[-1]
            assert leaf not in banned_modules, f"must not import from {node.module}"
            for alias in node.names:
                assert alias.name not in banned_names, f"must not import {alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.rsplit(".", 1)[-1] not in banned_modules, \
                    f"must not import {alias.name}"
