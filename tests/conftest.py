"""Shared test fixtures + global safety nets."""
from __future__ import annotations

import pytest

from src.agent.monitor import notifier


@pytest.fixture(autouse=True)
def _no_real_telegram(request, monkeypatch):
    """Safety net: NO test may ever hit the real Telegram API.

    The notifier reads live bot-token/chat-id from .env, so a test that exercises an
    alert path without mocking `send` would post to the production alert channel
    (this actually happened: a failover test paged a real "EXIT FAILED" alert). Neutralise
    `send` for every test; tests that need to assert on it re-patch with their own mock.

    test_notifier.py is exempt — it tests `send` itself and mocks `requests.post` at the
    network boundary, so it never reaches the real API either.
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_notifier":
        return
    monkeypatch.setattr(notifier, "send", lambda *a, **k: False)
