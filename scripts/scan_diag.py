"""Read-only sniper scan diagnostic.

Replays ONE live scan against the real tradable universe and prints, per token,
how close it is to firing a breakout entry: volume multiple (vol_5m/baseline),
breakout %, recent pump, route/liquidity, and the class bar it must clear.

Never creates orders, never signs. Safe to run alongside the live service.

    cd /home/agent/bnbhack-track1-agent && .venv/bin/python /tmp/scan_diag.py
"""
from __future__ import annotations

from src.agent.agent_loop import _event_prices, _volume_provider
from src.agent.aegis import token_class as tc
from src.agent.aegis.market_feed import MarketFeed
from src.agent.aegis.volume_breakout import breakout_pct
from src.agent.config import settings
from src.agent.data import token_list
from src.agent.risk.portfolio import read_onchain_balances


def main() -> None:
    symbols = token_list.alpha_symbols()
    balances = read_onchain_balances(settings.agent_wallet_address)
    prices = _event_prices(symbols, balances)
    feed = MarketFeed(volume_provider=_volume_provider())
    snaps = feed.build_snapshots(sorted(symbols), prices)

    rows = []
    for sym, s in snaps.items():
        cls = token_list.token_class(sym)
        bar = tc.params(cls).vol_mult
        vm = (s.vol_5m / s.baseline_vol) if s.baseline_vol > 0 else 0.0
        bo = breakout_pct(s)   # same-source kline move when available (matches live entry)
        rows.append((vm / bar if bar else 0.0, sym, cls, bar, vm, bo, s))

    rows.sort(reverse=True)
    print(f"universe={len(symbols)} snapshots={len(snaps)} "
          f"with_baseline={sum(1 for r in rows if r[6].baseline_vol > 0)} "
          f"liquid={sum(1 for r in rows if r[6].has_route and r[6].liquidity_ok)}")
    print(f"{'SYM':<12}{'CLS':<6}{'bar':>5}{'vol_x':>8}{'%bar':>7}"
          f"{'bo%':>7}{'pump%':>7}{'route':>6}{'liq':>5}{'slip':>7}")
    fired = 0
    for frac, sym, cls, bar, vm, bo, s in rows[:30]:
        gate = ""
        if not (s.has_route and s.liquidity_ok):
            gate = "noLiq"
        elif s.baseline_vol <= 0:
            gate = "noVol"
        elif vm < bar:
            gate = "lowVol"
        elif bo <= 0 or bo < tc.params(cls).breakout_min:
            gate = "flat"
        elif bo > tc.params(cls).breakout_max:
            gate = "tooHot"
        elif s.recent_pump_pct >= settings.aegis_overpump_pct:
            gate = "pumped"
        else:
            gate = "FIRE"
            fired += 1
        print(f"{sym:<12}{cls:<6}{bar:>5.1f}{vm:>8.2f}{frac * 100:>6.0f}%"
              f"{bo * 100:>6.1f}%{s.recent_pump_pct * 100:>6.1f}%"
              f"{str(s.has_route):>6}{str(s.liquidity_ok):>5}{s.slippage_est:>7.3f}  {gate}")
    print(f"\nwould-fire this scan: {fired}")


if __name__ == "__main__":
    main()
