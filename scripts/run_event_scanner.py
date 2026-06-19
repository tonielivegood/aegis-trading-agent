"""DRY_RUN runner for the Aegis event radar.

Pipeline: manual event feed -> catalyst scoring -> (live or stub) 5-minute market
confirmation -> combined score -> gated candidates. Prints what WOULD be traded.
Never signs, never broadcasts. Use:  python scripts/run_event_scanner.py

By default the market-confirmation snapshot is a neutral stub (no live quote), so
this is safe and offline. Pass --live-quote to pull a real PancakeSwap price/route
for slippage (still read-only, no trade).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.aegis import event_signal_scanner as scanner
from src.agent.aegis.events import ManualJsonEventSource, load_project_sources
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot, assess
from src.agent.data import token_list
from src.agent.strategy import event_driven_alpha_momentum as edam


def _stub_snapshot(symbol: str, contract: str) -> MarketSnapshot:
    """Neutral, safe placeholder confirmation (no live data). Demonstrates the
    pipeline; replace with a live price/volume feed for production."""
    return MarketSnapshot(
        symbol=symbol, contract=contract,
        vol_5m=0.0, baseline_vol=0.0, price_now=1.0, price_5m_ago=1.0,
        recent_pump_pct=0.0, slippage_est=0.0, has_route=True, liquidity_ok=True,
    )


def main() -> None:
    sources = load_project_sources()
    events = ManualJsonEventSource().fetch()
    print(f"Loaded {len(events)} manual events, {len(sources)} project sources.\n")

    scores = scanner.scan(events, project_sources=sources)
    candidates = []
    for key, es in scores.items():
        snap = _stub_snapshot(es.token, es.contract)
        conf = assess(snap)
        c = edam.make_candidate(es, conf, snap)
        candidates.append(c)

    print(f"{'token':<14}{'event':>7}{'conf':>6}{'risk':>6}{'combined':>10}"
          f"{'elig':>6}{'liq':>5}")
    print("-" * 54)
    for c in sorted(candidates, key=lambda x: x.combined_score, reverse=True):
        print(f"{c.symbol:<14}{c.event_score:>7.0f}{c.confirmation_score:>6.0f}"
              f"{c.risk_penalty:>6.0f}{c.combined_score:>10.0f}"
              f"{str(c.eligible):>6}{str(c.tradable):>5}")

    threshold = edam.settings.event_signal_threshold
    passing = [c for c in candidates
               if c.combined_score >= threshold and c.eligible and c.tradable]
    print(f"\nThreshold = {threshold:.0f}. "
          f"{len(passing)} candidate(s) would qualify for entry gating "
          f"(still DRY_RUN; market confirmation is a neutral stub here).")
    print(f"Tradable-alpha universe size: {len(token_list.alpha_symbols())}")


if __name__ == "__main__":
    main()
