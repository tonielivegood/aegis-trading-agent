"""Parse a Moralis `wallets/{address}/history` transaction into a clean single-leg
swap, or None if it isn't one.

The wallet only ever directly sends/receives the FIRST and LAST leg of a routed swap —
intermediate router/pool hops never have the wallet as from_address or to_address, so
filtering on that automatically drops them. If filtering doesn't leave exactly one
sent leg and one received leg, the tx is a genuine multi-token batch trade: return
None rather than guess which leg matters (the bug this replaces guessed wrong).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_STABLE_OR_NATIVE = {"USDT", "USDC", "BUSD", "BNB", "WBNB"}


@dataclass(frozen=True)
class ParsedSwap:
    hash: str
    wallet: str
    direction: Literal["buy", "sell"]
    token_symbol: str
    token_address: str
    token_decimals: int
    token_amount: float
    counter_symbol: str
    usd_value: float | None
    timestamp: str


def parse_swap(tx: dict, wallet: str) -> ParsedSwap | None:
    if tx.get("category") != "token swap":
        return None

    w = wallet.lower()
    transfers = tx.get("erc20_transfers", [])
    sent = [t for t in transfers if (t.get("from_address") or "").lower() == w]
    received = [t for t in transfers if (t.get("to_address") or "").lower() == w]

    if len(sent) != 1 or len(received) != 1:
        return None

    sent_leg, recv_leg = sent[0], received[0]
    sent_sym = sent_leg.get("token_symbol", "")
    recv_sym = recv_leg.get("token_symbol", "")

    # Buy = wallet gave up a stable/native and received the tracked token.
    # Sell = wallet gave up the tracked token and received a stable/native.
    if sent_sym in _STABLE_OR_NATIVE and recv_sym not in _STABLE_OR_NATIVE:
        direction: Literal["buy", "sell"] = "buy"
        token_leg, counter_sym = recv_leg, sent_sym
    elif sent_sym not in _STABLE_OR_NATIVE and recv_sym in _STABLE_OR_NATIVE:
        direction = "sell"
        token_leg, counter_sym = sent_leg, recv_sym
    else:
        return None  # stable<->stable or gem<->gem — not an actionable copy signal

    try:
        decimals = int(token_leg.get("token_decimals", 18))
        amount = float(token_leg.get("value_formatted", 0))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    return ParsedSwap(
        hash=tx.get("hash", ""),
        wallet=wallet,
        direction=direction,
        token_symbol=token_leg.get("token_symbol", ""),
        token_address=token_leg.get("address", ""),
        token_decimals=decimals,
        token_amount=amount,
        counter_symbol=counter_sym,
        usd_value=None,
        timestamp=tx.get("block_timestamp", ""),
    )
