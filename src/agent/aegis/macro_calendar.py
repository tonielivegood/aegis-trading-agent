"""CMC Agent Hub — macro-calendar awareness (the `get_upcoming_macro_events` skill).

Most hackathon agents ping ONE Agent Hub endpoint. This sleeve uses a SECOND class of
skill — CMC's curated macro/catalyst calendar — as a real risk input: the agent stands
DOWN (halts new entries) into an imminent high-volatility catalyst, then resumes after.

Pure and deterministic: the caller injects the raw event rows (from the MCP skill) and
the current time; this module parses the human dates, drops anything past, and decides
the guard. TIGHTENING-ONLY (it can only block new entries, never force a trade) and
FAIL-SAFE (no/garbled data ⇒ no guard). No network here.
"""
from __future__ import annotations

from datetime import date, datetime


def parse_event_date(s: str | None) -> date | None:
    """CMC's eventDate is a human string like '4 July 2026' / '28 Jul 2026'. → date, or
    None on anything unparseable (so a weird row is dropped, never raised)."""
    if not s:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    return parse_event_date(value if isinstance(value, str) else None)


def annotate(events, now) -> list[dict]:
    """Map raw events → UPCOMING events (eventDate >= today), each tagged with
    `days_until`, sorted nearest-first. Each event is {title, date(str/date), url?}.
    Rows with no parseable date, or already in the past, are dropped."""
    today = now.date() if isinstance(now, datetime) else now
    out: list[dict] = []
    for e in events or []:
        d = _as_date(e.get("date") or e.get("date_str"))
        if d is None:
            continue
        days = (d - today).days
        if days < 0:
            continue
        out.append({"title": e.get("title") or "", "date": d.isoformat(),
                    "days_until": days, "url": e.get("url") or ""})
    return sorted(out, key=lambda x: x["days_until"])


def guard(events, now, *, within_days: int = 1) -> tuple[bool, str | None]:
    """(block, reason). Block NEW entries if the nearest UPCOMING macro event lands within
    `within_days` (CMC's calendar is day-granular). Tightening-only; fail-safe — empty or
    unparseable events return (False, None) so the agent trades on its mechanical logic."""
    upcoming = annotate(events, now)
    if not upcoming:
        return False, None
    nxt = upcoming[0]
    if nxt["days_until"] <= max(0, within_days):
        when = "today" if nxt["days_until"] == 0 else f"in {nxt['days_until']}d"
        return True, f"macro guard: '{nxt['title']}' {when}"
    return False, None
