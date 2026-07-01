#!/usr/bin/env python
"""Aegis Desk — a CMC Agent Hub multi-skill trading desk (special-prize showcase).

This is a STANDALONE, 100% READ-ONLY showcase for the **CMC Agent Hub special prize**
(#CMCAgentHub). It does NOT touch the live Track-1 bot, its wallet, or its strategy — it
only reads CMC Agent Hub skills and reasons over them.

THE IDEA — "CMC Agent Hub as a full trading desk." We don't call a single skill; we run
EIGHT CMC Agent Hub skills as a panel of specialist analysts, then have **Claude** (the
same reasoner the live bot uses as its risk officer) synthesise the eight reports into ONE
desk verdict — the exact read that would drive a real on-chain decision. Skills → analysts
→ one signed-off bias, live.

The eight analysts (all CMC Agent Hub skills):
  1. PRICE       get_crypto_quotes_latest            — price + multi-horizon momentum
  2. TECHNICALS  get_crypto_technical_analysis       — RSI / MACD / moving averages / Fib
  3. DERIVATIVES get_global_crypto_derivatives_metrics — open interest + FUNDING + liquidations
  4. WHALES      get_crypto_metrics                  — holder / whale supply distribution
  5. MACRO       get_global_metrics_latest           — total cap, dominance, Fear & Greed
  6. CALENDAR    get_upcoming_macro_events           — imminent high-vol catalysts
  7. NEWS        get_crypto_latest_news              — latest headlines for the asset
  8. NARRATIVES  trending_crypto_narratives          — what the market is rotating into

Every skill call is FAIL-SAFE (a missing analyst is reported as "no data", never crashes
the desk). The Claude synthesis is fail-safe too: with no key / on any error the desk still
prints all eight panels and a mechanical fallback verdict.

Usage:
    python scripts/agent_hub_desk.py                 # BTC desk
    python scripts/agent_hub_desk.py --symbol BNB
    python scripts/agent_hub_desk.py --symbol ETH --json
    python scripts/agent_hub_desk.py --symbol BNB --model claude-opus-4-8
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.getLogger("httpx").setLevel(logging.WARNING)  # quiet the per-call POST line for clean screenshots

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console safety

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent.config import settings                       # noqa: E402
from src.agent.data import cmc_skill_hub as hub             # noqa: E402

RUNTIME = ROOT / "data" / "runtime"


# ───────────────────────────── skill resolution ─────────────────────────────

def resolve_id(symbol: str) -> tuple[int | None, str]:
    """Resolve a ticker → (CMC numeric id, display name) via the search_cryptos skill.
    Returns (None, symbol) if it can't be resolved (caller degrades gracefully)."""
    rows = hub.call_skill("search_cryptos", {"query": symbol})
    if isinstance(rows, list) and rows:
        up = symbol.upper()
        exact = next((r for r in rows if str(r.get("symbol", "")).upper() == up), None)
        pick = exact or rows[0]
        cid = pick.get("id")
        return (int(cid) if cid is not None else None, str(pick.get("name") or symbol))
    return None, symbol


# ───────────────────────────── analyst panels ───────────────────────────────
# Each returns a small, display-ready dict; never raises (hub.call_skill is fail-safe,
# and we guard the parsing). A failed analyst returns {} and is shown as "no data".

def panel_price(cid: int) -> dict:
    q = hub.call_skill("get_crypto_quotes_latest", {"id": str(cid)})
    row = q[0] if isinstance(q, list) and q else (q if isinstance(q, dict) else None)
    if not isinstance(row, dict):
        return {}
    return {
        "price": row.get("price"),
        "rank": row.get("rank"),
        "chg_1h": row.get("percent_change_1h"),
        "chg_24h": row.get("percent_change_24h"),
        "chg_7d": row.get("percent_change_7d"),
        "chg_30d": row.get("percent_change_30d"),
        "vol_24h": row.get("volume_24h"),
        "vol_chg_24h": row.get("volume_change_24h"),
    }


