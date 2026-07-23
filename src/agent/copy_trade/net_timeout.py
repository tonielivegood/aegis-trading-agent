"""Hard wall-clock timeout wrapper for network calls.

`requests`' own `timeout=` only bounds silence between socket reads/connects —
a DNS hang, a black-holed IPv6 route, or a server trickling bytes can all keep
a call blocked far longer than its declared timeout (live incident 2026-07-23:
the main copy-trade loop froze 71+ minutes despite every call here using
timeout=15). Running the call in a thread and bounding it with
`Future.result(timeout=...)` gives a real ceiling no matter what's stuck inside.
"""
from __future__ import annotations

import concurrent.futures

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="net_timeout")


def call_with_hard_timeout(fn, *args, hard_timeout: float, **kwargs):
    """Run fn(*args, **kwargs) with a real wall-clock deadline.

    Raises TimeoutError if exceeded. ponytail: the spawned thread is abandoned,
    not killed (Python has no safe way to kill a thread) — a small leaked
    thread/socket is an acceptable trade for never freezing the caller again.
    """
    future = _executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=hard_timeout)
    except concurrent.futures.TimeoutError as e:
        raise TimeoutError(
            f"{getattr(fn, '__name__', fn)} exceeded hard timeout of {hard_timeout}s"
        ) from e
