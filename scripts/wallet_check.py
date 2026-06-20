"""Read-only wallet snapshot: balances, USD value, gas headroom."""
from src.agent.agent_loop import _apply_price_fallback, _event_prices
from src.agent.config import settings
from src.agent.data import token_list
from src.agent.risk.portfolio import Portfolio, read_onchain_balances

bal = read_onchain_balances(settings.agent_wallet_address)
px = _apply_price_fallback(_event_prices(token_list.alpha_symbols(), bal), bal)
print("=== WALLET NOW ===")
for a, amt in sorted(bal.items()):
    print(f"  {a:<6} {amt:<16.6f} USD {amt * px.get(a, 0.0):>9.4f}")
eq = Portfolio().equity(bal, px)
stable = Portfolio().stable_value(bal, px)
print(f"  EQUITY = USD {eq:.2f}  | stable = USD {stable:.2f}")
print(f"  gas BNB = {bal.get('BNB', 0):.5f}  ~swaps = {int((bal.get('BNB', 0) - settings.min_gas_bnb) / 0.000166)}")
print(f"  LUNC held = {bal.get('LUNC', 0)}  | WBNB held = {bal.get('WBNB', 0)}")
