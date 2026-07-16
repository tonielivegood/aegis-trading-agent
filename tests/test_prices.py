from src.agent.copy_trade import prices

TOKEN = "0x" + "a" * 40


class FakeResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


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
