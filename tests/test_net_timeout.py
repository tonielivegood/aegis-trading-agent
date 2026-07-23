"""Hard wall-clock timeout wrapper — protects against calls that hang past
their own declared timeout (DNS hangs, black-holed routes, trickle responses)."""
import time

import pytest

from src.agent.copy_trade.net_timeout import call_with_hard_timeout


def test_call_with_hard_timeout_returns_fast_result():
    assert call_with_hard_timeout(lambda: 42, hard_timeout=1.0) == 42


def test_call_with_hard_timeout_passes_args_and_kwargs():
    assert call_with_hard_timeout(lambda a, b=0: a + b, 3, b=4, hard_timeout=1.0) == 7


def test_call_with_hard_timeout_raises_on_a_hang():
    def hangs_forever():
        time.sleep(10)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        call_with_hard_timeout(hangs_forever, hard_timeout=0.2)
    assert time.monotonic() - start < 5   # cut off near hard_timeout, not the full 10s
