"""Tiered catalyst sources.

Tier 1 — high authority: Binance / BNB Chain / CMC / Trust Wallet / CZ(X).
Tier 2 — official project channels.
Tier 3 — manual feed + (labelled) unverified social.

The real-network adapters are CREDENTIAL/CONFIG-GATED and FAIL SAFE: if the
required key/flag is absent or a fetch fails, they return [] and never crash.
We do NOT scrape X or any platform; X is adapter-only and needs X_BEARER_TOKEN.
We never invent official handles. The fully-working, tested path is the manual
JSON feed (Tier 3 / explicitly-labelled), used for simulation.
"""
from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

import requests

from ..config import settings
from ..monitor.logger import get_logger
from .catalyst_score import (
    TIER_AUTHORITY,
    CatalystEvent,
    detected_keywords,
    tier_of,
)
from .events import MANUAL_EVENTS_PATH

log = get_logger(__name__)
_TIMEOUT_S = 15


def make_event(*, source_name: str, source_tier: int, source_type: str, text: str,
               url: str = "", ts: float | None = None, symbols=(), contracts=(),
               is_official=False, is_verified=False, now: float | None = None) -> CatalystEvent:
    now = time.time() if now is None else now
    ts = ts if ts else now
    eid = hashlib.sha1(f"{source_name}|{text}|{int(ts)}".encode("utf-8")).hexdigest()[:16]
    return CatalystEvent(
        event_id=eid, source_name=source_name, source_tier=source_tier, source_url=url,
        source_type=source_type, timestamp=float(ts), detected_at=now, raw_text=text,
        normalized_text=" ".join(text.lower().split()),
        mentioned_symbols=tuple(s.upper() for s in symbols),
        matched_contracts=tuple(c.lower() for c in contracts),
        keywords=detected_keywords(text), is_official_source=is_official,
        is_verified_source=is_verified,
    )


class CatalystSource(ABC):
    name: str = "source"
    tier: int = 3

    @abstractmethod
    def fetch(self) -> list[CatalystEvent]:
        ...


class ManualCatalystSource(CatalystSource):
    """Local JSON feed — the testable catalyst input. Each entry may set its own
    source/source_type (and optional source_tier); tier is otherwise derived."""
    name = "manual"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or MANUAL_EVENTS_PATH

    def fetch(self) -> list[CatalystEvent]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        out: list[CatalystEvent] = []
        for r in raw:
            if not (r.get("token") or r.get("bsc_contract")):
                continue
            stype = r.get("source_type", "unverified")
            src = r.get("source", "manual")
            tier = int(r["source_tier"]) if r.get("source_tier") else tier_of(stype, src)
            symbols = [r["token"]] if r.get("token") else []
            contracts = [r["bsc_contract"]] if r.get("bsc_contract") else []
            out.append(make_event(
                source_name=src, source_tier=tier, source_type=stype, text=r.get("text", ""),
                url=r.get("url", ""), ts=float(r.get("timestamp") or 0) or None,
                symbols=symbols, contracts=contracts,
                is_official=(tier <= 2), is_verified=(tier <= 2)))
        return out


class _GatedHttpSource(CatalystSource):
    """Base for credential/flag-gated Tier-1 adapters. Disabled => returns []."""
    source_type = "authority"

    def __init__(self, enabled: bool, url: str = "") -> None:
        self._enabled = enabled
        self._url = url

    def _enabled_or_skip(self) -> bool:
        if not self._enabled:
            log.debug("catalyst_source_disabled", source=self.name)
        return self._enabled

    def fetch(self) -> list[CatalystEvent]:  # pragma: no cover - network, fail-safe
        if not self._enabled_or_skip() or not self._url:
            return []
        try:
            resp = requests.get(self._url, timeout=_TIMEOUT_S)
            resp.raise_for_status()
            return self.parse(resp.json())
        except Exception as e:  # noqa: BLE001 — any failure fails safe
            log.debug("catalyst_source_fetch_failed", source=self.name, error=type(e).__name__)
            return []

    def parse(self, payload) -> list[CatalystEvent]:  # pragma: no cover
        return []


class BinanceAnnouncementsSource(_GatedHttpSource):
    name = "binance"
    tier = TIER_AUTHORITY


class BNBChainAnnouncementsSource(_GatedHttpSource):
    name = "bnbchain"
    tier = TIER_AUTHORITY


class TrustWalletAnnouncementsSource(_GatedHttpSource):
    name = "trustwallet"
    tier = TIER_AUTHORITY


class CMCContentSource(_GatedHttpSource):
    """CMC content/news — only if a CMC API key is configured."""
    name = "coinmarketcap"
    tier = TIER_AUTHORITY


class CZXSource(_GatedHttpSource):
    """CZ / Binance official X — adapter-only, requires X_BEARER_TOKEN. Never scrapes."""
    name = "cz_binance"
    tier = TIER_AUTHORITY


def enabled_sources(*, manual_path: Path | None = None) -> list[CatalystSource]:
    """Assemble the active sources. Tier-1 adapters turn on only when their
    credential/flag is present; otherwise they are simply omitted (fail-safe).
    The manual feed is always included."""
    sources: list[CatalystSource] = [ManualCatalystSource(manual_path)]

    def _flag(name: str) -> bool:
        import os
        return os.getenv(name, "").lower() in ("1", "true", "yes")

    # Tier-1 adapters (disabled by default; no scraping). Enable explicitly + supply URL/creds.
    sources.append(BinanceAnnouncementsSource(_flag("CATALYST_BINANCE_ENABLED")))
    sources.append(BNBChainAnnouncementsSource(_flag("CATALYST_BNBCHAIN_ENABLED")))
    sources.append(TrustWalletAnnouncementsSource(_flag("CATALYST_TRUSTWALLET_ENABLED")))
    sources.append(CMCContentSource(bool(settings.cmc_api_key) and _flag("CATALYST_CMC_ENABLED")))
    sources.append(CZXSource(bool(_get_x_bearer()) and settings.catalyst_x_enabled))
    return sources


def _get_x_bearer() -> str:
    import os
    return os.getenv("X_BEARER_TOKEN", "")
