import pytest

from src.agent.copy_trade import prices

TOKEN = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def _clear_price_cache():
    prices._price_cache.clear()


class FakeResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


def _bsc_payload(price="3.0"):
    return {"pairs": [{"chainId": "bsc", "priceUsd": price,
                       "liquidity": {"usd": 900}}]}


def test_get_price_cached_within_ttl(monkeypatch):
    calls = []
    def counting_get(url, timeout=None):
        calls.append(url)
        return FakeResp(_bsc_payload())
    monkeypatch.setattr(prices.requests, "get", counting_get)
    assert prices.get_price_usd(TOKEN) == 3.0
    assert prices.get_price_usd(TOKEN) == 3.0   # served from cache
    assert prices.get_price_usd(TOKEN.upper()) == 3.0  # case-insensitive key
    assert len(calls) == 1


def test_get_price_refetches_after_ttl_expiry(monkeypatch):
    calls = []
    def counting_get(url, timeout=None):
        calls.append(url)
        return FakeResp(_bsc_payload(price=str(len(calls))))
    monkeypatch.setattr(prices.requests, "get", counting_get)
    assert prices.get_price_usd(TOKEN) == 1.0
    fetched_at, price = prices._price_cache[TOKEN]
    prices._price_cache[TOKEN] = (fetched_at - prices._PRICE_TTL_S - 1, price)
    assert prices.get_price_usd(TOKEN) == 2.0   # stale entry refetched
    assert len(calls) == 2


def test_get_price_failure_is_not_cached(monkeypatch):
    def boom(url, timeout=None):
        raise ConnectionError()
    monkeypatch.setattr(prices.requests, "get", boom)
    assert prices.get_price_usd(TOKEN) is None
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(_bsc_payload()))
    assert prices.get_price_usd(TOKEN) == 3.0   # recovers immediately


def test_get_price_picks_highest_liquidity_bsc_pair(monkeypatch):
    payload = {"pairs": [
        {"chainId": "bsc", "priceUsd": "2.0", "liquidity": {"usd": 100}},
        {"chainId": "bsc", "priceUsd": "3.0", "liquidity": {"usd": 900}},
        {"chainId": "ethereum", "priceUsd": "9.9", "liquidity": {"usd": 99999}},
    ]}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_price_usd(TOKEN) == 3.0


def test_get_price_none_on_failure(monkeypatch):
    def boom(url, timeout=None):
        raise ConnectionError()
    monkeypatch.setattr(prices.requests, "get", boom)
    assert prices.get_price_usd(TOKEN) is None


def test_get_taxes_parses_goplus(monkeypatch):
    payload = {"result": {TOKEN: {"buy_tax": "0.04", "sell_tax": "0.05"}}}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_taxes(TOKEN) == (0.04, 0.05)


def test_get_taxes_none_on_missing(monkeypatch):
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp({"result": {}}))
    assert prices.get_taxes(TOKEN) is None


def test_get_taxes_none_on_empty_tax_fields(monkeypatch):
    payload = {"result": {TOKEN: {"buy_tax": "", "sell_tax": ""}}}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_taxes(TOKEN) is None


def test_get_taxes_none_on_missing_tax_keys(monkeypatch):
    payload = {"result": {TOKEN: {"is_honeypot": "0"}}}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_taxes(TOKEN) is None


def test_get_taxes_zero_is_legitimate(monkeypatch):
    payload = {"result": {TOKEN: {"buy_tax": "0", "sell_tax": "0"}}}
    monkeypatch.setattr(prices.requests, "get",
                        lambda url, timeout=None: FakeResp(payload))
    assert prices.get_taxes(TOKEN) == (0.0, 0.0)
