"""Low-level transaction helpers — pure math + local signing.

The pure helpers (wei conversion, slippage, deadline) are fully unit tested.
Signing happens locally via eth_account; the private key never leaves the process
and is never logged.
"""
from __future__ import annotations

import math
from decimal import Decimal


def to_wei_amount(amount: float, decimals: int) -> int:
    """Convert a human token amount to integer base units, honoring decimals.

    DOGE uses 8 decimals; most BSC tokens use 18. Uses Decimal to avoid float
    rounding error on the conversion.
    """
    if not isinstance(amount, (int, float)) or math.isnan(amount) or math.isinf(amount) or amount < 0:
        raise ValueError(f"invalid amount: {amount!r}")
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def apply_slippage(amount_out_wei: int, slippage_bps: int) -> int:
    """Reduce an expected output by slippage tolerance to get min acceptable out.

    e.g. apply_slippage(1000, 50) -> 995 (0.5% tolerance).
    """
    return amount_out_wei * (10_000 - slippage_bps) // 10_000


def swap_deadline(now_ts: int, seconds: int = 120) -> int:
    """Unix deadline `seconds` from now. Kept short so a stale tx can't execute
    later at an unfavorable price."""
    return int(now_ts) + int(seconds)
