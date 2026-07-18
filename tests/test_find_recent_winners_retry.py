"""Tests for _get()'s 429 retry-with-backoff behavior. GeckoTerminal's free tier
rate-limits hard even with the flat _SLEEP_S delay between calls; a bare 429 used
to be treated as a fatal per-call failure (silently dropping that pool/candle-set).
These pin the retry/backoff scheme without ever sleeping for real."""
from unittest.mock import MagicMock, patch

import requests

from scripts.find_recent_winners import _get, _MAX_429_ATTEMPTS


def _resp(status_code, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.json.return_value = json_body
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_429_then_200_eventually_succeeds(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(429), _resp(200, {"ok": True})]
    result = _get("http://x")
    assert result == {"ok": True}
    assert mock_get.call_count > 1


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_429_honors_retry_after_header(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(429, headers={"Retry-After": "5"}),
                             _resp(200, {"ok": True})]
    result = _get("http://x")
    assert result == {"ok": True}
    # slept for (something derived from) the header value, not the default backoff
    assert mock_sleep.call_args[0][0] == 5.0


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_429_retry_after_zero_floors_to_at_least_one_second(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(429, headers={"Retry-After": "0"}),
                             _resp(200, {"ok": True})]
    _get("http://x")
    assert mock_sleep.call_args[0][0] >= 1.0


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_429_every_attempt_gives_up_and_returns_none(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(429), _resp(429), _resp(429), _resp(429), _resp(429)]
    result = _get("http://x")               # must terminate, not loop forever
    assert result is None
    assert mock_get.call_count == _MAX_429_ATTEMPTS


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_non_429_http_error_fails_immediately_no_retry(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(500)]
    result = _get("http://x")
    assert result is None
    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()


@patch("scripts.find_recent_winners.time.sleep")
@patch("scripts.find_recent_winners.requests.get")
def test_timeout_exception_fails_immediately_no_retry(mock_get, mock_sleep):
    mock_get.side_effect = requests.exceptions.Timeout("boom")
    result = _get("http://x")
    assert result is None
    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()
