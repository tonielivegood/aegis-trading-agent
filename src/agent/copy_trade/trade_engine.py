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
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..execution.binance_web3 import passes_safety_check
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .positions import CopyPosition, PositionStore
from .prices import get_price_usd, get_taxes, get_pair_stats, get_holder_stats

log = get_logger(__name__)

DEFAULT_TAXES = (0.05, 0.05)     # conservative when GoPlus has no data
GAS_USD_PER_LEG = 0.10
IMPACT_PER_LEG = 0.01


class TradeEngine:
    def __init__(self, budget: CopyTradeBudget, store: PositionStore,
                 executors: dict | None, shadow_mode: bool, journal_path: Path,
                 exit_wallets: int = 2, valve_drop_pct: float = 0.70,
                 slice_usd: float = 3.0,
                 cooldown_minutes: float = 0,
                 max_token_age_days: float | None = None,
                 max_market_cap_usd: float | None = None,
                 min_liquidity_usd: float | None = None,
                 signals_path: Path | None = None,
                 trail_pct: float | None = None,
                 partial_fraction: float | None = None,
                 max_single_holder_pct: float | None = None,
                 max_top5_holder_pct: float | None = None,
                 daily_loss_limit_usd: float | None = None) -> None:
        self._budget = budget
        self._store = store
        self._executors = executors
        self._shadow = shadow_mode
        self._journal_path = journal_path
        self._exit_wallets = exit_wallets
        self._valve_drop = valve_drop_pct
        self._slice = slice_usd
        self._cooldown_s = cooldown_minutes * 60
        self._max_age_days = max_token_age_days
        self._max_mcap_usd = max_market_cap_usd
        self._min_liq_usd = min_liquidity_usd
        self._signals_path = signals_path
        self._trail_pct = trail_pct
        self._partial_fraction = partial_fraction
        self._max_single_holder_pct = max_single_holder_pct
        self._max_top5_holder_pct = max_top5_holder_pct
        self._daily_loss_limit_usd = daily_loss_limit_usd
        # token -> epoch until which re-entry is refused. Seeded from the journal
        # so a restart right after a close can't bypass the cooldown (AKE was
        # churned 3x/20min live — this is that fix).
        self._cooldown_until: dict[str, float] = {}
        if self._cooldown_s > 0 and journal_path.exists():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    closed = datetime.fromisoformat(row["closed_at"]).timestamp()
                    token_address = row["token_address"]
                except (ValueError, KeyError):
                    continue
                if closed + self._cooldown_s > time.time():
                    self._cooldown_until[token_address] = closed + self._cooldown_s

    # ---------- open ----------

    def open_cluster_position(self, token_address: str, token_symbol: str,
                              token_decimals: int, cluster: dict,
                              batch_sellers: set[str] | None = None) -> bool:
        token = token_address.lower()
        if self._cooldown_until.get(token, 0.0) > time.time():
            self._log_signal(token, token_symbol, cluster, "skipped_cooldown", "")
            log.info("cluster_buy_skipped_cooldown", token=token_symbol)
            return False
        if self._store.find_by_token(token_address) is not None:
            # Root-cause guard for both entry paths (cluster-vote + phase2-film):
            # never let a token accumulate a second position. Cheapest check
            # (no network) so it runs before every pricier gate below.
            self._log_signal(token, token_symbol, cluster, "skipped_already_open", "")
            log.info("cluster_buy_skipped_already_open", token=token_symbol)
            return False
        if batch_sellers:
            dead = {w.lower() for w in cluster["wallets"]} & batch_sellers
            if len(dead) >= self._exit_wallets:
                self._log_signal(token, token_symbol, cluster, "skipped_stale",
                                 f"{len(dead)}_cluster_wallets_sold_same_batch")
                log.info("cluster_buy_skipped_stale", token=token_symbol,
                         dead=len(dead))
                return False
        ok, why = self._passes_gem_filter(token)
        if not ok:
            self._log_signal(token, token_symbol, cluster, "skipped_gem_filter", why)
            log.info("cluster_buy_skipped_gem_filter", token=token_symbol, reason=why)
            return False
        if self._daily_loss_limit_usd is not None:
            lost = self._realized_pnl_today()
            if lost <= -self._daily_loss_limit_usd:
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_circuit_breaker", f"day_pnl_{lost:.2f}")
                log.warning("circuit_breaker_open", day_pnl=round(lost, 2))
                return False
        if self._max_single_holder_pct is not None or self._max_top5_holder_pct is not None:
            hs = get_holder_stats(token)
            if hs is None:
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", "no_holder_data")
                return False           # fail closed — can't see the whales, don't buy
            if (self._max_single_holder_pct is not None
                    and hs["top_pct"] > self._max_single_holder_pct):
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", f"top_{hs['top_pct']:.2f}")
                return False
            if (self._max_top5_holder_pct is not None
                    and hs["top5_pct"] > self._max_top5_holder_pct):
                self._log_signal(token, token_symbol, cluster,
                                 "skipped_concentration", f"top5_{hs['top5_pct']:.2f}")
                return False
        if not self._budget.can_open_new():
            self._log_signal(token, token_symbol, cluster, "skipped_budget", "")
            log.info("cluster_buy_skipped_budget", token=token_symbol)
            return False
        usd_size = self._budget.allocate()
        amount_wei = str(int(usd_size * 10 ** 18))   # USDT: 18 decimals on BSC
        ok, decimals = passes_safety_check(settings.usdt_address, token_address,
                                           amount_wei)
        if not ok:
            self._budget.release(usd_size)
            self._log_signal(token, token_symbol, cluster, "skipped_safety", "")
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
        self._log_signal(token, token_symbol, cluster, "opened", "")
        log.info("cluster_position_opened", token=token_symbol,
                 simulated=pos.simulated, entry=pos.entry_price_usd)
        return True

    def _passes_gem_filter(self, token_address: str) -> tuple[bool, str]:
        """Gem thesis gate: 5x-10x lives in young + small + liquid tokens.
        All three knobs None → gate off (v2 behavior, and what old tests use)."""
        if (self._max_age_days is None and self._max_mcap_usd is None
                and self._min_liq_usd is None):
            return True, ""
        stats = get_pair_stats(token_address)
        if stats is None:
            return False, "no_pair_stats"
        if self._min_liq_usd is not None and stats["liquidity_usd"] < self._min_liq_usd:
            return False, f"liquidity_{stats['liquidity_usd']:.0f}"
        if self._max_mcap_usd is not None:
            if stats["market_cap_usd"] is None or stats["market_cap_usd"] > self._max_mcap_usd:
                return False, f"mcap_{stats['market_cap_usd']}"
        if self._max_age_days is not None:
            created = stats["pair_created_at_ms"]
            if created is None:
                return False, "age_unknown"     # can't verify young → not a gem entry
            age_days = (time.time() - created / 1000) / 86400
            if age_days > self._max_age_days:
                return False, f"age_{age_days:.1f}d"
        return True, ""

    def _realized_pnl_today(self) -> float:
        if not self._journal_path.exists():
            return 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0.0
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("closed_at", "").startswith(today) and not row.get("simulated"):
                    total += float(row.get("pnl_usd") or 0)
            except (ValueError, TypeError):
                continue
        return total

    def _log_signal(self, token: str, symbol: str, cluster: dict,
                    decision: str, detail: str) -> None:
        """One JSONL row per cluster decision — opened or why not. gem_report.py
        scores these later against what the token actually did (the 'do our
        signals even point at gems?' measurement)."""
        if self._signals_path is None:
            return
        row = {"ts": datetime.now(timezone.utc).isoformat(),
               "token_address": token, "token_symbol": symbol,
               "decision": decision, "detail": detail,
               "price_usd": get_price_usd(token),
               "cluster_wallets": cluster["wallets"]}
        self._signals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._signals_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

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
            simulated=True,
            first_price_usd=cluster.get("first_price_usd") or 0.0,
            high_water_usd=entry)

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
            entry_price_usd=usd_size / token_amount, simulated=False,
            first_price_usd=cluster.get("first_price_usd") or 0.0,
            high_water_usd=usd_size / token_amount)

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
            if self._partial_fraction is None:
                self._close(pos, reason="cluster_sell")
            elif not pos.cluster_partial_done:
                self._close_partial(pos, reason="cluster_partial")

    def check_exits(self) -> None:
        """Valve (catastrophe backstop) + HWM trailing stop, one price fetch per
        position per tick. HWM granularity is the ~60s tick — intra-minute wicks
        are invisible by design (we hold hours-days, not seconds)."""
        for pos in self._store.all():
            price = get_price_usd(pos.token_address)
            if price is None or pos.entry_price_usd <= 0:
                continue   # no price → hold state, never guess (spec)
            hwm = max(pos.high_water_usd, pos.entry_price_usd, price)
            if hwm != pos.high_water_usd:
                pos.high_water_usd = hwm             # entry-baseline also fixes
                self._store.update(pos)              # legacy rows loaded with 0.0
            if price <= pos.entry_price_usd * (1 - self._valve_drop):
                log.warning("valve_triggered", token=pos.token_symbol,
                            entry=pos.entry_price_usd, price=price)
                self._close(pos, reason="valve")
            elif self._trail_pct is not None and price <= hwm * (1 - self._trail_pct):
                log.info("trail_triggered", token=pos.token_symbol,
                         hwm=hwm, price=price)
                self._close(pos, reason="trail")

    def _close(self, pos: CopyPosition, reason: str) -> None:
        exit_price = get_price_usd(pos.token_address) or 0.0
        if not pos.simulated and self._shadow:
            # Invariant violation: a shadow-mode engine should never hold a real
            # position. Fail safe — don't touch executors/budget/store; leave the
            # position for manual investigation rather than risk a crash (executors
            # may be None here) or, worse, an unintended real swap.
            log.error("shadow_engine_has_real_position", token=pos.token_symbol)
            return
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
            "first_price_usd": pos.first_price_usd,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round((effective_exit / pos.entry_price_usd - 1), 4)
            if pos.entry_price_usd else None,
            "opened_at": pos.opened_at,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "cluster_wallets": pos.cluster_wallets,
            "exited_by": pos.exited_by, "fees_model_usd": round(fees_model, 4)})
        if self._cooldown_s > 0:
            self._cooldown_until[pos.token_address] = time.time() + self._cooldown_s
        log.info("cluster_position_closed", token=pos.token_symbol, reason=reason,
                 simulated=pos.simulated, pnl_usd=round(pnl_usd, 4))

    def _close_partial(self, pos: CopyPosition, reason: str) -> None:
        """Sell partial_fraction once; the remainder rides the trailing stop.
        This is what lets a 5x run instead of exiting at the first profit-taker."""
        exit_price = get_price_usd(pos.token_address) or 0.0
        if not pos.simulated and self._shadow:
            log.error("shadow_engine_has_real_position", token=pos.token_symbol)
            return
        sell_amount = pos.token_amount * self._partial_fraction
        sell_usd = pos.usd_size * self._partial_fraction
        if not pos.simulated:
            if not self._sell_live(pos, amount=sell_amount):
                return   # votes stay recorded; valve/trail still guard the whole
        _, sell_tax = get_taxes(pos.token_address) or DEFAULT_TAXES
        effective_exit = exit_price * (1 - sell_tax - IMPACT_PER_LEG)
        pnl_usd = (effective_exit - pos.entry_price_usd) * sell_amount
        pos.token_amount -= sell_amount
        pos.usd_size -= sell_usd
        pos.cluster_partial_done = True
        self._store.update(pos)
        self._budget.release(sell_usd)
        self._journal({
            "token_address": pos.token_address, "token_symbol": pos.token_symbol,
            "simulated": pos.simulated, "usd_size": sell_usd,
            "entry_price_usd": pos.entry_price_usd, "exit_price_usd": effective_exit,
            "first_price_usd": pos.first_price_usd,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round((effective_exit / pos.entry_price_usd - 1), 4)
            if pos.entry_price_usd else None,
            "opened_at": pos.opened_at,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "cluster_wallets": pos.cluster_wallets,
            "exited_by": pos.exited_by, "fees_model_usd": None})
        log.info("cluster_partial_closed", token=pos.token_symbol,
                 sold_usd=sell_usd, remaining_usd=pos.usd_size,
                 pnl_usd=round(pnl_usd, 4))

    def _sell_live(self, pos: CopyPosition, amount: float | None = None) -> bool:
        """Full-failover live sell (mirrors the old executor.py exit path)."""
        amount = pos.token_amount if amount is None else amount
        ranked = rank_backends(self._executors, pos.token_symbol, "USDT", amount)
        for backend in ranked:
            try:
                self._executors[backend].swap(pos.token_symbol, "USDT", amount)
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
