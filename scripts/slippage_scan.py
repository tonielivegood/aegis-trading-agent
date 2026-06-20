"""Measure on-chain slippage for EVERY tradable-alpha token at our real order size,
so we can see how many tokens each liquidity-gate level would unlock.

Read-only (getAmountsOut quotes only). Run on the VPS:
    cd /home/agent/bnbhack-track1-agent && .venv/bin/python /tmp/slippage_scan.py
"""
from src.agent.aegis.market_feed import MarketFeed
from src.agent.data import token_list

ORDER_USD = 12.0  # ~35% of $36 NAV = our actual position size

feed = MarketFeed(order_usd=ORDER_USD)
rows = []
for tok in token_list.tradable_alpha_tokens():
    snap = feed.snapshot(tok.symbol)
    rows.append((snap.slippage_est, tok.symbol, token_list.token_class(tok.symbol),
                 snap.has_route, snap.price_now))

rows.sort()
print(f"tradable_alpha tokens: {len(rows)} | order size: ${ORDER_USD}\n")
print(f"{'SYM':<10}{'class':<7}{'slip%':>8}  route/price")
for slip, sym, cls, route, price in rows:
    note = "" if route and price > 0 else "  (no route/price)"
    print(f"{sym:<10}{cls:<7}{slip * 100:>7.2f}%  {note}")

print("\n=== how many tokens unlock at each gate ===")
for gate in (0.005, 0.01, 0.02, 0.03, 0.05, 0.10):
    n = sum(1 for slip, _, _, route, price in rows if route and price > 0 and slip <= gate)
    print(f"  gate ≤{gate * 100:>4.1f}%  ->  {n} tokens")
