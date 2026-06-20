"""Slippage of every tradable-alpha token at several order sizes — to see how many
tokens each liquidity gate unlocks, and whether a smaller order size helps.

Read-only. Run on the VPS:  .venv/bin/python /tmp/slippage_scan.py
"""
from src.agent.aegis.market_feed import MarketFeed
from src.agent.data import token_list

SIZES = [12.0, 6.0, 3.0]
toks = token_list.tradable_alpha_tokens()

# slippage per token per size
data = {}
for size in SIZES:
    feed = MarketFeed(order_usd=size)
    for tok in toks:
        s = feed.snapshot(tok.symbol)
        data.setdefault(tok.symbol, {})[size] = (s.slippage_est, s.has_route and s.price_now > 0,
                                                  token_list.token_class(tok.symbol))

# print sorted by $12 slippage
print(f"{'SYM':<10}{'class':<7}" + "".join(f"{f'${s:.0f}slip':>9}" for s in SIZES))
for sym in sorted(data, key=lambda k: data[k][12.0][0]):
    cls = data[sym][12.0][2]
    cells = "".join(f"{data[sym][s][0] * 100:>8.2f}%" for s in SIZES)
    print(f"{sym:<10}{cls:<7}{cells}")

print("\n=== tokens passing gate, by size ===")
for size in SIZES:
    line = [f"order ${size:.0f}:"]
    for gate in (0.02, 0.04, 0.06):
        n = sum(1 for sym in data if data[sym][size][1] and data[sym][size][0] <= gate)
        line.append(f"≤{gate*100:.0f}%={n}")
    print("  " + "  ".join(line))
