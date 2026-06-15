"""CLI entrypoint.

    python -m src.agent status        # show wallet, equity, registration, drawdown
    python -m src.agent run           # run the scheduler in DRY-RUN (default, safe)
    python -m src.agent run --live    # run the scheduler with LIVE trading
    python -m src.agent tick          # execute a single tick (dry-run unless --live)
"""
from __future__ import annotations

import argparse

from . import agent_loop, scheduler
from .config import settings
from .monitor.logger import configure, get_logger

log = get_logger("agent")


def cmd_status() -> None:
    from .risk.portfolio import Portfolio, read_onchain_balances
    from .data import price_feed, token_list

    bals = read_onchain_balances(settings.agent_wallet_address)
    syms = [s for s in bals if s != "BNB"]
    prices = price_feed.get_prices(syms) if syms else {}
    if "BNB" in bals:
        prices["BNB"] = price_feed.onchain_price_usd("BNB") or 0.0
    equity = Portfolio().equity(bals, prices)

    print(f"Wallet   : {settings.agent_wallet_address}")
    print(f"Mode     : {'DRY-RUN' if settings.dry_run else 'LIVE'}")
    print(f"Equity   : ${equity:.2f}")
    print(f"Balances : {{ {', '.join(f'{k}: {v:.4f}' for k, v in bals.items())} }}")
    print(f"Tradable : {len(token_list.tradable_symbols())} core tokens")


def main() -> None:
    configure()
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reset")  # clear runtime state (run once before the contest)
    sub.add_parser("notify-test")  # send a test Telegram alert
    rp = sub.add_parser("run")
    rp.add_argument("--live", action="store_true", help="enable live trading (default: dry-run)")
    tp = sub.add_parser("tick")
    tp.add_argument("--live", action="store_true")
    args = p.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "reset":
        for f in (agent_loop.DRAWDOWN_FILE, agent_loop.TRADES_FILE, agent_loop.BASELINE_FILE):
            if f.exists():
                f.unlink()
                print(f"removed {f.name}")
        print("Runtime state reset — drawdown peak and trade ledger cleared.")
    elif args.cmd == "notify-test":
        from .monitor import notifier
        if not notifier.is_enabled():
            print("Telegram disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        else:
            ok = notifier.send("✅ Test alert from BNB Hack Track1 agent")
            print("Sent OK — check your Telegram." if ok else "Send FAILED — check token/chat_id.")
    elif args.cmd == "run":
        scheduler.run(dry_run=not args.live)
    elif args.cmd == "tick":
        result = agent_loop.tick(dry_run=not args.live)
        print(result)


if __name__ == "__main__":
    main()
