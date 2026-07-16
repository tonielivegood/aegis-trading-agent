"""Copy-Trade Monitor v2 — cluster-gated, RPC-sourced, shadow-mode-first.

    python -m src.agent.copy_trade.monitor            # scan loop
    python -m src.agent.copy_trade.monitor --status
    python -m src.agent.copy_trade.monitor --scan     # one pass

Pipeline per scan: ChainEventSource.poll() → buy events feed the
ClusterBuySignalTracker (>=3 distinct wallets / 15 min); a firing cluster opens ONE
position via TradeEngine (paper when shadow_mode, real otherwise); out events feed
the 2-of-cluster exit rule; a -70% price valve runs every pass. Wallets come from
data/copy_trade/wallets.json (built by scripts/build_bsc_smart_wallets.py).

Replaces the old Moralis-polling monitor (single-wallet-mirror, swap_parser.py +
executor.py) after the 2026-07-16 phantom-position incident: a fresh DRY_RUN
instance replayed 25 historical txs from an empty state.json and "bought" 9 phantom
positions in under a minute. ChainEventSource's start_block=pool.latest_block() is
the fix — this monitor must never construct it with an earlier block."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ..config import settings
from ..data.token_list import register_discovered
from ..email_notifier import EmailNotifier
from ..execution.oneinch import OneInch
from ..execution.openocean import OpenOcean
from ..execution.pancakeswap import PancakeSwap
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .chain_events import ChainEventSource, WalletEvent
from .cluster_signal import ClusterBuySignalTracker
from .positions import PositionStore
from .prices import get_price_usd
from .rpc_pool import RpcPool
from .trade_engine import TradeEngine

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"
WALLETS_PATH = ROOT / "data" / "copy_trade" / "wallets.json"
STATE_PATH = ROOT / "data" / "copy_trade" / "state.json"
POSITIONS_PATH = ROOT / "data" / "copy_trade" / "positions.json"
SHADOW_PATH = ROOT / "data" / "copy_trade" / "shadow_positions.json"
JOURNAL_PATH = ROOT / "data" / "copy_trade" / "closed_trades.jsonl"
FAILURE_ALERT_THRESHOLD = 5


def _load_wallets() -> list[str]:
    if not WALLETS_PATH.exists():
        print(f"FATAL: {WALLETS_PATH} missing — run scripts/build_bsc_smart_wallets.py first")
        raise SystemExit(1)
    return [w["address"] for w in json.loads(WALLETS_PATH.read_text(encoding="utf-8"))]


def _token_meta(pool: RpcPool, token_address: str) -> tuple[str, int]:
    """symbol()/decimals() via eth_call; graceful fallback for weird tokens."""
    def call(sig: str) -> str | None:
        try:
            return pool.call("eth_call", [{"to": token_address, "data": sig}, "latest"])
        except Exception:  # noqa: BLE001
            return None
    sym_raw = call("0x95d89b41")        # symbol()
    dec_raw = call("0x313ce567")        # decimals()
    symbol = token_address[:8]
    if sym_raw and len(sym_raw) > 130:
        try:
            n = int(sym_raw[66:130], 16)
            symbol = bytes.fromhex(sym_raw[130:130 + n * 2]).decode(
                "utf-8", errors="replace") or symbol
        except Exception:  # noqa: BLE001
            pass
    decimals = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else 18
    return symbol, decimals


def process_events(events: list[WalletEvent], tracker: ClusterBuySignalTracker,
                   engine: TradeEngine, store: PositionStore,
                   notifier: EmailNotifier | None,
                   token_meta_fn) -> None:
    for ev in events:
        if ev.direction == "out":
            was_open = store.find_by_token(ev.token_address) is not None
            engine.on_exit_signal(ev.wallet, ev.token_address)
            if was_open and store.find_by_token(ev.token_address) is None:
                _notify(notifier, f"[COPY-TRADE] CLOSED {ev.token_address[:10]}…",
                        f"closed by cluster exit rule; wallet {ev.wallet}\n"
                        f"tx https://bscscan.com/tx/{ev.tx_hash}")
            continue
        # direction == "in"
        if store.find_by_token(ev.token_address) is not None:
            continue   # already holding — never double-buy one token (spec §3)
        price = get_price_usd(ev.token_address)
        cluster = tracker.record(ev.token_address, ev.wallet, time.time(), price)
        if cluster is None:
            continue   # sub-threshold: log only, no email (spec §2)
        symbol, decimals = token_meta_fn(ev.token_address)
        opened = engine.open_cluster_position(ev.token_address, symbol, decimals,
                                              cluster)
        if opened:
            _notify(notifier,
                    f"[COPY-TRADE{' SHADOW' if engine._shadow else ''}] CLUSTER BUY {symbol}",
                    f"token {ev.token_address}\nwallets: {', '.join(cluster['wallets'])}\n"
                    f"first buy price: {cluster['first_price_usd']}\n"
                    f"trigger price: {price}\n"
                    f"tx https://bscscan.com/tx/{ev.tx_hash}")


def _notify(notifier, subject: str, body: str) -> None:
    if notifier is None:
        return
    try:
        notifier.send_alert(subject, body)
    except Exception:  # noqa: BLE001 — email must never kill the loop
        log.warning("notify_failed", subject=subject)


def _build_runtime(cfg: dict):
    shadow = cfg.get("shadow_mode", True)
    budget = CopyTradeBudget(total_usd=cfg.get("total_budget_usd", 16.14),
                             slice_usd=cfg.get("slice_usd", 3.0))
    store = PositionStore(SHADOW_PATH if shadow else POSITIONS_PATH)
    store.load()
    for p in store.all():   # reconcile after restart (C2+C3, now incl. v2 fields)
        register_discovered(p.token_symbol, p.token_address, p.token_decimals)
        if budget.can_open_new():
            budget.allocate()
    executors = None
    if not shadow:
        account = None
        if not settings.dry_run:
            from eth_account import Account
            account = Account.from_key(settings.agent_private_key)
        executors = {
            "1inch": OneInch(account=account, dry_run=settings.dry_run),
            "openocean": OpenOcean(account=account, dry_run=settings.dry_run),
            "pancake": PancakeSwap(account=account, dry_run=settings.dry_run,
                                   slippage_bps=cfg.get("exec_slippage_bps", 1500)),
        }
    engine = TradeEngine(budget=budget, store=store, executors=executors,
                         shadow_mode=shadow, journal_path=JOURNAL_PATH,
                         exit_wallets=cfg.get("exit_wallets", 2),
                         valve_drop_pct=cfg.get("valve_drop_pct", 0.70),
                         slice_usd=cfg.get("slice_usd", 3.0))
    return budget, store, engine


def run_scan(once: bool = False) -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["copy_settings"]
    wallets = _load_wallets()
    pool = RpcPool(cfg["rpc_endpoints"])
    source = ChainEventSource(pool, wallets, start_block=pool.latest_block(),
                              ignore_tokens=set(cfg.get("ignore_tokens", [])))
    budget, store, engine = _build_runtime(cfg)
    tracker = ClusterBuySignalTracker(min_wallets=cfg.get("min_wallets", 3),
                                      window_minutes=cfg.get("window_minutes", 15))
    try:
        notifier = EmailNotifier()
    except ValueError:
        notifier = None
    interval = cfg.get("poll_interval_seconds", 45)
    consecutive_failures, outage_alerted = 0, False
    mode = "SHADOW" if cfg.get("shadow_mode", True) else "LIVE"
    log.info("copy_trade_monitor_v2_started", wallets=len(wallets), mode=mode,
             start_block=source.last_processed)

    while True:
        try:
            events = source.poll()
            consecutive_failures, outage_alerted = 0, False
        except Exception as e:  # noqa: BLE001
            consecutive_failures += 1
            log.error("event_poll_failed", error=str(e), streak=consecutive_failures)
            if consecutive_failures >= FAILURE_ALERT_THRESHOLD and not outage_alerted:
                _notify(notifier, "[COPY-TRADE] data source DOWN",
                        f"{consecutive_failures} consecutive poll failures — "
                        f"monitor is blind until RPC recovers. Last error: {e}")
                outage_alerted = True
            events = []
        process_events(events, tracker, engine, store, notifier,
                       lambda a: _token_meta(pool, a))
        open_before = {p.token_address for p in store.all()}
        engine.check_valve()
        for token_address in open_before - {p.token_address for p in store.all()}:
            _notify(notifier,
                    f"[COPY-TRADE{' SHADOW' if engine._shadow else ''}] "
                    f"VALVE STOP-LOSS {token_address[:10]}…",
                    f"closed by -70% price valve\ntoken {token_address}")
        STATE_PATH.write_text(json.dumps({
            "last_scan_at": datetime.now(timezone.utc).isoformat(),
            "last_processed_block": source.last_processed}), encoding="utf-8")
        if once:
            break
        time.sleep(interval)


def show_status() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["copy_settings"]
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    print("\n" + "=" * 60)
    print("  COPY-TRADE MONITOR STATUS (v2)")
    print("=" * 60)
    wallets_count = "?"
    if WALLETS_PATH.exists():
        wallets_count = len(json.loads(WALLETS_PATH.read_text(encoding="utf-8")))
    print(f"\n  wallets.json:  {wallets_count}"
          f"{'' if WALLETS_PATH.exists() else ' (missing — see scripts/build_bsc_smart_wallets.py)'}")
    for label, path in (("REAL", POSITIONS_PATH), ("SHADOW", SHADOW_PATH)):
        store = PositionStore(path)
        store.load()
        print(f"  {label} positions: {len(store.all())}")
        for p in store.all():
            print(f"    {p.token_symbol} ${p.usd_size} entry={p.entry_price_usd} "
                  f"exits={len(p.exited_by)}/{len(p.cluster_wallets)}")
    print(f"\n  shadow_mode: {cfg.get('shadow_mode')}")
    print(f"  budget:      ${cfg.get('total_budget_usd')} total / ${cfg.get('slice_usd')} per slice")
    print(f"  last scan:   {state.get('last_scan_at', 'never')}")
    print(f"  last block:  {state.get('last_processed_block', '-')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy-Trade Monitor v2 (cluster+shadow)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    if args.status:
        show_status()
    elif args.scan:
        run_scan(once=True)
    else:
        run_scan(once=False)


if __name__ == "__main__":
    main()
