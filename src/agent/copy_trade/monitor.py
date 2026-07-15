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

ROOT = Path(__file__).resolve().parents[3]
ENV = dotenv_values(ROOT / ".env")
API_KEY = ENV.get("MORALIS_API_KEY", "")
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"
STATE_PATH = ROOT / "data" / "copy_trade" / "state.json"

MORALIS = "https://deep-index.moralis.io/api/v2.2"
HEADERS = {"X-API-Key": API_KEY, "accept": "application/json"}


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


def parse_swap(tx: dict) -> dict | None:
    """Parse một swap transaction thành format dễ đọc."""
    try:
        summary = tx.get("summary", "")
        ts = tx.get("block_timestamp", "")
        tx_hash = tx.get("hash", "")

        # Parse erc20 transfers để tìm token in/out
        transfers = tx.get("erc20_transfers", [])
        token_in = None
        token_out = None
        for tr in transfers:
            direction = tr.get("direction", "")
            sym = tr.get("token_symbol", "???")
            val = float(tr.get("value_formatted") or 0)
            addr = tr.get("token_address", "")
            if direction == "send":
                token_in = {"symbol": sym, "amount": val, "address": addr}
            elif direction == "receive":
                token_out = {"symbol": sym, "amount": val, "address": addr}

        # Parse native transfers (BNB in/out)
        for nt in tx.get("native_transfers", []):
            direction = nt.get("direction", "")
            val = float(nt.get("value_formatted") or 0)
            if direction == "send" and not token_in:
                token_in = {"symbol": "BNB", "amount": val, "address": "native"}
            elif direction == "receive" and not token_out:
                token_out = {"symbol": "BNB", "amount": val, "address": "native"}

        return {
            "hash": tx_hash,
            "timestamp": ts,
            "summary": summary,
            "token_in": token_in,
            "token_out": token_out,
            "block_number": tx.get("block_number", ""),
        }
    except Exception as e:
        _log("WARN", f"Failed to parse swap", error=str(e))
        return None


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

        parsed = parse_swap(tx)
        if not parsed:
            continue

        # Skip ignored tokens
        in_sym = (parsed.get("token_in") or {}).get("symbol", "")
        out_sym = (parsed.get("token_out") or {}).get("symbol", "")
        if in_sym in ignore_tokens or out_sym in ignore_tokens:
            processed.add(tx_hash)
            continue

        alert = {
            "wallet": address,
            "wallet_label": label,
            "detected_at": _ts(),
            **parsed,
        }
        new_alerts.append(alert)
        processed.add(tx_hash)

        _log("ALERT", f"NEW SWAP on [{label}]",
             summary=parsed["summary"],
             timestamp=parsed["timestamp"])

    state["processed_txs"] = list(processed)
    state["last_checked"][address] = _ts()
    return new_alerts


def send_email_alert(alert: dict):
    """Gửi email thông báo alert tới người dùng qua SMTP Gmail."""
    server_host = ENV.get("SMTP_SERVER", "smtp.gmail.com")
    port_val = ENV.get("SMTP_PORT", "587")
    user = ENV.get("SMTP_USER", "")
    password = ENV.get("SMTP_PASSWORD", "")
    to_email = ENV.get("NOTIFICATION_EMAIL", "")

    if not user or not password:
        _log("WARN", "SMTP credentials (SMTP_USER/SMTP_PASSWORD) missing in .env. Email notification skipped.")
        return

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        port = int(port_val)
        
        # Soạn nội dung email
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to_email
        msg["Subject"] = f"[AEGIS ALERT] New Swap on {alert.get('wallet_label')}"

        body = f"""Hệ thống Aegis phát hiện giao dịch swap mới!

- Ví nguồn: {alert.get('wallet_label')} ({alert.get('wallet')})
- Thời gian: {alert.get('timestamp')}
- Tóm tắt: {alert.get('summary')}

Chi tiết giao dịch:
"""
        if alert.get("token_in"):
            ti = alert["token_in"]
            body += f"  * Bán (Sold): {ti['amount']:.6f} {ti['symbol']} ({ti['address']})\n"
        if alert.get("token_out"):
            to = alert["token_out"]
            body += f"  * Mua (Bought): {to['amount']:.6f} {to['symbol']} ({to['address']})\n"

        body += f"\nLink kiểm tra trên BscScan:\nhttps://bscscan.com/tx/{alert.get('hash')}\n"
        body += f"\nHệ thống tự động Aegis Trading Bot."

        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Gửi qua SMTP
        server = smtplib.SMTP(server_host, port)
        server.starttls()
        server.login(user, password)
        server.sendmail(user, to_email, msg.as_string())
        server.quit()
        _log("INFO", f"Email alert sent successfully to {to_email}")
    except Exception as e:
        _log("ERROR", "Failed to send email alert", error=str(e))


def run_scan(once: bool = False):
    """Chạy 1 vòng scan hoặc loop liên tục."""
    config = _load_json(CONFIG_PATH)
    state = _load_json(STATE_PATH)
    settings = config.get("copy_settings", {})
    interval = settings.get("poll_interval_seconds", 30)

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

        if all_new_alerts:
            state.setdefault("alerts", []).extend(all_new_alerts)
            _save_json(STATE_PATH, state)
            _log("INFO", f"SUCCESS: {len(all_new_alerts)} new swap(s) detected!")

            for a in all_new_alerts:
                print(f"\n  {'='*60}")
                print(f"  [SWAP ALERT] — [{a['wallet_label']}]")
                print(f"  Time:    {a.get('timestamp', '?')}")
                print(f"  Summary: {a.get('summary', '?')}")
                if a.get("token_in"):
                    ti = a["token_in"]
                    print(f"  Sold:    {ti['amount']:.6f} {ti['symbol']} ({ti['address'][:16]}...)")
                if a.get("token_out"):
                    to = a["token_out"]
                    print(f"  Bought:  {to['amount']:.6f} {to['symbol']} ({to['address'][:16]}...)")
                print(f"  TX:      https://bscscan.com/tx/{a['hash']}")
                print(f"  {'='*60}")

                # Gửi email thông báo
                send_email_alert(a)

                # Copy trade signal
                if settings.get("auto_execute"):
                    _log("EXEC", "Auto-execute enabled — would copy this swap")
                    # TODO: integrate with best_execution.py
                else:
                    _log("INFO", "Alert-only mode — manual review required")
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
