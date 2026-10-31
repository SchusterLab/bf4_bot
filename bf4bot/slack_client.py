"""
Slack API wrapper for BF4-bot.
Handles sending messages and uploading images.

All Slack-facing calls are wrapped with a retry layer that backs off on
transient failures (network blips, 429 rate-limits, 5xx errors).  This
used to be a single .send() that could crash the bot if Slack was
briefly unreachable; the retry decorator makes it resilient.
"""

import time
from datetime import datetime
from functools import wraps

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# Maximum retry attempts for a single Slack call.  With exp backoff at
# 1s, 2s, 4s, total worst-case latency is ~7s before we give up.
_MAX_ATTEMPTS = 3
_INITIAL_BACKOFF = 1.0   # seconds, doubles each failure

# Slack API-level errors (HTTP 200 with ``ok: false``) that are transient
# and worth retrying rather than giving up on.  The multi-step
# files_upload_v2 flow in particular can return ``file_update_failed`` from
# completeUploadExternal on a Slack-side hiccup (seen 2026-08-25, which
# silently dropped that day's scheduled report).  Everything NOT in this
# set (bad channel, missing scope, bad token, ...) stays permanent.
_RETRYABLE_SLACK_ERRORS = frozenset({
    "file_update_failed",
    "internal_error",
    "service_unavailable",
    "backend_error",
    "fatal_error",
    "timeout",
})


def _retryable_slack_call(fn):
    """
    Decorator: retry ``fn`` on transient Slack failures.

    A Slack call is worth retrying if:
      * It raised SlackApiError with a 429 (rate limit) or 5xx response.
      * It raised a generic network Exception (e.g. connection reset).

    Non-transient errors (invalid channel, missing token, etc) are
    re-raised immediately -- retrying would not help and would waste time.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        backoff = _INITIAL_BACKOFF
        last_error = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except SlackApiError as e:
                # Slack returns a "retry_after" header on rate limit (429).
                # When present we use it instead of our own backoff.
                status = getattr(e.response, "status_code", None)
                if status == 429:
                    retry_after = float(
                        e.response.headers.get("retry-after", backoff))
                    print(f"[{datetime.now()}] Slack rate-limited "
                          f"(attempt {attempt}/{_MAX_ATTEMPTS}); "
                          f"sleeping {retry_after}s.")
                    time.sleep(retry_after)
                    last_error = e
                    continue
                if status is not None and 500 <= status < 600:
                    print(f"[{datetime.now()}] Slack {status} error "
                          f"(attempt {attempt}/{_MAX_ATTEMPTS}); "
                          f"retry in {backoff}s.")
                    time.sleep(backoff)
                    backoff *= 2
                    last_error = e
                    continue
                # Some HTTP-200 ``ok: false`` errors are transient Slack-side
                # failures (e.g. file_update_failed during an upload) -- retry
                # those; everything else (bad channel, token, scope) is
                # permanent.
                err_code = e.response.get("error") \
                    if e.response is not None else None
                if err_code in _RETRYABLE_SLACK_ERRORS:
                    print(f"[{datetime.now()}] Slack '{err_code}' error "
                          f"(attempt {attempt}/{_MAX_ATTEMPTS}); "
                          f"retry in {backoff}s.")
                    time.sleep(backoff)
                    backoff *= 2
                    last_error = e
                    continue
                # Any other Slack error is permanent (bad channel, bad
                # token, etc) -- no point retrying.
                raise
            except Exception as e:
                # Connection reset, DNS blip, etc -- worth a retry.
                print(f"[{datetime.now()}] Slack call transient failure "
                      f"(attempt {attempt}/{_MAX_ATTEMPTS}): "
                      f"{type(e).__name__}: {e}; retry in {backoff}s.")
                time.sleep(backoff)
                backoff *= 2
                last_error = e
                continue
        # All attempts exhausted -- propagate the last error so callers
        # (which already wrap in try/except) can log + skip.
        raise last_error
    return wrapper


class BF4SlackClient:
    """Send messages and upload images to Slack."""

    def __init__(self, token=None, token_file="slack_bot_token.txt"):
        if token:
            self.client = WebClient(token=token)
        else:
            self.client = WebClient(token=open(token_file).read().strip())

        # Cache DM channel IDs so we don't re-open every time
        self._dm_channels = {}

    def _get_dm_channel(self, user_id):
        """Open/get a DM channel with a user."""
        if user_id not in self._dm_channels:
            response = self.client.conversations_open(users=[user_id])
            self._dm_channels[user_id] = response["channel"]["id"]
        return self._dm_channels[user_id]

    @_retryable_slack_call
    def send_message(self, channel_id, text):
        """Send a text message to a channel or DM."""
        response = self.client.chat_postMessage(channel=channel_id, text=text)
        return response

    def send_dm(self, user_id, text):
        """Send a DM to a user by user ID."""
        channel = self._get_dm_channel(user_id)
        return self.send_message(channel, text)

    @_retryable_slack_call
    def upload_image(self, channel_id, file_path, title=None, comment=None,
                     thread_ts=None):
        """Upload an image to a channel or DM channel.

        ``thread_ts`` (optional) posts the file as a reply in that thread,
        used by the @mention handler so a channel report stays threaded
        under the mention instead of landing in the main channel.
        """
        kwargs = dict(
            channel=channel_id,
            file=file_path,
            title=title or "BF4 Plot",
            initial_comment=comment or "",
        )
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = self.client.files_upload_v2(**kwargs)
        return response

    def upload_image_dm(self, user_id, file_path, title=None, comment=None):
        """Upload an image as a DM to a user."""
        channel = self._get_dm_channel(user_id)
        return self.upload_image(channel, file_path, title=title, comment=comment)

    def send_report(self, channel_id, plot_path, hours=24, scheduled=False,
                    comment=None, title=None, thread_ts=None):
        """Send a fridge status report (plot + summary) to a channel.

        By default the caption is "Last <period>".  Pass ``comment`` to
        override it (the bot builds an at-time-aware caption -- including
        any window warnings -- and passes it here).
        """
        from bf4bot import format_duration
        if comment is None:
            comment = f"BF4 Fridge Status Report -- Last {format_duration(hours)}"
            if scheduled:
                comment += ("\nTo edit report schedule or destinations, "
                            "access the BF4 fridge PC.")
        return self.upload_image(channel_id, plot_path,
                                 title=title or comment, comment=comment,
                                 thread_ts=thread_ts)

    def send_alarm(self, channel_id, message, plot_path=None, tag_channel=True):
        """
        Send an alarm message, optionally with a plot.

        ``tag_channel`` controls whether the message is prefixed with the
        Slack ``<!channel>`` tag (which notifies everyone in a public
        channel).  Defaults to True for real alarms.  Pass False for test
        alarms -- we do not want to spam @channel when the user is just
        verifying routing.  Also automatically downgraded to False for DM
        channels (``D...``) where @channel does nothing useful anyway.
        """
        # DM channels start with "D" and @channel does not work there, so
        # silently drop the tag in that case.
        if channel_id and channel_id.startswith("D"):
            tag_channel = False
        prefix = "<!channel> " if tag_channel else ""
        self.send_message(channel_id, f"{prefix}{message}")
        if plot_path:
            self.upload_image(channel_id, plot_path, title="Alarm Data")