def panel_technicals(cid: int) -> dict:
    d = hub.call_skill("get_crypto_technical_analysis", {"id": str(cid)})
    if not isinstance(d, dict) or "rsi" not in d:
        return {}
    rsi = d.get("rsi") or {}
    macd = d.get("macd") or {}
    ma = d.get("moving_averages") or {}
    return {
        "rsi14": rsi.get("rsi14"),
        "rsi7": rsi.get("rsi7"),
        "macd_hist": macd.get("histogram"),
        "macd_line": macd.get("macdLine"),
        "macd_signal": macd.get("signalLine"),
        "sma_7": ma.get("simple_moving_average_7_day"),
        "sma_30": ma.get("simple_moving_average_30_day"),
        "sma_200": ma.get("simple_moving_average_200_day"),
        "pivot": d.get("pivotPoint"),
    }


def panel_derivatives() -> dict:
    d = hub.call_skill("get_global_crypto_derivatives_metrics")
    if not isinstance(d, dict):
        return {}
    oi = d.get("totalOpenInterest") or {}
    fr = d.get("fundingRate") or {}
    liq = d.get("btc_liquidations") or {}
    liq24 = liq.get("total_usd_24h") or {}   # {total, long, short} forced-closure USD in 24h
    return {
        "open_interest": oi.get("current"),
        "oi_chg_24h": oi.get("percentage_change_24h"),
        "oi_chg_30d": oi.get("percentage_change_30d"),   # leverage buildup/unwind vs a month ago
        "funding_rate": fr.get("current"),
        "funding_chg_24h": fr.get("percentage_change_24h"),
        # long vs short liquidations: long >> short = longs being flushed (capitulation/long squeeze)
        "liq_total_24h": liq24.get("total"),
        "liq_long_24h": liq24.get("long"),
        "liq_short_24h": liq24.get("short"),
    }


def panel_whales(cid: int) -> dict:
    d = hub.call_skill("get_crypto_metrics", {"id": str(cid)})
    if not isinstance(d, dict) or "circulatingSupplyDistribution" not in d:
        return {}
    dist = d.get("circulatingSupplyDistribution") or {}
    hold = d.get("addressesByHoldingTime") or {}
    val = d.get("addressesByHoldingValue") or {}
    return {
        "whale_supply_pct": (dist.get("whales") or {}).get("percentOfSupply"),
        "holders_pct_addr": (hold.get("holders") or {}).get("percentOfAddresses"),
        "traders_pct_addr": (hold.get("traders") or {}).get("percentOfAddresses"),
        "usd100k_plus_pct": (val.get("usd100kPlus") or {}).get("percentOfAddresses"),
    }


def panel_macro() -> dict:
    d = hub.call_skill("get_global_metrics_latest")
    if not isinstance(d, dict):
        return {}
    cap = ((d.get("market_size") or {}).get("total_crypto_market_cap_usd") or {})
    pc = cap.get("percent_change") or {}
    # sentiment.fear_greed.current = {"value": "<label>", "index": <0-100>}
    fg_cur = (((d.get("sentiment") or {}).get("fear_greed") or {}).get("current") or {})
    # dominance.btc.current = "+58.35%"
    btc_dom = ((d.get("dominance") or {}).get("btc") or {}).get("current")
    # rotation.altcoin_season.current.index = 0-100 (rising = alts outperform, falling = back to BTC/cash)
    alt = (d.get("rotation") or {}).get("altcoin_season") or {}
    # trad_fi_flows.etf_aum.btc.current = spot-BTC-ETF AUM (institutional demand proxy)
    btc_etf = ((d.get("trad_fi_flows") or {}).get("etf_aum") or {}).get("btc") or {}
    return {
        "total_mcap": cap.get("current"),
        "mcap_chg_24h": pc.get("24h"),
        "mcap_chg_7d": pc.get("7d"),
        "fear_greed": fg_cur.get("index"),
        "fear_greed_label": fg_cur.get("value"),
        "btc_dominance": btc_dom,
        "altseason_index": (alt.get("current") or {}).get("index"),
        "altseason_chg_24h": (alt.get("percent_change") or {}).get("24h"),
        "btc_etf_aum": btc_etf.get("current"),
        "btc_etf_aum_prev_week": (btc_etf.get("history") or {}).get("last_week"),
        "last_updated": d.get("last_updated"),
    }


