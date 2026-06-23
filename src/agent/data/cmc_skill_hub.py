"""CoinMarketCap **Agent Hub — MCP Skill Hub** client.

Beyond the two REST sentiment endpoints in `cmc_agent_hub.py`, the CMC Agent Hub exposes
a MARKETPLACE of agent SKILLS over an MCP server (https://mcp.coinmarketcap.com/mcp). This
module lets our agent discover and CALL those skills — e.g. `trending_crypto_narratives`,
`get_crypto_technical_analysis`, `get_upcoming_macro_events` — with our existing CMC Pro key
(passed as `X-CMC-MCP-API-KEY`; CMC documents four Agent-Hub access paths and the key is shared).

Stateless JSON-RPC 2.0 over HTTP: one POST per call, no session handshake required. Every call
is FAIL-SAFE — a network/parse/HTTP error returns ``None`` and is logged, never raised — so this
is safe to call from a diagnostic or, later, the hourly (out-of-hot-path) signal refresh.
"""
from __future__ import annotations

import json

import requests

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

MCP_URL = "https://mcp.coinmarketcap.com/mcp"
_TIMEOUT = 30.0


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-CMC-MCP-API-KEY": settings.cmc_api_key,
    }


def _post(body: dict, *, timeout: float = _TIMEOUT) -> dict | None:
    """POST a JSON-RPC request and return the parsed JSON-RPC envelope (or None)."""
    r = requests.post(MCP_URL, headers=_headers(), json=body, timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:                 # SSE framing: pull the data: line
        for line in r.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    return r.json()


def list_skills() -> list[str]:
    """Names of the Agent Hub skills available to us (empty list on any error)."""
    try:
        env = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = (env or {}).get("result", {}).get("tools", [])
        return [t["name"] for t in tools if t.get("name")]
    except Exception as e:  # noqa: BLE001 — fail safe
        log.info("cmc_skill_list_failed", error=type(e).__name__)
        return []


def call_skill(name: str, arguments: dict | None = None) -> dict | list | None:
    """Execute one Agent Hub skill and return its parsed result payload.

    The MCP envelope is result.content[]; the skill's data is a JSON string in the first
    text block, which we parse. Returns None on any error (caller treats as 'no data')."""
    try:
        env = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments or {}}})
        content = (env or {}).get("result", {}).get("content", [])
        for c in content:
            if c.get("type") == "text" and c.get("text"):
                try:
                    return json.loads(c["text"])
                except (ValueError, TypeError):
                    return {"text": c["text"]}
        return None
    except Exception as e:  # noqa: BLE001 — fail safe
        log.info("cmc_skill_call_failed", skill=name, error=type(e).__name__)
        return None


def trending_narratives(limit: int = 5) -> list[dict]:
    """Top trending crypto NARRATIVES via the Agent Hub skill, as tidy dicts:
    {rank, name, market_cap, change_24h, top_coins, social_keywords}. [] on error."""
    data = call_skill("trending_crypto_narratives")
    cat = (data or {}).get("categoryList") if isinstance(data, dict) else None
    if not cat or "headers" not in cat or "rows" not in cat:
        return []
    idx = {h: i for i, h in enumerate(cat["headers"])}

    def _get(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in cat["rows"][:limit]:
        out.append({
            "rank": _get(row, "trendingRank"),
            "name": _get(row, "categoryName"),
            "market_cap": _get(row, "marketCapUsd"),
            "change_24h": _get(row, "marketCapChangePercentage24h"),
            "top_coins": _coin_symbols(_get(row, "topCoinList")),
            "social_keywords": _get(row, "socialKeywords") or [],
        })
    return out


def upcoming_macro_events(limit: int = 6) -> list[dict]:
    """Upcoming macro/catalyst events via the Agent Hub `get_upcoming_macro_events` skill,
    as tidy dicts: {title, date_str, url}. The agent uses these to stand down into an
    imminent high-volatility catalyst (see aegis.macro_calendar). [] on any error."""
    data = call_skill("get_upcoming_macro_events")
    block = (data or {}).get("upcomingEventNews") if isinstance(data, dict) else None
    if not block or "headers" not in block or "rows" not in block:
        return []
    idx = {h: i for i, h in enumerate(block["headers"])}

    def _get(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in block["rows"][:limit]:
        title = _get(row, "title")
        if not title:
            continue
        out.append({"title": str(title), "date_str": _get(row, "eventDate") or "",
                    "url": _get(row, "url") or ""})
    return out


def _coin_symbols(v) -> list[str]:
    """topCoinList is a nested table {headers:[coinSymbol,...], rows:[[SYM,...]]}; pull the
    symbol column. Also tolerate a flat list of strings/dicts (shape can vary). [] on anything else."""
    if isinstance(v, dict) and isinstance(v.get("rows"), list):
        hdr = v.get("headers") or []
        si = hdr.index("coinSymbol") if "coinSymbol" in hdr else 0
        return [str(r[si]) for r in v["rows"] if isinstance(r, list) and len(r) > si]
    if isinstance(v, list):
        return [str(c.get("symbol") if isinstance(c, dict) else c) for c in v]
    return []
