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


def cmd_compliance() -> None:
    import time

    from .aegis.compliance import ComplianceTracker
    rep = ComplianceTracker.load(agent_loop.COMPLIANCE_FILE).report(time.time())
    print("Track 1 — minimum-trade compliance report")
    print(f"  date                  : {rep.date}")
    print(f"  valid trades today    : {rep.valid_trades_today} "
          f"(need {settings.track1_min_trades_per_day}/day, remaining {rep.remaining_today})")
    print(f"  valid trades total    : {rep.valid_trades_total} "
          f"(need {settings.track1_min_trades_total}/week, remaining {rep.remaining_total})")
    print(f"  last valid trade      : {rep.last_valid_trade}")
    print(f"  invalid trades ignored: {rep.invalid_trades_ignored} (outside the 149 allowlist)")
    print(f"  scoring mode          : {settings.track1_scoring_mode} "
          f"(NAV assumption: {settings.track1_score_nav_assumption})")
    print(f"  settlement asset      : {settings.track1_settlement_asset}")


def cmd_signals() -> None:
    """Show the live CMC AI Agent Hub signals and how they steer the agent (read-only).

    A judge runs this to SEE the #CMCAgentHub integration working: the Fear & Greed
    score, the community-trending set, and the regime they produce — all from the
    Agent Hub REST skills, fully fail-safe.
    """
    from .aegis import regime as rg
    from .aegis.volume_breakout import TRENDING_BOOST
    from .data import cmc_agent_hub, cmc_client

    fng = cmc_agent_hub.get_fear_greed()
    trending = sorted(cmc_agent_hub.get_trending_symbols())
    try:
        btc = cmc_client.get_quotes(["BTC"]).get("BTC", {})
    except Exception:  # noqa: BLE001 — diagnostic command, never traceback at a judge
        btc = {}

    print("CMC AI Agent Hub — live signals (#CMCAgentHub)")
    if fng:
        print(f"  Fear & Greed      : {fng['value']} ({fng['classification']})")
    else:
        print("  Fear & Greed      : <unavailable> (fails safe → BTC-only regime)")
    print(f"  Community trending : {', '.join(trending) if trending else '<none>'}")
    if btc:
        flag, reason = rg.decide_regime(btc, fear_greed=fng)
        print(f"  Resulting regime   : {flag.value}   [{reason}]")
    else:
        print("  Resulting regime   : <BTC quote unavailable>")
    print("  How it steers the agent:")
    print(f"    • sentiment TIGHTENS risk only — RISK_ON→CAUTIOUS at F&G ≤ {rg.SENTIMENT_FEAR_FLOOR} "
          f"(never loosens)")
    print(f"    • a breakout that is ALSO community-trending gets a {TRENDING_BOOST:g}× rank boost "
          f"for the scarce slots")


def main() -> None:
    configure()
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("compliance")  # Track-1 min-trade compliance report
    sub.add_parser("signals")  # show live CMC AI Agent Hub signals + resulting regime
    sub.add_parser("reset")  # clear runtime state (run once before the contest)
    sub.add_parser("notify-test")  # send a test Telegram alert
    rp = sub.add_parser("run")
    rp.add_argument("--live", action="store_true", help="enable live trading (default: dry-run)")
    tp = sub.add_parser("tick")
    tp.add_argument("--live", action="store_true")
    pp = sub.add_parser("panic")  # KILL-SWITCH: sell everything to USDT + clear books
    pp.add_argument("--live", action="store_true", help="actually sell (default: dry-run preview)")
    args = p.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "compliance":
        cmd_compliance()
    elif args.cmd == "signals":
        cmd_signals()
    elif args.cmd == "reset":
        for f in (agent_loop.DRAWDOWN_FILE, agent_loop.TRADES_FILE, agent_loop.BASELINE_FILE,
                  agent_loop.POSITIONS_FILE, agent_loop.COMPLIANCE_FILE,
                  agent_loop.COOLDOWN_FILE, agent_loop.REGIME_FILE, agent_loop.CMC_SIGNAL_FILE,
                  agent_loop.CLAUDE_FILE):
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
    elif args.cmd == "panic":
        result = agent_loop.flatten_to_cash(dry_run=not args.live)
        print(("LIVE" if args.live else "DRY-RUN (use --live to execute)"), "flatten:", result)


if __name__ == "__main__":
    main()
