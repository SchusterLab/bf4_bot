"""
Tests for the Slack retry decorator's handling of HTTP-200 ``ok: false``
errors (bf4bot/slack_client.py _retryable_slack_call).

The 2026-08-25 dropped report was caused by files.completeUploadExternal
returning {'ok': False, 'error': 'file_update_failed'} -- an HTTP-200
Slack-side hiccup that the decorator treated as permanent.  These tests
pin the new behaviour: known-transient ok:false errors retry; everything
else stays permanent.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_slack_retry.py -v
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from slack_sdk.errors import SlackApiError  # noqa: E402
import bf4bot.slack_client as sc            # noqa: E402


class _Resp:
    """Minimal stand-in for slack_sdk's SlackResponse."""

    def __init__(self, error, status_code=200):
        self._error = error
        self.status_code = status_code
        self.headers = {}

    def get(self, key, default=None):
        if key == "error":
            return self._error
        return default


def _api_err(error, status=200):
    return SlackApiError(message=f"failed: {error}", response=_Resp(error, status))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the backoff sleeps so tests are instant."""
    monkeypatch.setattr(sc.time, "sleep", lambda *_a, **_k: None)


def test_transient_ok_false_is_retried_then_succeeds():
    calls = {"n": 0}

    @sc._retryable_slack_call
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _api_err("file_update_failed")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_transient_ok_false_gives_up_after_max_attempts():
    calls = {"n": 0}

    @sc._retryable_slack_call
    def always_fail():
        calls["n"] += 1
        raise _api_err("file_update_failed")

    with pytest.raises(SlackApiError):
        always_fail()
    assert calls["n"] == sc._MAX_ATTEMPTS


def test_permanent_ok_false_is_not_retried():
    calls = {"n": 0}

    @sc._retryable_slack_call
    def bad_channel():
        calls["n"] += 1
        raise _api_err("channel_not_found")

    with pytest.raises(SlackApiError):
        bad_channel()
    assert calls["n"] == 1  # raised immediately, no retry
