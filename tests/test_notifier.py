"""Telegram notifier tests — written test-first (TDD).

Security/robustness properties:
  - disabled (no token) -> no-op, never raises
  - network failure -> returns False, never raises (best-effort, must not break trading)
  - messages never contain the private key
"""
from __future__ import annotations

from src.agent.monitor import notifier


def test_disabled_when_no_token(mocker):
    mocker.patch.object(notifier.settings, "telegram_bot_token", "")
    mocker.patch.object(notifier.settings, "telegram_chat_id", "")
    assert notifier.is_enabled() is False
    # send is a safe no-op when disabled
    spy = mocker.patch("src.agent.monitor.notifier.requests.post")
    assert notifier.send("hello") is False
    spy.assert_not_called()


def test_send_success(mocker):
    mocker.patch.object(notifier.settings, "telegram_bot_token", "tok")
    mocker.patch.object(notifier.settings, "telegram_chat_id", "123")
    resp = mocker.Mock()
    resp.status_code = 200
    mocker.patch("src.agent.monitor.notifier.requests.post", return_value=resp)
    assert notifier.send("hello") is True


def test_send_failure_never_raises(mocker):
    mocker.patch.object(notifier.settings, "telegram_bot_token", "tok")
    mocker.patch.object(notifier.settings, "telegram_chat_id", "123")
    mocker.patch("src.agent.monitor.notifier.requests.post",
                 side_effect=OSError("network down"))
    # must swallow the error and report failure, not raise
    assert notifier.send("hello") is False


def test_messages_never_leak_private_key(mocker):
    # the formatting helpers must not embed any secret
    from src.agent.config import settings
    msg = notifier.format_heartbeat(equity=37.0, drawdown=0.05, cumulative_return=-0.1)
    assert settings.agent_private_key not in msg
    breaker = notifier.format_breaker(equity=80.0, drawdown=0.21)
    assert settings.agent_private_key not in breaker
