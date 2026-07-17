"""Convergence gate (v2 spec §2): a single GMGN-tagged wallet buying is public info
everyone sees — no edge. Three DISTINCT tracked wallets converging on one token
inside 15 minutes is our self-built signal. RAM-only on purpose: pre-threshold
there is no money at risk, so losing the buffer on restart is acceptable (unlike
positions, which are always disk-persisted)."""
from __future__ import annotations

from ..monitor.logger import get_logger

log = get_logger(__name__)


class ClusterBuySignalTracker:
    def __init__(self, min_wallets: int = 3, window_minutes: int = 15) -> None:
        self._min = min_wallets
        self._window_s = window_minutes * 60
        # token -> wallet -> (first_ts_in_window, price_at_first_obs)
        self._obs: dict[str, dict[str, tuple[float, float | None]]] = {}

    def record(self, token_address: str, wallet: str, ts: float,
               price_usd: float | None) -> dict | None:
        token, wallet = token_address.lower(), wallet.lower()
        per_token = self._obs.setdefault(token, {})
        per_token = {w: v for w, v in per_token.items() if ts - v[0] <= self._window_s}
        newly_added = wallet not in per_token
        if newly_added:
            per_token[wallet] = (ts, price_usd)
        self._obs[token] = per_token
        if len(per_token) < self._min:
            if newly_added:   # repeat buys from a counted wallet would just spam
                log.info("cluster_pending", token=token, wallets=len(per_token),
                         need=self._min)
            return None
        ordered = sorted(per_token.items(), key=lambda kv: kv[1][0])
        first_ts, first_price = ordered[0][1]
        return {"wallets": [w for w, _ in ordered],
                "first_ts": first_ts, "first_price_usd": first_price}

    def clear(self, token_address: str) -> None:
        """Drop all buffered observations for a token. Call this whenever a
        position for the token closes — otherwise the wallets that formed the
        original cluster stay in the buffer and can immediately re-fire a new
        cluster event (and a new position) at a crashed/exited price, corrupting
        the shadow-mode go-live metric with re-entry churn."""
        self._obs.pop(token_address.lower(), None)
