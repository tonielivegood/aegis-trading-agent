"""Bridge between historical price series and the live strategy code.

`momentum_decide` reconstructs the same percent-change "quotes" the live agent
gets from CMC, then runs the REAL signal_engine + momentum_strategy. Backtesting
this guarantees parity with live behavior.
"""
from __future__ import annotations

import statistics

from ..config import settings
from ..data.token_list import STABLECOINS
from ..risk.position_sizer import PositionSizer
from ..signal import signal_engine
from ..strategy import momentum_strategy, rebalance_strategy
from ..strategy.base_strategy import PortfolioState, TradeOrder

MIN_ORDER_USD = 2.0
MAX_NEW = 3


def _pct(series: list[float], steps: int) -> float:
    if len(series) > steps and series[-1 - steps] > 0:
        return (series[-1] - series[-1 - steps]) / series[-1 - steps] * 100.0
    return 0.0


def quotes_from_history(history: dict[str, list[float]]) -> dict[str, dict]:
    return {
        sym: {
            "percent_change_1h": _pct(ser, 1),
            "percent_change_24h": _pct(ser, 24),
            "percent_change_7d": _pct(ser, 168),
            "price": ser[-1],
        }
        for sym, ser in history.items()
    }


def momentum_decide(prices: dict[str, float], state: PortfolioState,
                    history: dict[str, list[float]]) -> list[TradeOrder]:
    quotes = quotes_from_history(history)
    signals = signal_engine.generate(list(history.keys()), quotes)
    return momentum_strategy.decide(signals, state)


def _volatility(series: list[float], n: int) -> float:
    w = series[-n:]
    rets = [(w[i] - w[i - 1]) / w[i - 1] for i in range(1, len(w)) if w[i - 1] > 0]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def mean_reversion_decide(prices, state, history, lookback: int = 48,
                          z_buy: float = -1.0, z_sell: float = 1.0) -> list[TradeOrder]:
    """Buy oversold (price well below its mean), sell overbought. Contrarian."""
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)

    sizer = PositionSizer(state.equity_usd, settings.max_position_pct, settings.stablecoin_floor_pct)
    running = state.risk_value_usd
    zscores: dict[str, float] = {}
    for sym, ser in history.items():
        if len(ser) < lookback:
            continue
        window = ser[-lookback:]
        mu, sd = statistics.mean(window), statistics.pstdev(window)
        if sd > 0:
            zscores[sym] = (ser[-1] - mu) / sd

    orders: list[TradeOrder] = []
    for sym, z in zscores.items():
        if z >= z_sell:
            held = state.token_values_usd.get(sym, 0.0)
            if held >= MIN_ORDER_USD:
                orders.append(TradeOrder(sym, "USDT", held, f"mr sell z={z:.1f}"))
    for z, sym in sorted((z, s) for s, z in zscores.items() if z <= z_buy)[:MAX_NEW]:
        held = state.token_values_usd.get(sym, 0.0)
        size = sizer.size_for(held, running)
        if size >= MIN_ORDER_USD:
            orders.append(TradeOrder("USDT", sym, size, f"mr buy z={z:.1f}"))
            running += size
    return orders


def basket_rebalance_decide(prices, state, history, k: int = 8, band: float = 0.30,
                            lookback: int = 48) -> list[TradeOrder]:
    """Vol-targeted, trend-filtered, periodically-rebalanced basket.

    Hold an inverse-volatility-weighted basket of up-trending tokens; rebalance
    when a holding drifts outside the band. Diversification + rebalancing keeps
    drawdown low and naturally generates the required trade count.
    """
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)

    candidates = []
    for sym, ser in history.items():
        if len(ser) <= 168:
            continue
        mom_7d = (ser[-1] - ser[-169]) / ser[-169]
        if mom_7d > 0:  # trend filter
            candidates.append((sym, _volatility(ser, lookback)))
    if not candidates:
        return rebalance_strategy.derisk_orders(state)  # nothing trending -> go safe

    candidates.sort(key=lambda x: x[1])             # lowest volatility first
    chosen = candidates[:k]
    inv_weights = [(s, (1.0 / v if v > 0 else 0.0)) for s, v in chosen]
    total_w = sum(w for _, w in inv_weights) or 1.0
    deploy = state.equity_usd * (1.0 - settings.stablecoin_floor_pct)
    max_pos = state.equity_usd * settings.max_position_pct
    targets = {s: min(deploy * (w / total_w), max_pos) for s, w in inv_weights}

    orders: list[TradeOrder] = []
    chosen_syms = {s for s, _ in chosen}
    for sym, val in state.token_values_usd.items():
        if sym not in chosen_syms and val >= MIN_ORDER_USD:
            orders.append(TradeOrder(sym, "USDT", val, "basket exit"))
    for sym, target in targets.items():
        cur = state.token_values_usd.get(sym, 0.0)
        if cur < target * (1 - band) and (target - cur) >= MIN_ORDER_USD:
            orders.append(TradeOrder("USDT", sym, target - cur, "basket buy"))
        elif cur > target * (1 + band) and (cur - target) >= MIN_ORDER_USD:
            orders.append(TradeOrder(sym, "USDT", cur - target, "basket trim"))
    return orders


