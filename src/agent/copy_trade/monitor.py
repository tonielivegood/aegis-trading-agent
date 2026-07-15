"""Copy-Trade Monitor — Theo dõi real-time swap từ cluster ví Top 1 Hackathon.

Chạy:
    python -m src.agent.copy_trade.monitor           # monitor loop
    python -m src.agent.copy_trade.monitor --status   # check trạng thái
    python -m src.agent.copy_trade.monitor --scan     # scan 1 lần rồi thoát

Cách hoạt động:
    1. Poll Moralis API mỗi 30s để lấy lịch sử giao dịch mới nhất
    2. Lọc các "token swap" chưa xử lý
    3. Ghi alert vào state.json + in ra console
    4. Nếu auto_execute = true → gọi best_execution để thực hiện swap giống
"""
from __future__ import annotations

import json
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import dotenv_values

from ..config import settings
from ..data.token_list import register_discovered
from ..email_notifier import EmailNotifier
from ..execution.oneinch import OneInch
from ..execution.openocean import OpenOcean
from ..execution.pancakeswap import PancakeSwap
from .budget import CopyTradeBudget
from .executor import handle_alert
from .positions import PositionStore
from .swap_parser import parse_swap

ROOT = Path(__file__).resolve().parents[3]
ENV = dotenv_values(ROOT / ".env")
API_KEY = ENV.get("MORALIS_API_KEY", "")
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"
STATE_PATH = ROOT / "data" / "copy_trade" / "state.json"

MORALIS = "https://deep-index.moralis.io/api/v2.2"
HEADERS = {"X-API-Key": API_KEY, "accept": "application/json"}

POSITIONS_PATH = ROOT / "data" / "copy_trade" / "positions.json"


def _reconcile_after_restart(budget: CopyTradeBudget, store: PositionStore) -> None:
    """Replay disk-persisted state into the fresh in-RAM runtime (C2 + C3).

    The token registry (`register_discovered`) and the budget tracker are both
    RAM-only and start empty on every process. After a restart while holding open
    positions, we must (C2) re-register each position's token so a mirror-sell can
    resolve it via `get_token()`, and (C3) re-consume its budget slice so we never
    over-allocate past the $15.39 hard cap. Without this the exact orphan-position /
    over-allocation bugs this branch exists to fix reappear on the next deploy."""
    for p in store.all():
        register_discovered(p.token_symbol, p.token_address, p.token_decimals)
        # ponytail: guard so a hand-edited/over-full positions.json can't crash
        # startup (allocate() raises when below one slice) into a systemd restart loop.
        if budget.can_open_new():
            budget.allocate()


def _build_runtime():
    """One-time construction of the shared budget tracker, position store, and
    executor pool — called once from main(), passed down into the scan loop."""
    config = _load_json(CONFIG_PATH)
    settings_ = config.get("copy_settings", {})
    budget = CopyTradeBudget(
        total_usd=settings_.get("total_budget_usd", 15.39),
        slice_usd=settings_.get("slice_usd", 1.5),
    )
    store = PositionStore(POSITIONS_PATH)
    store.load()
    _reconcile_after_restart(budget, store)
    # C1: build executors with a real signing account (live) exactly like
    # agent_loop._make_executor_for, so live swaps never crash on a missing account.
    account = None
    if not settings.dry_run:
        from eth_account import Account
        account = Account.from_key(settings.agent_private_key)
    executors = {
        "1inch": OneInch(account=account, dry_run=settings.dry_run),
        "openocean": OpenOcean(account=account, dry_run=settings.dry_run),
        "pancake": PancakeSwap(account=account, dry_run=settings.dry_run),
    }
    return budget, store, executors


# ─────────────────────── helpers ───────────────────────


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(level: str, msg: str, **kw):
    extra = " | ".join(f"{k}={v}" for k, v in kw.items()) if kw else ""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg} {extra}")


# ─────────────────────── core ───────────────────────


