"""Event ingestion: typed events, project-source mapping, and source adapters.

Source adapters are pluggable. We ship a SAFE local/manual JSON feed
(`ManualJsonEventSource`) so the radar is fully testable without depending on
any third-party API. X/CMC/news API adapters are declared as interfaces only —
we do NOT implement brittle scraping that violates platform terms; wire real
APIs later behind these interfaces when credentials are available.

Nothing here touches the chain or signs anything.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PROJECT_SOURCES_PATH = DATA_DIR / "project_sources.json"
MANUAL_EVENTS_PATH = DATA_DIR / "manual_events.json"

# How a source is trusted. "authority" = Binance/BNB Chain/CMC/Trust Wallet/CZ;
# "project" = the token's own official channel; "aggregator" = high-signal
# third party; "unverified" = unknown/low-trust (penalised).
SourceType = Literal["authority", "project", "aggregator", "unverified"]


@dataclass(frozen=True)
class Event:
    token: str                       # symbol (may be ambiguous; contract preferred)
    text: str
    source: str = ""                 # handle/name, e.g. "binance", "cz_binance"
    source_type: SourceType = "unverified"
    bsc_contract: str = ""           # authoritative token key when present
    url: str = ""
    timestamp: float = field(default_factory=lambda: time.time())


@dataclass(frozen=True)
class ProjectSource:
    symbol: str
    name: str = ""
    bsc_contract: str = ""
    official_website: str = ""
    official_x_handle: str = ""
    cmc_slug_or_id: str = ""
    keywords: tuple[str, ...] = ()
    risk_notes: str = ""


def load_project_sources(path: Path | None = None) -> dict[str, ProjectSource]:
    """Return {UPPER symbol -> ProjectSource}. Missing file -> empty mapping."""
    path = path or PROJECT_SOURCES_PATH
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, ProjectSource] = {}
    for r in raw:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        out[sym.upper()] = ProjectSource(
            symbol=sym,
            name=r.get("name", ""),
            bsc_contract=(r.get("bsc_contract") or "").strip(),
            official_website=r.get("official_website", "") or "",
            official_x_handle=r.get("official_x_handle", "") or "",
            cmc_slug_or_id=str(r.get("cmc_slug_or_id", "") or ""),
            keywords=tuple(r.get("keywords", []) or ()),
            risk_notes=r.get("risk_notes", "") or "",
        )
    return out


# --------------------------- source adapters ---------------------------

class EventSource(ABC):
    """Pluggable event feed. Implementations must NOT scrape in violation of
    platform terms; use official APIs or a manual feed."""

    @abstractmethod
    def fetch(self) -> list[Event]:
        ...


class ManualJsonEventSource(EventSource):
    """Reads events from a local JSON file — lets us test the radar end-to-end
    with no external dependency. Each entry: {token, text, source, source_type,
    bsc_contract?, url?, timestamp?}."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or MANUAL_EVENTS_PATH

    def fetch(self) -> list[Event]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        events: list[Event] = []
        for r in raw:
            if not (r.get("token") or r.get("bsc_contract")):
                continue
            ts = float(r.get("timestamp") or 0)
            events.append(Event(
                token=(r.get("token") or "").strip(),
                text=r.get("text", "") or "",
                source=r.get("source", "") or "",
                source_type=r.get("source_type", "unverified"),
                bsc_contract=(r.get("bsc_contract") or "").strip(),
                url=r.get("url", "") or "",
                timestamp=ts if ts > 0 else time.time(),   # 0 => treat as "now"
            ))
        return events


class XApiEventSource(EventSource):
    """Interface stub for the X (Twitter) API. NOT implemented — requires
    official API credentials. Declared so the radar can be wired to a compliant
    feed later without changing the scanner."""

    def __init__(self, *_, **__) -> None:
        self._available = False

    def fetch(self) -> list[Event]:
        if not self._available:
            raise NotImplementedError(
                "XApiEventSource requires official X API credentials; "
                "use ManualJsonEventSource for now."
            )
        return []
