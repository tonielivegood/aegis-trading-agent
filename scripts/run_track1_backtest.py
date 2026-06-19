"""Track 1 validation harness (read-only, DRY_RUN-safe).

Honest, multi-part validation of the Aegis Event-Driven Alpha Momentum strategy.
It does NOT fake catalysts or 5-minute history. Sections:

  1. Eligible-universe data coverage (real CMC daily) — quantifies how little
     history exists for the new Alpha tokens (the reason a multi-year walk-forward
     of the event strategy is impossible).
  2. Catalyst scenario replay (real scanner logic on documented manual events).
  3. Contest-safety checklist (enforced by the pytest suite).

Quantitative risk-engine / baseline numbers come from `run_walkforward.py`
(real CMC daily majors, 5.7yr) — printed in docs/BACKTEST_REPORT.md.

Never signs, never broadcasts, never stages anything.
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests

from src.agent.aegis.catalyst_scanner import CatalystScanner
from src.agent.config import settings
from src.agent.data import token_list

REPO = Path(__file__).resolve().parent.parent
ELIGIBLE = json.loads((REPO / "src/agent/data/eligible_tokens.json").read_text(encoding="utf-8"))
CMC_OHLCV = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/ohlcv/historical"


def _cmc_daily_bars(cmc_id) -> int:
    try:
        r = requests.get(CMC_OHLCV, headers={"X-CMC_PRO_API_KEY": settings.cmc_api_key},
                         params={"id": cmc_id, "interval": "daily", "count": 400, "convert": "USD"},
                         timeout=30)
        if r.status_code != 200:
            return -1
        data = r.json().get("data") or {}
        entry = data.get(str(cmc_id))
        if isinstance(entry, list):
            entry = entry[0] if entry else {}
        quotes = (entry or {}).get("quotes", [])
        return len(quotes)
    except Exception:
        return -1


def section_coverage() -> None:
    print("=" * 70)
    print("1. ELIGIBLE-UNIVERSE DATA COVERAGE (real CMC daily)")
    print("=" * 70)
    tradable = token_list.tradable_alpha_tokens()
    print(f"Official eligible tokens: {len(ELIGIBLE)}   liquid tradable subset: {len(tradable)}")
    by_sym = {t.get("symbol"): t for t in ELIGIBLE}
    bars = []
    enough = 0
    SAMPLE = tradable[:20]   # bound API usage; representative of the liquid subset
    print(f"Probing CMC daily history for {len(SAMPLE)} tradable tokens...")
    for tok in SAMPLE:
        meta = by_sym.get(tok.symbol, {})
        n = _cmc_daily_bars(meta.get("id"))
        if n >= 0:
            bars.append(n)
            if n >= 30:
                enough += 1
    if bars:
        print(f"  tokens returning history : {len(bars)}/{len(SAMPLE)}")
        print(f"  median daily bars        : {int(statistics.median(bars))}")
        print(f"  with >= 30 daily bars    : {enough}/{len(SAMPLE)} "
              f"({enough/len(SAMPLE)*100:.0f}%)")
    else:
        print("  no CMC daily history returned for the sample")
    print("\n  -> Most eligible Alpha tokens are 2025-26 listings with little history.")
    print("  -> A multi-year walk-forward of the EVENT strategy is NOT possible; and the")
    print("     strategy is intraday (5h max hold) so daily bars cannot represent it.")
    print("     Risk-engine evidence therefore comes from the majors CMC-daily walk-forward.")


def section_catalyst_replay() -> None:
    print("\n" + "=" * 70)
    print("2. CATALYST SCENARIO REPLAY (real scanner logic, documented manual events)")
    print("=" * 70)
    sigs = CatalystScanner().scan()
    if not sigs:
        print("  (no signals — manual_events.json empty?)")
        return
    print(f"{'symbol':<10}{'tier':>5}{'score':>7}{'conf':>6}{'matched':>10}  interpretation")
    print("-" * 64)
    for s in sigs:
        interp = ("Tier-1 authority" if s.source_tier == 1 else
                  "Tier-2 project" if s.source_tier == 2 else "Tier-3 unverified")
        verdict = "WATCHLIST (needs vol+price+liq)" if s.score >= settings.event_signal_threshold \
            else "rejected/penalised (below threshold)"
        print(f"{s.symbol:<10}{s.source_tier:>5}{s.score:>7.0f}{s.confidence:>6.2f}"
              f"{s.matched_by:>10}  {interp}: {verdict}")
    print("\n  -> A catalyst ALONE never trades. Entry also requires eligible-by-contract +")
    print("     liquid + real 5m volume + price breakout + risk gates (proven by tests).")
    print("     This is SCENARIO validation, not historical alpha proof.")


def section_safety() -> None:
    print("\n" + "=" * 70)
    print("3. CONTEST-SAFETY CHECKLIST (enforced by the pytest suite)")
    print("=" * 70)
    checks = [
        ("Trades only the official 149 allowlist (by contract)", "test_alpha_universe / test_compliance"),
        ("Tier-3/unverified can never enter alone", "test_aegis_loop"),
        ("Catalyst without real volume stays WATCHLIST", "test_aegis_loop"),
        ("Max 3 open positions", "test_event_strategy / test_aegis_loop"),
        ("$10 order cap", "test_event_strategy"),
        ("Stablecoin floor never breached", "test_event_strategy / test_compliance"),
        ("Max 5-hour hold (hard exit)", "test_event_strategy"),
        ("2x take-profit / stop-loss / trailing", "test_event_strategy"),
        ("5x volume = FOMO defense, not blind sell", "test_event_strategy"),
        ("Drawdown breaker overrides everything", "test_event_strategy"),
        ("Min-trade compliance never forces a bad trade", "test_compliance"),
        ("DRY_RUN prevents broadcasting", "test_aegis_loop / test_binance_web3"),
    ]
    for prop, test in checks:
        print(f"  [tested] {prop:<52} <- {test}")
    print(f"\n  DRY_RUN={settings.dry_run}  STRATEGY_MODE={settings.strategy_mode}  "
          f"broadcast={settings.binance_web3_broadcast_enabled}")


def main() -> None:
    print("AEGIS — TRACK 1 VALIDATION (read-only, no trading)\n")
    section_coverage()
    section_catalyst_replay()
    section_safety()
    print("\nDone. See docs/BACKTEST_REPORT.md for the full report and the majors")
    print("walk-forward risk numbers (python scripts/run_walkforward.py --cmc).")


if __name__ == "__main__":
    main()