def buy_and_hold_decide(prices, state, history) -> list[TradeOrder]:
    """Market reference: deploy equally across all tokens once, then hold."""
    if state.risk_value_usd > 1.0:
        return []
    syms = [s for s in history if history[s]]
    per = state.equity_usd * 0.95 / len(syms) if syms else 0.0
    return [TradeOrder("USDT", s, per, "b&h") for s in syms if per >= MIN_ORDER_USD]


def hold_with_breaker_decide(prices, state, history) -> list[TradeOrder]:
    """Diversified equal-weight hold, but exit to cash if the drawdown breaker
    trips — keeps the upside of holding while cutting the tail-risk drawdown.
    """
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    if state.risk_value_usd < state.equity_usd * 0.5:   # not yet deployed
        return buy_and_hold_decide(prices, state, history)
    return []


def fractional_hold_decide(prices, state, history, deploy_frac: float = 0.5) -> list[TradeOrder]:
    """Preservation-first instrument: keep most capital in stablecoin, deploy only
    `deploy_frac` of equity into a diversified equal-weight basket, with the hard
    breaker exiting on a -20% drawdown. Caps downside to roughly deploy_frac of a
    full-hold loss while still catching up-weeks proportionally.
    """
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    target_risk = state.equity_usd * deploy_frac
    if state.risk_value_usd >= target_risk * 0.8:
        return []  # already at target exposure
    invest = target_risk - state.risk_value_usd
    syms = [s for s in history if history[s]]
    per = invest / len(syms) if syms else 0.0
    return [TradeOrder("USDT", s, per, "fractional hold") for s in syms if per >= MIN_ORDER_USD]


def _momentum(ser: list[float], lookback: int) -> float:
    if len(ser) <= lookback or ser[-lookback - 1] <= 0:
        return 0.0
    return (ser[-1] - ser[-lookback - 1]) / ser[-lookback - 1]


def trend_filtered_hold_decide(prices, state, history, deploy_frac: float = 0.5,
                               lookback: int = 168) -> list[TradeOrder]:
    """Fractional hold, but only deploy into tokens with positive momentum.
    In a down market nothing qualifies -> stay/go to cash (extra preservation)."""
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    up = [s for s, ser in history.items() if _momentum(ser, lookback) > 0]
    if not up:
        return rebalance_strategy.derisk_orders(state)
    target_risk = state.equity_usd * deploy_frac
    if state.risk_value_usd >= target_risk * 0.8:
        return []
    invest = target_risk - state.risk_value_usd
    per = min(invest / len(up), state.equity_usd * settings.max_position_pct)
    if per < MIN_ORDER_USD:
        return []
    return [TradeOrder("USDT", s, per, "trend hold") for s in up]


def momentum_weighted_hold_decide(prices, state, history, deploy_frac: float = 0.5,
                                  lookback: int = 168) -> list[TradeOrder]:
    """Fractional hold weighted by momentum strength (more to stronger trends)."""
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    moms = {s: m for s, ser in history.items() if (m := _momentum(ser, lookback)) > 0}
    if not moms:
        return rebalance_strategy.derisk_orders(state)
    if state.risk_value_usd >= state.equity_usd * deploy_frac * 0.8:
        return []
    total = sum(moms.values())
    deploy = state.equity_usd * deploy_frac
    max_pos = state.equity_usd * settings.max_position_pct
    orders = []
    for sym, m in moms.items():
        target = min(deploy * (m / total), max_pos)
        gap = target - state.token_values_usd.get(sym, 0.0)
        if gap >= MIN_ORDER_USD:
            orders.append(TradeOrder("USDT", sym, gap, "mom-wt hold"))
    return orders


def equal_weight_rebalance_decide(prices, state, history, deploy_frac: float = 0.6,
                                  band: float = 0.25) -> list[TradeOrder]:
    """Hold an equal-weight basket and rebalance back to target when a holding
    drifts outside the band. Captures the rebalancing premium (mechanically sells
    relative winners, buys relative losers) — a return source independent of market
    direction."""
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    basket = [s for s in history if s not in STABLECOINS]
    if not basket:
        return []
    target = min(state.equity_usd * deploy_frac / len(basket),
                 state.equity_usd * settings.max_position_pct)
    orders: list[TradeOrder] = []
    for sym in basket:
        cur = state.token_values_usd.get(sym, 0.0)
        if cur < target * (1 - band) and (target - cur) >= MIN_ORDER_USD:
            orders.append(TradeOrder("USDT", sym, target - cur, "rebal buy"))
        elif cur > target * (1 + band) and (cur - target) >= MIN_ORDER_USD:
            orders.append(TradeOrder(sym, "USDT", cur - target, "rebal trim"))
    return orders


