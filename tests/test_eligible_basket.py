"""Layer A — baseline eligible-basket fallback tests."""
from __future__ import annotations

from src.agent.strategy import eligible_basket_strategy as basket
from src.agent.strategy.base_strategy import PortfolioState


def _state(equity, holdings=None, **kw):
    holdings = holdings or {}
    risk = sum(v for s, v in holdings.items() if s != "USDT")
    stable = holdings.get("USDT", equity - risk)
    return PortfolioState(equity_usd=equity, risk_value_usd=risk,
                          stable_value_usd=stable, token_values_usd=holdings, **kw)


def test_breaker_derisks():
    st = _state(100.0, {"FOO": 10.0}, drawdown_tripped=True)
    orders = basket.decide(st, ["FOO", "BAR"])
    assert orders and all(o.token_out == "USDT" for o in orders)


def test_equal_weight_with_per_token_cap():
    # 100 equity, 20% floor -> 80 deployable; 4 names; cap 5% = $5 each.
    st = _state(100.0)
    orders = basket.decide(st, ["A", "B", "C", "D"], per_token_pct=0.05, stable_floor=0.20)
    sizes = {o.token_out: o.amount_in_usd for o in orders}
    assert set(sizes) == {"A", "B", "C", "D"}
    assert all(v == 5.0 for v in sizes.values())   # capped at 5% each


def test_small_capital_trims_basket_to_avoid_dust():
    # $12 equity, 0% floor -> 12 deployable; min order $2 -> at most 6 names.
    st = _state(12.0)
    orders = basket.decide(st, [f"T{i}" for i in range(20)], per_token_pct=1.0, stable_floor=0.0)
    assert 1 <= len(orders) <= 6
    assert all(o.amount_in_usd >= 2.0 for o in orders)


def test_no_orders_when_capital_too_small():
    st = _state(1.0)
    assert basket.decide(st, ["A", "B"]) == []


def test_skips_stablecoins_in_basket():
    st = _state(100.0)
    orders = basket.decide(st, ["USDT", "USDC", "FOO"], per_token_pct=0.05)
    assert {o.token_out for o in orders} == {"FOO"}
