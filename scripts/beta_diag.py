"""Read-only beta-core diagnostic (barbell Phase-2 soak tool).

Shows what the beta core WOULD do right now WITHOUT trading: the major universe ranked
by blended (1h+24h) momentum, the basket it would hold, and the simulated orders given
the current wallet/book/regime. Never signs, never persists — safe to run alongside the
live agent (the live tick ignores beta-core until BETA_CORE_ENABLED=true).

    cd /home/agent/bnbhack-track1-agent && .venv/bin/python scripts/beta_diag.py
"""
from __future__ import annotations

import time

from src.agent.aegis import beta_core as bc
from src.agent.aegis import regime as rg
from src.agent.agent_loop import (
    POSITIONS_FILE, REGIME_FILE, _event_prices,
)
from src.agent.aegis.positions import PositionBook
from src.agent.config import settings
from src.agent.data import cmc_client, token_list
from src.agent.risk.portfolio import Portfolio, read_onchain_balances
from src.agent.strategy.base_strategy import PortfolioState


def main() -> None:
    now = time.time()
    majors = [s for s in token_list.alpha_symbols() if token_list.token_class(s) == "major"]
    balances = read_onchain_balances(settings.agent_wallet_address)
    prices = _event_prices(majors, balances)

    # Momentum sourced by CMC id (same as pricing) to avoid same-symbol collisions.
    id_of = {s: token_list.cmc_id(s) for s in majors}
    try:
        by_id = cmc_client.get_quotes_by_id([i for i in id_of.values() if i])
    except Exception as e:  # noqa: BLE001 — diagnostic must never traceback
        print(f"CMC quotes unavailable ({type(e).__name__}); cannot rank momentum.")
        return
    quotes = {s: by_id[i] for s, i in id_of.items() if i and i in by_id}
    momentum = bc.build_momentum(quotes, w_1h=settings.beta_core_mom_w1h)

    flag = rg.current_regime(rg.RegimeState.load(REGIME_FILE),
                             max_age_s=settings.regime_max_age_seconds, now=now)
    pf = Portfolio()
    equity = pf.equity(balances, prices)
    stable = pf.stable_value(balances, prices)
    position_usd = equity * settings.beta_core_position_pct
    floor_usd = max(settings.stablecoin_floor_usd, equity * settings.stablecoin_floor_pct)

    basket = bc.select_basket(momentum, max_names=settings.beta_core_max_names,
                              min_momentum=settings.beta_core_min_momentum)
    ranked = sorted(momentum.items(), key=lambda kv: kv[1], reverse=True)

    print(f"BETA-CORE diagnostic — regime={flag.value} equity=${equity:.2f} "
          f"stable=${stable:.2f} position=${position_usd:.2f} (×{settings.beta_core_max_names}) "
          f"min_mom={settings.beta_core_min_momentum:g}%  [enabled={settings.beta_core_enabled}]")
    print(f"{'SYM':<12}{'mom%':>8}{'1h%':>7}{'24h%':>8}{'price':>12}  pick")
    for sym, mom in ranked[:15]:
        q = quotes.get(sym, {})
        mark = "★ BASKET" if sym in basket else ""
        print(f"{sym:<12}{mom:>8.1f}{(q.get('percent_change_1h') or 0):>7.1f}"
              f"{(q.get('percent_change_24h') or 0):>8.1f}{prices.get(sym, 0.0):>12.6g}  {mark}")

    # Simulate the decision on a NON-persisted copy of the book.
    book = PositionBook.load(POSITIONS_FILE)
    token_values = {s: balances.get(s, 0.0) * prices.get(s, 0.0) for s in balances}
    state = PortfolioState(equity_usd=equity, risk_value_usd=equity - stable,
                           stable_value_usd=stable, token_values_usd=token_values)
    orders, mode = bc.decide_beta(
        state, prices, momentum, book=book, regime_flag=flag, now=now,
        max_names=settings.beta_core_max_names, position_usd=position_usd, floor_usd=floor_usd,
        min_momentum=settings.beta_core_min_momentum, trail_pct=settings.beta_core_trail_pct,
        hard_stop_pct=settings.beta_core_hard_stop_pct,
        breakeven_trigger=settings.aegis_breakeven_trigger_pct,
        breakeven_buffer=settings.aegis_breakeven_buffer_pct,
        exit_rank_mult=settings.beta_core_exit_rank_mult)
    print(f"\nwould-do this scan (mode={mode}): {len(orders)} order(s)")
    for o in orders:
        print(f"  {o.token_in:>6} -> {o.token_out:<10} ${o.amount_in_usd:.2f}  [{o.reason}]")
    if not orders:
        print("  (nothing — basket already held / regime not RISK_ON / floor reached)")


if __name__ == "__main__":
    main()
