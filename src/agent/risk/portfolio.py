"""Portfolio valuation and PnL tracking.

Valuation logic is pure (balances + prices in, USD out) so it is fully unit
tested without a network. On-chain balance reading lives in `read_balances`,
the only part that crosses the RPC boundary.

Cost basis is tracked per token as a running (total_amount, total_cost) so
`unrealized_pnl` reflects average-cost accounting.
"""
from __future__ import annotations

from ..data import token_list
from ..data.token_list import STABLECOINS
from .guards import require_finite_nonneg

# Minimal ERC-20 ABI for balanceOf.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

# Multicall3 — same canonical address on BSC and most EVM chains.
_MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_MULTICALL3_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "target", "type": "address"},
                {"name": "allowFailure", "type": "bool"},
                {"name": "callData", "type": "bytes"},
            ],
            "name": "calls", "type": "tuple[]",
        }],
        "name": "aggregate3",
        "outputs": [{
            "components": [
                {"name": "success", "type": "bool"},
                {"name": "returnData", "type": "bytes"},
            ],
            "name": "returnData", "type": "tuple[]",
        }],
        "stateMutability": "payable",
        "type": "function",
    }
]
_BALANCEOF_SELECTOR = "0x70a08231"
_GETETHBALANCE_SELECTOR = "0x4d2301cc"  # Multicall3.getEthBalance(address)


def _selected_tokens(symbols: list[str] | None):
    # Valuation reads the FULL holdable universe (core ∪ alpha), not the trading
    # subset — otherwise a holding outside the trading set reads as 0 and trips a
    # phantom drawdown. Callers that want a specific set still filter by symbol.
    if symbols is None:
        return token_list.valuation_tokens()
    # Resolve each wanted symbol via get_token() (core ∪ alpha ∪ runtime-discovered),
    # NOT by filtering the static valuation_tokens() list — a discovered hot-token
    # symbol is never in that static list, so filtering it always yielded nothing
    # (real-money bug, 2/7: a held discovered token's balance silently read as 0).
    tokens = []
    for s in symbols:
        try:
            tokens.append(token_list.get_token(s))
        except KeyError:
            continue
    return tokens


def _calldata(selector: str, address: str) -> bytes:
    return bytes.fromhex(selector[2:] + address.lower().replace("0x", "").rjust(64, "0"))


def _read_balances_multicall(wallet: str, symbols: list[str] | None) -> dict[str, float]:
    """One RPC round-trip for native + all token balances via Multicall3."""
    from web3 import Web3

    from ..data.rpc import get_web3

    w3 = get_web3()
    owner = Web3.to_checksum_address(wallet)
    mc = w3.eth.contract(address=Web3.to_checksum_address(_MULTICALL3), abi=_MULTICALL3_ABI)
    tokens = _selected_tokens(symbols)

    calls = [(Web3.to_checksum_address(_MULTICALL3), True, _calldata(_GETETHBALANCE_SELECTOR, owner))]
    calls += [(t.address, True, _calldata(_BALANCEOF_SELECTOR, owner)) for t in tokens]

    results = mc.functions.aggregate3(calls).call()
    out: dict[str, float] = {}

    ok, data = results[0]
    if ok and data:
        native = int.from_bytes(data, "big")
        if native > 0:
            out["BNB"] = native / 1e18
    for tok, (ok, data) in zip(tokens, results[1:]):
        if ok and data:
            raw = int.from_bytes(data, "big")
            if raw > 0:
                out[tok.symbol] = raw / (10 ** tok.decimals)
    return out


def _read_balances_sequential(wallet: str, symbols: list[str] | None) -> dict[str, float]:
    """Fallback: one RPC call per token (used if multicall is unavailable)."""
    from web3 import Web3

    from ..data.rpc import get_web3

    w3 = get_web3()
    owner = Web3.to_checksum_address(wallet)
    out: dict[str, float] = {}

    native = w3.eth.get_balance(owner)
    if native > 0:
        out["BNB"] = native / 1e18

    for tok in _selected_tokens(symbols):
        try:
            c = w3.eth.contract(address=tok.address, abi=_ERC20_ABI)
            raw = c.functions.balanceOf(owner).call()
            if raw > 0:
                out[tok.symbol] = raw / (10 ** tok.decimals)
        except Exception:  # noqa: BLE001 — skip a token that fails, don't abort the read
            continue
    return out


def read_onchain_balances(wallet: str, symbols: list[str] | None = None) -> dict[str, float]:
    """Read live token balances for `wallet` from BSC. Crosses the RPC boundary.

    Returns {symbol: human_amount}; zero balances omitted; native BNB under "BNB".
    Uses Multicall3 (one round-trip) and falls back to per-token reads on failure.
    """
    try:
        return _read_balances_multicall(wallet, symbols)
    except Exception:  # noqa: BLE001 — any multicall issue -> robust sequential path
        return _read_balances_sequential(wallet, symbols)


class Portfolio:
    def __init__(self) -> None:
        # symbol -> [total_amount, total_cost_usd]
        self._basis: dict[str, list[float]] = {}

    # --- valuation (pure) ---
    def equity(self, balances: dict[str, float], prices: dict[str, float]) -> float:
        total = 0.0
        for sym, bal in balances.items():
            bal = require_finite_nonneg(bal, f"balance[{sym}]")
            price = prices.get(sym)
            if price and price > 0:
                total += bal * price
        return total

    def stable_value(self, balances: dict[str, float], prices: dict[str, float]) -> float:
        return sum(
            bal * prices.get(sym, 0.0)
            for sym, bal in balances.items()
            if sym in STABLECOINS and prices.get(sym, 0.0) > 0
        )

    def risk_value(self, balances: dict[str, float], prices: dict[str, float]) -> float:
        return self.equity(balances, prices) - self.stable_value(balances, prices)

    # --- cost basis / PnL ---
    def record_fill(self, symbol: str, amount: float, price: float) -> None:
        amount = require_finite_nonneg(amount, "amount")
        price = require_finite_nonneg(price, "price")
        entry = self._basis.setdefault(symbol, [0.0, 0.0])
        entry[0] += amount
        entry[1] += amount * price

    def amount_held(self, symbol: str) -> float:
        return self._basis.get(symbol, [0.0, 0.0])[0]

    def avg_cost(self, symbol: str) -> float:
        amount, cost = self._basis.get(symbol, [0.0, 0.0])
        return cost / amount if amount > 0 else 0.0

    def record_sell(self, symbol: str, amount: float, price: float) -> float:
        """Reduce a holding and return realized PnL. Average-cost basis is
        preserved; the cost pool shrinks proportionally to the amount sold."""
        amount = require_finite_nonneg(amount, "amount")
        price = require_finite_nonneg(price, "price")
        held = self.amount_held(symbol)
        if amount > held:
            raise ValueError(f"cannot sell {amount} {symbol}: only {held} held")
        avg = self.avg_cost(symbol)
        entry = self._basis[symbol]
        entry[0] -= amount
        entry[1] -= amount * avg  # remove sold units at average cost
        return (price - avg) * amount

    def unrealized_pnl(self, symbol: str, current_price: float) -> float:
        amount = self.amount_held(symbol)
        if amount <= 0:
            return 0.0
        return (current_price - self.avg_cost(symbol)) * amount
