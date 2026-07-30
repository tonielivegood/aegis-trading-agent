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


@pytest.fixture(autouse=True)
def _default_rug_check_passes(request, monkeypatch):
    """Safety net: tests written before passes_rug_check existed don't know
    about it and would otherwise hit the real GoPlus API inside
    open_cluster_position. Default every test to a passing rug check; tests
    that need to assert on rug-blocking re-patch with their own return value
    (a test-level @patch always wins over this fixture for that test).

    test_rug_check.py is exempt — it tests passes_rug_check itself via
    requests.get at the network boundary, never through this default.
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_rug_check":
        return
    monkeypatch.setattr("src.agent.copy_trade.trade_engine.passes_rug_check",
                        lambda *a, **k: (True, ""))
