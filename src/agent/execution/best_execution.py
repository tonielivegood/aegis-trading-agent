"""Best-execution router: quote every routable aggregator live and rank by output.

"Linh hoạt chọn nơi thanh khoản cao nhất, tùy từng thời điểm cho token đó" (2/7,
user call) — instead of a fixed primary backend from config, ask 1inch, OpenOcean
and PancakeSwap for a live quote on THIS token/size and prefer whichever currently
gives the best output. Read-only: only quotes, never signs/broadcasts. The caller
still executes the swap via the winning executor's own `swap()`.
"""
from __future__ import annotations

from ..data.token_list import get_token
from ..monitor.logger import get_logger

log = get_logger(__name__)


def _out_human(backend: str, executor, token_in: str, token_out: str,
               amount_in_human: float) -> float | None:
    """Effective output amount for this backend's live quote, in human units of
    token_out. None on any failure (no key, no route, network hiccup) — a quote
    hiccup simply drops this venue from the ranking, it never raises."""
    try:
        if backend in ("1inch", "oneinch"):
            out_wei = executor._quote_out_wei(token_in, token_out, amount_in_human)
        elif backend == "openocean":
            out_wei = int(executor.quote(token_in, token_out, amount_in_human).get("outAmount", 0) or 0)
        elif backend == "pancake":
            out_wei = executor.quote(token_in, token_out, amount_in_human).expected_out_wei
        else:
            return None
        decimals = get_token(token_out).decimals
        return out_wei / (10 ** decimals) if out_wei > 0 else None
    except Exception as e:  # noqa: BLE001
        log.debug("best_execution_quote_failed", backend=backend, token_out=token_out,
                  error=type(e).__name__)
        return None


def rank_backends(executors: dict[str, object], token_in: str, token_out: str,
                  amount_in_human: float) -> list[str]:
    """Rank the given {name: executor} by live quoted output, best (highest) first.
    A backend that can't quote right now is dropped from the ranking entirely
    (not just deprioritized) — the caller decides what to do if the list is empty."""
    scored = [(name, out) for name, ex in executors.items()
              if (out := _out_human(name, ex, token_in, token_out, amount_in_human)) is not None]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [name for name, _ in scored]