def fetch_recent_swaps(address: str, limit: int = 25) -> list[dict]:
    """Lấy danh sách swap gần đây từ Moralis wallet history."""
    url = f"{MORALIS}/wallets/{address}/history"
    params = {"chain": "bsc", "limit": limit}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        results = r.json().get("result", [])
        return [tx for tx in results if tx.get("category") == "token swap"]
    except Exception as e:
        _log("ERROR", f"Failed to fetch swaps for {address[:12]}...", error=str(e))
        return []


def check_wallet(address: str, label: str, state: dict, config: dict) -> list[dict]:
    """Check một ví và trả về danh sách alert mới."""
    settings = config.get("copy_settings", {})
    min_usd = settings.get("min_swap_usd", 5.0)
    ignore_tokens = set(settings.get("ignore_tokens", []))
    processed = set(state.get("processed_txs", []))

    swaps = fetch_recent_swaps(address)
    new_alerts = []

    for tx in swaps:
        tx_hash = tx.get("hash", "")
        if tx_hash in processed:
            continue

        parsed = parse_swap(tx, address)
        if not parsed:
            processed.add(tx_hash)
            continue

        if parsed.token_symbol in ignore_tokens or parsed.token_address.lower() in {
            t.lower() for t in ignore_tokens
        }:
            processed.add(tx_hash)
            continue

        alert = {
            "wallet": address,
            "wallet_label": label,
            "detected_at": _ts(),
            "parsed": parsed,
        }
        new_alerts.append(alert)
        processed.add(tx_hash)

        _log("ALERT", f"NEW SWAP on [{label}]",
             symbol=parsed.token_symbol, direction=parsed.direction)

    state["processed_txs"] = list(processed)
    state["last_checked"][address] = _ts()
    return new_alerts


def run_scan(once: bool = False):
    """Chạy 1 vòng scan hoặc loop liên tục."""
    config = _load_json(CONFIG_PATH)
    state = _load_json(STATE_PATH)
    settings = config.get("copy_settings", {})
    interval = settings.get("poll_interval_seconds", 30)
    budget, store, executors = _build_runtime()
    try:
        notifier = EmailNotifier()
    except ValueError:
        notifier = None
    consecutive_failures = 0

    wallets = [w for w in config.get("target_wallets", []) if w.get("monitor")]
    wallets.sort(key=lambda w: w.get("priority", 99))

    _log("INFO", f"Copy-Trade Monitor started", wallets=len(wallets), interval=f"{interval}s")
    print(f"  Monitoring {len(wallets)} wallets:")
    for w in wallets:
        print(f"    [{w['label']}] {w['address'][:12]}... ({w['role'][:50]})")

    iteration = 0
    while True:
        iteration += 1
        _log("INFO", f"=== Scan #{iteration} ===")

        all_new_alerts = []
        for w in wallets:
            alerts = check_wallet(w["address"], w["label"], state, config)
            all_new_alerts.extend(alerts)
            time.sleep(0.2)  # Rate limit

        # A wallet only lands in last_checked when fetch_recent_swaps didn't except —
        # 401s inside fetch_recent_swaps are already caught there and logged, but the
        # wallet's last_checked timestamp is still written by check_wallet() either
        # way, so use a request-level probe instead: re-check the most recently seen
        # HTTP status via a lightweight one-off call every 10 iterations.
        if iteration % 10 == 0:
            probe = requests.get(
                f"{MORALIS}/wallets/{wallets[0]['address']}/history",
                headers=HEADERS, params={"chain": "bsc", "limit": 1}, timeout=10,
            )
            if probe.status_code == 401:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if consecutive_failures == 1 and notifier is not None:  # alert once, not every 10 iters
                notifier.send_alert(
                    "[AEGIS COPY-TRADE] Moralis auth failing",
                    f"wallets/history returned 401 at iteration {iteration}. "
                    f"MORALIS_API_KEY likely invalid/expired — copy-trade monitor is blind until fixed.",
                )

        if all_new_alerts:
            state.setdefault("alerts", []).extend(all_new_alerts)
            _save_json(STATE_PATH, state)
            _log("INFO", f"SUCCESS: {len(all_new_alerts)} new swap(s) detected!")

            for a in all_new_alerts:
                p = a["parsed"]
                print(f"\n  {'='*60}")
                print(f"  [SWAP ALERT] — [{a['wallet_label']}]")
                print(f"  Time:      {p.timestamp}")
                print(f"  Direction: {p.direction.upper()} {p.token_symbol}")
                print(f"  Amount:    {p.token_amount:.6f} {p.token_symbol} ({p.token_address[:16]}...)")
                print(f"  TX:        https://bscscan.com/tx/{p.hash}")
                print(f"  {'='*60}")

                # ponytail: guard against notifier=None (set above when SMTP creds are
                # missing) — without it, a missing-config gap crashes the scan loop with
                # AttributeError instead of being swallowed like the brief intended.
                if notifier is not None:
                    try:
                        notifier.send_alert(
                            f"[AEGIS COPY-TRADE] {a['parsed'].direction.upper()} {a['parsed'].token_symbol}",
                            f"Wallet: {a['wallet_label']} ({a['wallet']})\n"
                            f"Direction: {a['parsed'].direction}\n"
                            f"Token: {a['parsed'].token_symbol} ({a['parsed'].token_address})\n"
                            f"Amount: {a['parsed'].token_amount}\n"
                            f"TX: https://bscscan.com/tx/{a['parsed'].hash}\n",
                        )
                    except ValueError:
                        pass  # SMTP not configured — alert still logged to console above

                if settings.get("auto_execute"):
                    # I2: a reverted swap (slippage/gas/honeypot) raises RuntimeError —
                    # routine in live trading. Guard it so one bad alert never kills the
                    # scan loop (which would crash-loop under systemd Restart=always).
                    try:
                        handle_alert(a["parsed"], budget, store, executors)
                    except Exception as e:  # noqa: BLE001
                        _log("ERROR", "handle_alert failed — skipping this alert",
                             symbol=a["parsed"].token_symbol, error=str(e))
        else:
            _log("INFO", "No new swaps detected")
            _save_json(STATE_PATH, state)

        if once:
            break

        _log("INFO", f"Sleeping {interval}s...")
        time.sleep(interval)