def panel_market_ta() -> dict:
    """Total-market-cap technical read (RSI/MACD/pivot) — is the WHOLE market over-
    bought/oversold, beyond the single asset? Complements per-asset technicals. {} on error."""
    d = hub.call_skill("get_crypto_marketcap_technical_analysis")
    if not isinstance(d, dict) or "rsi" not in d:
        return {}
    rsi = d.get("rsi") or {}
    macd = d.get("macd") or {}
    return {
        "mcap": d.get("currentMarketCap"),
        "rsi14": rsi.get("rsi14"),
        "macd_hist": macd.get("histogram"),
        "pivot": d.get("pivotPoint"),
    }


def panel_calendar(limit: int = 5) -> list[dict]:
    try:
        return hub.upcoming_macro_events(limit=limit)
    except Exception:  # noqa: BLE001 — fail safe
        return []


def panel_news(cid: int, limit: int = 4) -> list[dict]:
    d = hub.call_skill("get_crypto_latest_news", {"id": str(cid)})
    if not isinstance(d, dict) or "rows" not in d:
        return []
    idx = {h: i for i, h in enumerate(d.get("headers") or [])}

    def _g(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in (d.get("rows") or [])[:limit]:
        title = _g(row, "title")
        if title:
            clean = " ".join(str(title).split())[:160]  # collapse embedded newlines/runs of space
            out.append({"title": clean, "published": _g(row, "publishedAt") or ""})
    return out


def panel_narratives(limit: int = 5) -> list[dict]:
    try:
        rows = hub.trending_narratives(limit=limit * 2)  # over-fetch, then de-dup by name
    except Exception:  # noqa: BLE001 — fail safe
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for n in rows:
        name = n.get("name")
        if name and name not in seen:
            seen.add(name)
            out.append(n)
        if len(out) >= limit:
            break
    return out


def gather(symbol: str) -> dict:
    """Run all nine CMC Agent Hub analysts for `symbol`. Read-only, fail-safe."""
    cid, name = resolve_id(symbol)
    desk = {
        "symbol": symbol.upper(),
        "name": name,
        "cmc_id": cid,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "macro": panel_macro(),
        "market_ta": panel_market_ta(),
        "derivatives": panel_derivatives(),
        "calendar": panel_calendar(),
        "narratives": panel_narratives(),
    }
    if cid is not None:
        desk["price"] = panel_price(cid)
        desk["technicals"] = panel_technicals(cid)
        desk["whales"] = panel_whales(cid)
        desk["news"] = panel_news(cid)
    else:
        desk["price"] = desk["technicals"] = desk["whales"] = {}
        desk["news"] = []
    desk["analysts_online"] = sum(
        1 for k in ("price", "technicals", "market_ta", "derivatives", "whales", "macro")
        if desk.get(k)
    ) + sum(1 for k in ("calendar", "news", "narratives") if desk.get(k))
    return desk


# ───────────────────────────── Claude synthesis ─────────────────────────────

_SYSTEM = (
    "You are the RISK OFFICER of an autonomous, LONG-ONLY SPOT trading desk on BNB Chain. "
    "The desk can only do two things: DEPLOY capital into spot alts, or hold STABLECOIN "
    "CASH — it cannot short or use leverage. Nine specialist analysts (price action, "
    "technicals, market-wide technicals, derivatives, whale flows, macro, the catalyst "
    "calendar, news, and trending narratives) have each filed a one-line report from "
    "CoinMarketCap's Agent Hub. "
    "Synthesise ALL of them into a single risk posture for the spot book. Weigh confirmation "
    "across panels; call out where they disagree. Be concrete and decisive, not wishy-washy. "
    "Output EXACTLY this format:\n"
    "Line 1: `POSTURE | CONVICTION` where POSTURE is one of RISK_ON (deploy into alts), "
    "NEUTRAL (selective / trim exposure), RISK_OFF (rotate to stablecoin cash) and "
    "CONVICTION is an integer 0-100.\n"
    "Line 2: a single-sentence thesis for the spot book (deploy / trim / sit in cash — never short).\n"
    "Line 3: `KEY RISK:` then the one signal that would flip this posture.\n"
    "Output nothing else."
)


def _digest(desk: dict) -> str:
    """Compact, model-friendly digest of the nine analyst panels."""
    def f(panel: dict, *fields) -> str:
        bits = [f"{k}={panel.get(k)}" for k in fields if panel.get(k) is not None]
        return ", ".join(bits) if bits else "no data"

    p, t, dv = desk.get("price", {}), desk.get("technicals", {}), desk.get("derivatives", {})
    w, m, mt = desk.get("whales", {}), desk.get("macro", {}), desk.get("market_ta", {})
    narr = "; ".join(f"{n['name']} ({n.get('change_24h')})" for n in desk.get("narratives", [])[:5]) or "no data"
    cal = "; ".join(f"{e['title']} [{e['date_str']}]" for e in desk.get("calendar", [])[:4]) or "none imminent"
    news = "; ".join(n["title"] for n in desk.get("news", [])[:4]) or "no data"
    return (
        f"ASSET: {desk['name']} ({desk['symbol']})\n"
        f"1. PRICE: {f(p, 'price', 'chg_1h', 'chg_24h', 'chg_7d', 'chg_30d', 'vol_chg_24h')}\n"
        f"2. TECHNICALS: {f(t, 'rsi14', 'macd_hist', 'sma_7', 'sma_30', 'sma_200', 'pivot')}\n"
        f"3. DERIVATIVES: {f(dv, 'open_interest', 'oi_chg_24h', 'oi_chg_30d', 'funding_rate', 'funding_chg_24h', 'liq_long_24h', 'liq_short_24h')}\n"
        f"4. WHALES: {f(w, 'whale_supply_pct', 'holders_pct_addr', 'traders_pct_addr')}\n"
        f"5. MACRO: {f(m, 'total_mcap', 'mcap_chg_24h', 'fear_greed', 'fear_greed_label', 'btc_dominance', 'altseason_index', 'altseason_chg_24h', 'btc_etf_aum')}\n"
        f"6. MARKET TECHNICALS (total mcap): {f(mt, 'mcap', 'rsi14', 'macd_hist', 'pivot')}\n"
        f"7. CALENDAR: {cal}\n"
        f"8. NEWS: {news}\n"
        f"9. NARRATIVES: {narr}\n"
    )


def synthesize(desk: dict, model: str | None) -> dict:
    """Claude reads the nine panels → desk verdict. Fail-safe: returns a mechanical
    fallback verdict (and source='fallback') if no key / on any error."""
    digest = _digest(desk)
    if not settings.anthropic_api_key:
        return {**_fallback(desk), "source": "fallback", "digest": digest}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                     timeout=40.0, max_retries=1)
        resp = client.messages.create(
            model=model or settings.anthropic_model, max_tokens=220,
            system=_SYSTEM, messages=[{"role": "user", "content": digest}])
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        bias, conviction = _parse_header(text)
        return {"source": "claude", "model": model or settings.anthropic_model,
                "bias": bias, "conviction": conviction, "text": text, "digest": digest}
    except Exception as e:  # noqa: BLE001 — showcase must still print without Claude
        return {**_fallback(desk), "source": f"fallback ({type(e).__name__})", "digest": digest}


