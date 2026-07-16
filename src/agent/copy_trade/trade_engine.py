"""Open/exit/valve logic for cluster-gated copy trading — one engine, two fill modes.

shadow_mode=True  → paper fills at DexScreener price + a fee model; positions go to
                    the SHADOW store; `executors` is never touched (may be None).
shadow_mode=False → real fills through the existing best-execution stack, identical
                    to the old executor.py flow (safety gate, ranked backends,
                    full exit failover).
Every close (either mode) appends one JSON line to the closed-trades journal — the
shadow report and the go-live decision are built from that file."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..execution.binance_web3 import passes_safety_check
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .positions import CopyPosition, PositionStore
from .prices import get_price_usd, get_taxes

log = get_logger(__name__)

DEFAULT_TAXES = (0.05, 0.05)     # conservative when GoPlus has no data
GAS_USD_PER_LEG = 0.10
IMPACT_PER_LEG = 0.01


class TradeEngine:
    def __init__(self, budget: CopyTradeBudget, store: PositionStore,
                 executors: dict | None, shadow_mode: bool, journal_path: Path,
                 exit_wallets: int = 2, valve_drop_pct: float = 0.70,
                 slice_usd: float = 3.0) -> None:
        self._budget = budget
        self._store = store
        self._executors = executors
        self._shadow = shadow_mode
        self._journal_path = journal_path
        self._exit_wallets = exit_wallets
        self._valve_drop = valve_drop_pct
        self._slice = slice_usd

    # ---------- open ----------

    def open_cluster_position(self, token_address: str, token_symbol: str,
                              token_decimals: int, cluster: dict) -> bool:
        if not self._budget.can_open_new():
            log.info("cluster_buy_skipped_budget", token=token_symbol)
            return False
        usd_size = self._budget.allocate()
        amount_wei = str(int(usd_size * 10 ** 18))   # USDT: 18 decimals on BSC
        ok, decimals = passes_safety_check(settings.usdt_address, token_address,
                                           amount_wei)
        if not ok:
            self._budget.release(usd_size)
            log.warning("cluster_buy_skipped_safety", token=token_symbol)
            return False
        resolved_decimals = decimals or token_decimals
        try:
            if self._shadow:
                pos = self._paper_fill(token_address, token_symbol,
                                       resolved_decimals, usd_size, cluster)
            else:
                pos = self._live_fill(token_address, token_symbol,
                                      resolved_decimals, usd_size, cluster)
        except Exception:
            self._budget.release(usd_size)   # never leak a slice on failure
            raise
        if pos is None:
            self._budget.release(usd_size)
            return False
        self._store.open_position(pos)
        log.info("cluster_position_opened", token=token_symbol,
                 simulated=pos.simulated, entry=pos.entry_price_usd)
        return True

    def _paper_fill(self, token_address, token_symbol, decimals, usd_size,
                    cluster) -> CopyPosition | None:
        price = get_price_usd(token_address)
        if price is None or price <= 0:
            log.warning("shadow_fill_no_price", token=token_symbol)
            return None
        buy_tax, _ = get_taxes(token_address) or DEFAULT_TAXES
        entry = price * (1 + buy_tax + IMPACT_PER_LEG)
        return CopyPosition(
            token_symbol=token_symbol, token_address=token_address.lower(),
            token_decimals=decimals, source_wallet="", usd_size=usd_size,
            token_amount=usd_size / entry,
            opened_at=datetime.now(timezone.utc).isoformat(),
            cluster_wallets=cluster["wallets"], entry_price_usd=entry,
            simulated=True)

    def _live_fill(self, token_address, token_symbol, decimals, usd_size,
                   cluster) -> CopyPosition | None:
        register_discovered(token_symbol, token_address, decimals)
        ranked = rank_backends(self._executors, "USDT", token_symbol, usd_size)
        if not ranked:
            log.warning("cluster_buy_no_route", token=token_symbol)
            return None
        result = self._executors[ranked[0]].swap("USDT", token_symbol, usd_size)
        received_wei = getattr(result, "received_out_wei", 0) or \
            getattr(result, "expected_out_wei", 0)
        token_amount = received_wei / (10 ** decimals)
        if token_amount <= 0:
            log.warning("cluster_buy_zero_fill", token=token_symbol)
            return None
        return CopyPosition(
            token_symbol=token_symbol, token_address=token_address.lower(),
            token_decimals=decimals, source_wallet="", usd_size=usd_size,
            token_amount=token_amount,
            opened_at=datetime.now(timezone.utc).isoformat(),
            cluster_wallets=cluster["wallets"],
            entry_price_usd=usd_size / token_amount, simulated=False)

    # ---------- exits ----------

    def on_exit_signal(self, wallet: str, token_address: str) -> None:
        pos = self._store.find_by_token(token_address)
        if pos is None:
            return
        w = wallet.lower()
        if w not in (cw.lower() for cw in pos.cluster_wallets):
            log.debug("exit_signal_outside_cluster", token=pos.token_symbol)
            return
        if w not in (e.lower() for e in pos.exited_by):
            pos.exited_by.append(w)
            self._store.update(pos)
            log.info("cluster_exit_vote", token=pos.token_symbol,
                     votes=len(pos.exited_by), need=self._exit_wallets)
        if len(pos.exited_by) >= self._exit_wallets:
            self._close(pos, reason="cluster_sell")

    def check_valve(self) -> None:
        for pos in self._store.all():
            price = get_price_usd(pos.token_address)
            if price is None or pos.entry_price_usd <= 0:
                continue   # no price → hold state, never guess (spec)
            if price <= pos.entry_price_usd * (1 - self._valve_drop):
                log.warning("valve_triggered", token=pos.token_symbol,
                            entry=pos.entry_price_usd, price=price)
                self._close(pos, reason="valve")

    def _close(self, pos: CopyPosition, reason: str) -> None:
        exit_price = get_price_usd(pos.token_address) or 0.0
        if not pos.simulated:
            if not self._sell_live(pos):
                return   # keep position open; a later signal/valve tick retries
        _, sell_tax = get_taxes(pos.token_address) or DEFAULT_TAXES
        effective_exit = exit_price * (1 - sell_tax - IMPACT_PER_LEG)
        pnl_usd = (effective_exit - pos.entry_price_usd) * pos.token_amount
        buy_tax, _ = get_taxes(pos.token_address) or DEFAULT_TAXES
        fees_model = 2 * GAS_USD_PER_LEG + pos.usd_size * (buy_tax + sell_tax
                                                           + 2 * IMPACT_PER_LEG)
        self._store.close_by_token(pos.token_address)
        self._budget.release(pos.usd_size)
        self._journal({
            "token_address": pos.token_address, "token_symbol": pos.token_symbol,
            "simulated": pos.simulated, "usd_size": pos.usd_size,
            "entry_price_usd": pos.entry_price_usd, "exit_price_usd": effective_exit,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round((effective_exit / pos.entry_price_usd - 1), 4)
            if pos.entry_price_usd else None,
            "opened_at": pos.opened_at,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "cluster_wallets": pos.cluster_wallets,
            "exited_by": pos.exited_by, "fees_model_usd": round(fees_model, 4)})
        log.info("cluster_position_closed", token=pos.token_symbol, reason=reason,
                 simulated=pos.simulated, pnl_usd=round(pnl_usd, 4))

    def _sell_live(self, pos: CopyPosition) -> bool:
        """Full-failover live sell (mirrors the old executor.py exit path)."""
        ranked = rank_backends(self._executors, pos.token_symbol, "USDT",
                               pos.token_amount)
        for backend in ranked:
            try:
                self._executors[backend].swap(pos.token_symbol, "USDT",
                                              pos.token_amount)
                return True
            except Exception as e:  # noqa: BLE001 — try every backend
                log.warning("live_sell_failed", token=pos.token_symbol,
                            backend=backend, error=str(e))
        log.error("live_sell_all_backends_failed", token=pos.token_symbol)
        return False

    def _journal(self, row: dict) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