def show_status():
    """Hiển thị trạng thái hiện tại của hệ thống."""
    config = _load_json(CONFIG_PATH)
    state = _load_json(STATE_PATH)

    print("\n" + "=" * 60)
    print("  COPY-TRADE MONITOR STATUS")
    print("=" * 60)

    wallets = config.get("target_wallets", [])
    print(f"\n  Configured wallets: {len(wallets)}")
    for w in wallets:
        monitored = "[ON]" if w.get("monitor") else "[OFF]"
        last = state.get("last_checked", {}).get(w["address"], "never")
        print(f"    {monitored} [{w['label']:<12}] {w['address'][:16]}... | last: {last}")

    alerts = state.get("alerts", [])
    processed = state.get("processed_txs", [])
    print(f"\n  Total alerts: {len(alerts)}")
    print(f"  Processed txs: {len(processed)}")

    settings = config.get("copy_settings", {})
    print(f"\n  Settings:")
    print(f"    Auto-execute: {settings.get('auto_execute', False)}")
    print(f"    Alert-only:   {settings.get('alert_only', True)}")
    print(f"    Min swap USD: ${settings.get('min_swap_usd', 5)}")
    print(f"    Max copy USD: ${settings.get('max_copy_usd', 50)}")
    print(f"    Poll interval: {settings.get('poll_interval_seconds', 30)}s")

    if alerts:
        print(f"\n  Recent alerts:")
        for a in alerts[-5:]:
            print(f"    [{a.get('wallet_label','')}] {a.get('detected_at','')} — {a.get('summary','')}")

    print()


# ─────────────────────── CLI ───────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Copy-Trade Monitor — Theo dõi swap từ cluster ví Top 1 Hackathon"
    )
    parser.add_argument("--status", action="store_true", help="Hiển thị trạng thái hệ thống")
    parser.add_argument("--scan", action="store_true", help="Scan 1 lần rồi thoát")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.scan:
        run_scan(once=True)
    else:
        run_scan(once=False)


if __name__ == "__main__":
    main()