def _parse_header(text: str) -> tuple[str, int | None]:
    """Pull BIAS and CONVICTION from the first line `BIAS | CONVICTION`."""
    first = (text.splitlines() or [""])[0].upper()
    bias = next((b for b in ("RISK_ON", "RISK_OFF", "NEUTRAL") if b in first), "NEUTRAL")
    conv = None
    for tok in first.replace("|", " ").split():
        if tok.isdigit():
            conv = max(0, min(100, int(tok)))
            break
    return bias, conv


def _fallback(desk: dict) -> dict:
    """Mechanical verdict when Claude is unavailable: blend 24h momentum + Fear & Greed
    so the showcase still produces a defensible call."""
    p = desk.get("price", {})
    chg24 = _num(p.get("chg_24h"))
    fg = _num((desk.get("macro") or {}).get("fear_greed"))
    score = 0
    if chg24 is not None:
        score += 1 if chg24 > 1 else (-1 if chg24 < -1 else 0)
    if fg is not None:
        score += 1 if fg >= 60 else (-1 if fg <= 25 else 0)
    bias = "RISK_ON" if score >= 1 else ("RISK_OFF" if score <= -1 else "NEUTRAL")
    conv = 50 + 15 * score
    text = (f"{bias} | {max(0, min(100, conv))}\n"
            f"Mechanical blend of 24h momentum and Fear & Greed (Claude offline).\n"
            f"KEY RISK: a sharp reversal in BTC 24h momentum.")
    return {"bias": bias, "conviction": max(0, min(100, conv)), "text": text}


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ───────────────────────────── presentation ─────────────────────────────────

