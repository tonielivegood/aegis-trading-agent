from datetime import datetime, timezone

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