def short_mr_decide(prices, state, history, deploy_frac: float = 0.6, lookback: int = 12,
                    z_buy: float = -1.0, z_sell: float = 1.0) -> list[TradeOrder]:
    """Short-horizon mean reversion: buy oversold majors (z below -1 over `lookback`
    hours), sell when they revert above +1. Deployable budget = deploy_frac."""
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)
    sizer = PositionSizer(state.equity_usd, settings.max_position_pct, 1.0 - deploy_frac)
    running = state.risk_value_usd
    zscores = {}
    for sym, ser in history.items():
        if sym in STABLECOINS or len(ser) < lookback:
            continue
        window = ser[-lookback:]
        mu, sd = statistics.mean(window), statistics.pstdev(window)
        if sd > 0:
            zscores[sym] = (ser[-1] - mu) / sd
    orders: list[TradeOrder] = []
    for sym, z in zscores.items():
        if z >= z_sell:
            held = state.token_values_usd.get(sym, 0.0)
            if held >= MIN_ORDER_USD:
                orders.append(TradeOrder(sym, "USDT", held, f"mr sell z={z:.1f}"))
    for z, sym in sorted((z, s) for s, z in zscores.items() if z <= z_buy):
        held = state.token_values_usd.get(sym, 0.0)
        size = sizer.size_for(held, running)
        if size >= MIN_ORDER_USD:
            orders.append(TradeOrder("USDT", sym, size, f"mr buy z={z:.1f}"))
            running += size
    return orders


def btc_trend_hold_decide(prices, state, history, deploy_frac: float = 0.5,
                          btc_ma: int = 168, btc_symbol: str = "BTCB") -> list[TradeOrder]:
    """Fractional hold gated by BTC trend: deploy the basket only when BTC is above
    its moving average, else hold cash. BTC leads the market, so its trend is a
    cleaner regime signal than breadth (fewer constituents = less noise)."""
    btc = history.get(btc_symbol)
    regime_on = True
    if btc and len(btc) > btc_ma:
        regime_on = btc[-1] > statistics.mean(btc[-btc_ma:])
    if not regime_on:
        return rebalance_strategy.derisk_orders(state)
    return fractional_hold_decide(prices, state, history, deploy_frac=deploy_frac)


def market_breadth(history: dict[str, list[float]], ma: int = 72) -> float:
    """Fraction of tokens trading above their moving average — a regime gauge."""
    above = total = 0
    for ser in history.values():
        if len(ser) <= ma:
            continue
        total += 1
        if ser[-1] > statistics.mean(ser[-ma:]):
            above += 1
    return above / total if total else 0.0


def preservation_first_decide(prices, state, history, *, breadth_ma: int = 168,
                              on: float = 0.55, off: float = 0.45) -> list[TradeOrder]:
    """Default to cash; deploy the basket ONLY on a confirmed broad uptrend.

    Uses a slow (7-day) breadth gauge to avoid whipsaw, with portfolio-state
    hysteresis (different thresholds to turn risk on vs off). In a flat/down
    market this stays in stablecoin and preserves capital; in a sustained
    uptrend it participates via the vol-targeted basket. The drawdown breaker
    inside basket_rebalance still forces an exit if things turn sharply.
    """
    breadth = market_breadth(history, ma=breadth_ma)
    deployed = state.risk_value_usd > state.equity_usd * 0.10
    risk_on = breadth > (off if deployed else on)
    if not risk_on:
        return rebalance_strategy.derisk_orders(state)
    return basket_rebalance_decide(prices, state, history)


def regime_adaptive_decide(prices, state, history,
                           breadth_on: float = 0.6, breadth_off: float = 0.4) -> list[TradeOrder]:
    """The contest edge: deploy the basket only when the market is healthy,
    hold stablecoin otherwise. Uses the current portfolio (deployed vs cash) as
    hysteresis memory so it does NOT whipsaw around a single threshold.
    """
    breadth = market_breadth(history)
    currently_deployed = state.risk_value_usd > state.equity_usd * 0.10

    if currently_deployed:
        if breadth < breadth_off:
            return rebalance_strategy.derisk_orders(state)   # turn risk OFF
        return basket_rebalance_decide(prices, state, history)  # stay in, maintain basket
    # currently in cash
    if breadth > breadth_on:
        return basket_rebalance_decide(prices, state, history)  # turn risk ON
    return []  # stay in cash, preserve capital