def _row(label: str, value) -> str:
    return f"  {label:<16} {value}"


def render(desk: dict, verdict: dict) -> str:
    p, t, dv = desk.get("price", {}), desk.get("technicals", {}), desk.get("derivatives", {})
    w, m, mt = desk.get("whales", {}), desk.get("macro", {}), desk.get("market_ta", {})
    L = []
    bar = "═" * 70
    L.append(bar)
    L.append(f"  AEGIS DESK · CMC Agent Hub  ·  {desk['name']} ({desk['symbol']})"
             f"   [{desk['analysts_online']}/9 analysts online]")
    L.append(f"  {desk['generated']}  ·  powered by CoinMarketCap Agent Hub skills")
    L.append(bar)

    L.append("\n① PRICE  (get_crypto_quotes_latest)")
    if p:
        L.append(_row("price", f"${_fmt(p.get('price'))}   rank #{p.get('rank')}"))
        L.append(_row("momentum", f"1h {_pct(p.get('chg_1h'))}  ·  24h {_pct(p.get('chg_24h'))}  ·  "
                                  f"7d {_pct(p.get('chg_7d'))}  ·  30d {_pct(p.get('chg_30d'))}"))
    else:
        L.append("  no data")

    L.append("\n② TECHNICALS  (get_crypto_technical_analysis)")
    if t:
        L.append(_row("RSI(14)", f"{t.get('rsi14')}    MACD hist {t.get('macd_hist')}"))
        L.append(_row("MAs", f"7d {t.get('sma_7')}  ·  30d {t.get('sma_30')}  ·  200d {t.get('sma_200')}"))
    else:
        L.append("  no data")

    L.append("\n③ DERIVATIVES  (get_global_crypto_derivatives_metrics)")
    if dv:
        L.append(_row("open interest", f"{dv.get('open_interest')}  (24h {dv.get('oi_chg_24h')}  ·  30d {dv.get('oi_chg_30d')})"))
        L.append(_row("funding rate", f"{dv.get('funding_rate')}  (24h {dv.get('funding_chg_24h')})"))
        L.append(_row("liquidations 24h", f"long {dv.get('liq_long_24h')}  vs  short {dv.get('liq_short_24h')}"))
    else:
        L.append("  no data")

    L.append("\n④ WHALES  (get_crypto_metrics)")
    if w:
        L.append(_row("whale supply", f"{w.get('whale_supply_pct')}% of circulating"))
        L.append(_row("long holders", f"{w.get('holders_pct_addr')}% of addresses  ·  "
                                      f"active traders {w.get('traders_pct_addr')}%"))
    else:
        L.append("  no data")

    L.append("\n⑤ MACRO  (get_global_metrics_latest)")
    if m:
        L.append(_row("total mcap", f"{m.get('total_mcap')}  (24h {m.get('mcap_chg_24h')})  ·  BTC dom {m.get('btc_dominance')}"))
        L.append(_row("Fear & Greed", f"{m.get('fear_greed')} {m.get('fear_greed_label') or ''}"
                                      f"   ·   Altseason {m.get('altseason_index')} (24h {m.get('altseason_chg_24h')})"))
        L.append(_row("BTC ETF AUM", f"{m.get('btc_etf_aum')}  (prev wk {m.get('btc_etf_aum_prev_week')})"))
    else:
        L.append("  no data")

    L.append("\n⑥ MARKET TECHNICALS  (get_crypto_marketcap_technical_analysis)")
    if mt:
        L.append(_row("total mcap", f"{mt.get('mcap')}   RSI(14) {mt.get('rsi14')}"))
        L.append(_row("MACD hist", f"{mt.get('macd_hist')}   ·   pivot {mt.get('pivot')}"))
    else:
        L.append("  no data")

    L.append("\n⑦ CALENDAR  (get_upcoming_macro_events)")
    cal = desk.get("calendar", [])
    L.extend([_row("•", f"{e['title']}  [{e['date_str']}]") for e in cal[:4]] or ["  none imminent"])

    L.append("\n⑧ NEWS  (get_crypto_latest_news)")
    news = desk.get("news", [])
    L.extend([_row("•", n["title"]) for n in news[:4]] or ["  no data"])

    L.append("\n⑨ NARRATIVES  (trending_crypto_narratives)")
    narr = desk.get("narratives", [])
    L.extend([_row("•", f"{n['name']}  ({n.get('change_24h')} 24h)") for n in narr[:5]] or ["  no data"])

    L.append("\n" + bar)
    src = verdict.get("source", "?")
    model = f" · {verdict['model']}" if verdict.get("model") else ""
    L.append(f"  🧠 DESK VERDICT  (Claude synthesis{model}  ·  {src})")
    L.append(bar)
    for ln in verdict.get("text", "").splitlines():
        L.append(f"  {ln}")
    L.append(bar)
    L.append("  Read-only showcase — the live Aegis bot is untouched.  #CMCAgentHub")
    return "\n".join(L)


