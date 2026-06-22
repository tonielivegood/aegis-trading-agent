"""TDD for the CMC Agent Hub MCP Skill Hub client — mocked HTTP, fail-safe."""
from __future__ import annotations

import json

from src.agent.data import cmc_skill_hub as sh


class _Resp:
    def __init__(self, payload, *, headers=None, raise_exc=None):
        self._p = payload
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._p


def _mock_post(mocker, payload, **kw):
    return mocker.patch("src.agent.data.cmc_skill_hub.requests.post",
                        return_value=_Resp(payload, **kw))


def test_list_skills(mocker):
    _mock_post(mocker, {"result": {"tools": [{"name": "trending_crypto_narratives"},
                                             {"name": "get_crypto_quotes_latest"}]}})
    assert sh.list_skills() == ["trending_crypto_narratives", "get_crypto_quotes_latest"]


def test_call_skill_parses_text_json(mocker):
    inner = {"categoryList": {"headers": ["x"], "rows": [[1]]}}
    _mock_post(mocker, {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]}})
    assert sh.call_skill("trending_crypto_narratives") == inner


def test_trending_narratives_maps_rows(mocker):
    inner = {"categoryList": {
        "headers": ["trendingRank", "categoryName", "marketCapUsd",
                    "marketCapChangePercentage24h", "topCoinList", "socialKeywords"],
        "rows": [[1, "Binance Ecosystem", "2.2 T", "-0.5%", ["BNB", "CAKE"], ["Binance"]],
                 [2, "AI", "100 B", "+3%", ["FET"], ["AI"]]]}}
    _mock_post(mocker, {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]}})
    out = sh.trending_narratives(limit=5)
    assert len(out) == 2
    assert out[0]["name"] == "Binance Ecosystem" and out[0]["top_coins"] == ["BNB", "CAKE"]
    assert out[1]["rank"] == 2


def test_fail_safe_on_http_error(mocker):
    _mock_post(mocker, {}, raise_exc=RuntimeError("boom"))
    assert sh.list_skills() == []
    assert sh.call_skill("x") is None
    assert sh.trending_narratives() == []


def test_sse_framed_response(mocker):
    inner = {"result": {"tools": [{"name": "get_crypto_metrics"}]}}
    sse = "event: message\ndata: " + json.dumps(inner) + "\n\n"
    r = _Resp(inner, headers={"content-type": "text/event-stream"})
    r.text = sse
    mocker.patch("src.agent.data.cmc_skill_hub.requests.post", return_value=r)
    assert sh.list_skills() == ["get_crypto_metrics"]
