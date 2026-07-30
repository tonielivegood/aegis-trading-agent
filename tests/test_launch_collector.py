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