def _fmt(v) -> str:
    n = _num(v)
    if n is None:
        return str(v)
    if n >= 1000:
        return f"{n:,.0f}"
    return f"{n:,.4f}".rstrip("0").rstrip(".")


def _pct(v) -> str:
    """Render a raw percent float as e.g. '-1.48%'. Pass through if already a string."""
    if isinstance(v, str):
        return v
    n = _num(v)
    return f"{n:+.2f}%" if n is not None else "n/a"


# ───────────────────────────────── main ─────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Aegis Desk — CMC Agent Hub showcase (read-only)")
    ap.add_argument("--symbol", default="BTC", help="asset ticker (default BTC)")
    ap.add_argument("--model", default=None, help="override Claude model for the synthesis")
    ap.add_argument("--json", action="store_true", help="also write JSON to data/runtime")
    ap.add_argument("--slow", action="store_true",
                    help="cinematic line-by-line reveal — nice for screen recording")
    args = ap.parse_args()

    print(f"Aegis Desk: convening 9 CMC Agent Hub analysts on {args.symbol.upper()}...", flush=True)
    if args.slow:
        time.sleep(0.8)
    desk = gather(args.symbol)
    verdict = synthesize(desk, args.model)
    desk["verdict"] = {k: v for k, v in verdict.items() if k != "digest"}

    report = render(desk, verdict)
    if args.slow:
        print()
        for line in report.split("\n"):
            print(line, flush=True)
            time.sleep(0.06)
    else:
        print("\n" + report)

    RUNTIME.mkdir(parents=True, exist_ok=True)
    out_md = RUNTIME / f"agent_hub_desk_{desk['symbol']}.md"
    out_md.write_text("```\n" + render(desk, verdict) + "\n```\n", encoding="utf-8")
    print(f"\nsaved report -> {out_md.relative_to(ROOT)}", flush=True)
    if args.json:
        out_json = RUNTIME / f"agent_hub_desk_{desk['symbol']}.json"
        out_json.write_text(json.dumps(desk, indent=2, default=str), encoding="utf-8")
        print(f"saved json   -> {out_json.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
