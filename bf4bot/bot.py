"""
BF4 Fridge Monitoring Bot -- Main loop.

Runs continuously:
- Polls fridge API for alarm checks (every poll_interval seconds)
- Sends scheduled daily/weekly reports
- All fridge access is READ-ONLY
"""

import os
import threading
import time
import traceback
from datetime import datetime, timedelta

from bf4bot import (
    format_duration, parse_report_request, history_is_empty,
)
from bf4bot.reader import FridgeReader
from bf4bot.plotting import (
    plot_fridge_status, plot_compressor, plot_turbo_pumps, plot_alarm_data,
    SUBFOLDER_DAILY, SUBFOLDER_ON_DEMAND, SUBFOLDER_ALARMS,
)
from bf4bot.slack_client import BF4SlackClient
from bf4bot.alarms import AlarmManager
from bf4bot.config import (
    load_config, load_keys, load_reports, resolve_channel, get_operator_user_id,
    ReportManager, load_hardware,
)

# Commands the bot responds to in DMs
HELP_TEXT = """*BF4-bot commands:*
  `status` -- live fridge readings
  `report` -- 24h fridge plot
  `report 30min` / `report 6h` / `report 7d` -- custom window
  `report 6h at 2607141530` -- window CENTERED on a past time
       (time is yymmddhhmm; hh/mm optional, min precision is the day)
  `compressor [30min|6h|7d] [at <time>]` -- compressor plot
  `turbo [30min|6h|7d] [at <time>]` -- turbo pump plot
  `list-alarms` -- list active alarm thresholds and destinations
  `list-reports` -- list scheduled reports and where they go
  `suppress` -- suppress the last alarm that fired here
  `suppress <name>` -- suppress a specific alarm in this channel
  `unsuppress` -- lift all suppressions in this channel
  `unsuppress <name>` -- lift a specific suppression
  `help` -- show this message"""


class BF4Bot:
    """Main bot orchestrator."""

    def __init__(self, channel_id, config_file="config/alarm_config.yaml"):
        """
        Args:
            channel_id: Slack DM channel ID for interactive commands
            config_file: Path to config.yaml for scheduled reports and settings
        """
        self.channel_id = channel_id
        self.config = load_config(config_file)
        self.keys = load_keys()
        self.poll_interval = self.config.get("poll_interval", 30)
        # Store the raw config value (e.g. "operator_dm" or a key name)
        # and let _resolve_one() expand it on demand.  resolve_channel
        # alone cannot handle "operator_dm" / "dm_<nick>" magic strings
        # because it does not have access to the Slack client.
        self._alarm_channel_raw = self.config.get("alarm_channel", channel_id)
        # Back-compat: self.alarm_channel is still read in a few places
        # (e.g. startup log line).  Keep it as the raw string; real
        # dispatch uses _resolve_destinations/_resolve_one.
        self.alarm_channel = self._alarm_channel_raw
        self.report_hours = 24  # default for on-demand reports with no duration

        # Per-channel alarm suppression.  Key = (alarm_name, channel_id).
        # When a user says "suppress P4_high" in a channel, we add the
        # pair here.  check_alarms() skips sending to suppressed channels.
        # Suppression auto-lifts when the alarm CLEARS (condition returns
        # to normal), so the next fresh trigger sends normally.
        # In-memory only -- resets on bot restart (intentional: a restart
        # is a fresh start, all alarms should re-announce).
        self._suppressed = {}
        # Track the last alarm that fired per channel, so "suppress" with
        # no alarm name can suppress "whatever just fired here."
        self._last_alarm_per_channel = {}
        # Throttle for scheduled-report failure alerts: (report_name, dest)
        # -> date we last DMed the operator about a failed delivery.  Keeps
        # a multi-tick Slack outage from spamming one alert every poll.
        # In-memory only (resets on restart, like _suppressed above).
        self._report_failure_notified = {}
        # Lock protecting _suppressed and _last_alarm_per_channel.
        # check_alarms() runs on a background thread; suppress/unsuppress
        # are called from the Socket Mode main thread.  Without this lock,
        # mutating the dict while another thread iterates it can raise
        # RuntimeError.
        self._suppress_lock = threading.Lock()

        # Hardware settings live in config/hardware.yaml (separate file so
        # the user-facing config stays focused on routing + alerts).  Falls
        # back to built-in defaults if hardware.yaml is missing entirely.
        hw = load_hardware()
        api_host = hw.get("api_host", "https://localhost")
        api_port = hw.get("api_port", "49098")
        log_prefix = hw.get("log_prefix", "S:/_BlueFors4/Logs/")
        self.reader = FridgeReader(
            api_key=self.keys.get("bluefors_api_key", ""),
            api_host=api_host, api_port=api_port, log_prefix=log_prefix,
        )
        self.slack = BF4SlackClient(token=self.keys.get("slack_bot_token", ""))
        self.alarm_mgr = AlarmManager()

        # Scheduled reports live in config/reports.yaml and are managed
        # by a ReportManager that supports live reload (same pattern as
        # AlarmManager).  self.scheduled_reports is kept as a property-
        # style shortcut so existing call sites keep working.
        self.report_mgr = ReportManager()

        self._last_dm_timestamp = None
        self._bot_user_id = None

    # Human nouns for each on-demand report kind, used in captions/titles.
    _KIND_NOUNS = {
        "fridge": "Fridge Status",
        "compressor": "Compressor Status",
        "turbo": "Turbo Pump Status",
    }

    @staticmethod
    def _anchor_end(hours, anchor):
        """Window END for a report.

        A normal report ends at "now" (return None so read_history uses
        datetime.now()).  An at-time report is CENTERED on ``anchor``, so
        the window ends half a period AFTER it.
        """
        if anchor is None:
            return None
        return anchor + timedelta(hours=hours / 2.0)

    def _window_phrase(self, history):
        """Short window label: "Last 6h" or "6h around 2026-07-14 15:30"."""
        label = history.get("label", f"{history.get('hours', '')}h")
        anchor = history.get("anchor")
        if anchor is not None:
            return f"{label} around {anchor.strftime('%Y-%m-%d %H:%M')}"
        return f"Last {label}"

    def _report_warnings(self, history, kind):
        """User-facing warning lines for a report window (may be empty).

        * The window extends past the current time -- only possible for an
          at-time report anchored near/after now; the future portion has no
          data yet.
        * No samples at all fell inside the window (out-of-range at-time
          request, or the log files for that span are missing).
        """
        notes = []
        if history["now"] > datetime.now():
            notes.append(":warning: Window extends into the future; "
                         "the portion after now has no data yet.")
        if history_is_empty(history, kind):
            notes.append(":warning: No data found in this window.")
        return notes

    def _report_caption(self, kind, history, scheduled=False):
        """Build the Slack caption (initial_comment) for a report upload."""
        noun = self._KIND_NOUNS.get(kind, kind)
        phrase = self._window_phrase(history)
        if history.get("anchor") is not None:
            phrase += (f" ({history['cutoff'].strftime('%Y-%m-%d %H:%M')} to "
                       f"{history['now'].strftime('%Y-%m-%d %H:%M')})")
        lines = [f"BF4 {noun} Report -- {phrase}"]
        lines += self._report_warnings(history, kind)
        if scheduled:
            lines.append("To edit report schedule or destinations, "
                         "access the BF4 fridge PC.")
        return "\n".join(lines)

    def send_fridge_report(self, hours=None, channel_id=None,
                           subfolder=SUBFOLDER_ON_DEMAND, scheduled=False,
                           anchor=None, thread_ts=None):
        """
        Generate and send a fridge status report.

        ``subfolder`` selects where the PNG lands under plots/.  Defaults to
        on_demand; the scheduled-report path passes SUBFOLDER_DAILY.
        ``scheduled`` adds "edit on BF4 PC" footer to the Slack message.
        ``anchor`` (a datetime) centers the window on a past time instead of
        ending at now (issue #25).  ``thread_ts`` threads the reply under an
        @mention.  Both DM and @mention dispatch route through here so the
        two paths cannot diverge (the cause of #24).
        """
        hours = hours or self.report_hours
        target = channel_id or self.channel_id
        label = format_duration(hours)
        end = self._anchor_end(hours, anchor)
        when = f" around {anchor:%Y-%m-%d %H:%M}" if anchor is not None else ""
        print(f"[{datetime.now()}] Generating {label} fridge report{when}...")
        try:
            history = self.reader.read_history(hours=hours, end=end,
                                               anchor=anchor)
            plot_path = plot_fridge_status(history, subfolder=subfolder)
            comment = self._report_caption("fridge", history,
                                           scheduled=scheduled)
            self.slack.send_report(target, plot_path, comment=comment,
                                   thread_ts=thread_ts)
            print(f"  Report sent to {target}")
            return True
        except Exception as e:
            print(f"  Error sending report: {e}")
            traceback.print_exc()
            return False

    def send_compressor_report(self, hours=None, channel_id=None,
                               subfolder=SUBFOLDER_ON_DEMAND,
                               anchor=None, thread_ts=None):
        """Generate and send a compressor status report.

        ``channel_id`` lets scheduled reports target a specific destination;
        on-demand DM commands pass None and the report goes to self.channel_id.
        ``anchor``/``thread_ts`` as in send_fridge_report.
        """
        hours = hours or self.report_hours
        target = channel_id or self.channel_id
        label = format_duration(hours)
        end = self._anchor_end(hours, anchor)
        when = f" around {anchor:%Y-%m-%d %H:%M}" if anchor is not None else ""
        print(f"[{datetime.now()}] Generating {label} compressor report{when}...")
        try:
            # read_history now populates ALL compressor series (temps AND
            # cpalp/cpahp/cpacurrent), so no per-report augmentation is needed.
            # See reader.read_history comp_keys and issue #24.
            history = self.reader.read_history(hours=hours, end=end,
                                               anchor=anchor)
            plot_path = plot_compressor(history, subfolder=subfolder)
            title = f"BF4 Compressor Status -- {self._window_phrase(history)}"
            comment = self._report_caption("compressor", history)
            self.slack.upload_image(target, plot_path, title=title,
                                    comment=comment, thread_ts=thread_ts)
            print(f"  Compressor report sent to {target}")
            return True
        except Exception as e:
            print(f"  Error sending compressor report: {e}")
            traceback.print_exc()
            return False

    def send_turbo_report(self, hours=None, channel_id=None,
                          subfolder=SUBFOLDER_ON_DEMAND,
                          anchor=None, thread_ts=None):
        """Generate and send a turbo pump status report.

        ``channel_id`` lets scheduled reports target a specific destination;
        on-demand DM commands pass None and the report goes to self.channel_id.
        ``anchor``/``thread_ts`` as in send_fridge_report.
        """
        hours = hours or self.report_hours
        target = channel_id or self.channel_id
        label = format_duration(hours)
        end = self._anchor_end(hours, anchor)
        when = f" around {anchor:%Y-%m-%d %H:%M}" if anchor is not None else ""
        print(f"[{datetime.now()}] Generating {label} turbo report{when}...")
        try:
            history = self.reader.read_history(hours=hours, end=end,
                                               anchor=anchor)
            plot_path = plot_turbo_pumps(history, subfolder=subfolder)
            title = f"BF4 Turbo Pump Status -- {self._window_phrase(history)}"
            comment = self._report_caption("turbo", history)
            self.slack.upload_image(target, plot_path, title=title,
                                    comment=comment, thread_ts=thread_ts)
            print(f"  Turbo report sent to {target}")
            return True
        except Exception as e:
            print(f"  Error sending turbo report: {e}")
            traceback.print_exc()
            return False

    def _get_user_dm(self, user_id):
        """
        Resolve (and cache) the DM channel ID for any user ID.

        Slack DMs are not addressable by user ID directly for sending --
        you must first call conversations.open(users=[user_id]) which
        returns a channel ID of the form "D0...".  We cache results in
        a dict keyed by user_id so we only pay the API round-trip once
        per user per bot lifetime.
        """
        if not user_id:
            return None
        cache = getattr(self, "_dm_cache", None)
        if cache is None:
            cache = {}
            self._dm_cache = cache
        if user_id in cache:
            return cache[user_id]
        try:
            resp = self.slack.client.conversations_open(users=[user_id])
            dm_id = resp["channel"]["id"]
            cache[user_id] = dm_id
            return dm_id
        except Exception as e:
            print(f"[{datetime.now()}] Could not open DM for {user_id}: {e}")
            return None

    def _get_operator_dm(self):
        """Convenience: DM channel for the current operator.

        Resolved via channels.yaml's two-level mapping
        ('operator' nickname -> 'dm_users[nickname]' -> Slack user ID).
        """
        return self._get_user_dm(get_operator_user_id(self.keys))

    def _resolve_one(self, dest_ref):
        """
        Turn ONE destination string into a concrete Slack channel ID.

        Returns None on failure (the caller decides whether to fall back).
        This is the ONLY place that interprets destination strings; both
        per-alarm destinations and the global alarm_channel fallback go
        through here.

        Rules, in order:
          1. Empty / None             -> None
          2. "operator_dm"            -> DM channel for bot_operator_user_id
          3. "dm_<nickname>"          -> DM channel for channels.yaml's
                                         dm_users[nickname]
          4. Raw Slack ID (C/D/G)     -> used as-is
          5. Any other string         -> looked up in merged keys via
                                         resolve_channel (channels.yaml entries)
        """
        if not dest_ref:
            return None
        if dest_ref == "operator_dm":
            return self._get_operator_dm()
        if dest_ref.startswith("dm_"):
            nick = dest_ref[3:]
            dm_users = self.keys.get("dm_users") or {}
            uid = dm_users.get(nick)
            if not uid:
                print(f"[{datetime.now()}] dm_{nick} has no matching entry "
                      f"in channels.yaml dm_users. Skipping.")
                return None
            return self._get_user_dm(uid)
        return resolve_channel(dest_ref, self.keys)

    def _resolve_destinations(self, alarm):
        """
        Turn an alarm's list of destinations into concrete Slack channel IDs.

        If the alarm has no destinations set, we use the global alarm_channel
        from config.yaml -- but we run IT through the same resolver so names
        like "operator_dm" expand correctly there too.
        """
        if not alarm.destinations:
            fallback = self._resolve_one(self._alarm_channel_raw)
            return [fallback] if fallback else []
        resolved = []
        for d in alarm.destinations:
            r = self._resolve_one(d)
            if r:
                resolved.append(r)
            else:
                print(f"[{datetime.now()}] Skipping destination '{d}' for "
                      f"alarm '{alarm.name}' (could not resolve).")
        # If nothing resolved, try the global fallback so the alarm still
        # goes SOMEWHERE rather than vanishing silently.
        if not resolved:
            fb = self._resolve_one(self._alarm_channel_raw)
            if fb:
                resolved = [fb]
        return resolved

    def check_alarms(self):
        """Poll the fridge API and check all alarms."""
        try:
            # Pick up any edits the user made to alarms.yaml since last poll.
            # reload_if_changed() is a cheap mtime check; load() only runs
            # when the file's timestamp has advanced.
            if self.alarm_mgr.reload_if_changed():
                print(f"[{datetime.now()}] alarms.yaml reloaded "
                      f"({len(self.alarm_mgr.alarms)} alarm(s))")
                # Bug 3 fix: prune suppression entries for alarms that
                # were deleted from alarms.yaml, so the dict doesn't leak.
                current_names = {a.name for a in self.alarm_mgr.alarms}
                with self._suppress_lock:
                    orphaned = [k for k in self._suppressed
                                if k[0] not in current_names]
                    for k in orphaned:
                        del self._suppressed[k]

            snapshot = self.reader.get_all_live()
            triggered, cleared = self.alarm_mgr.check_all(snapshot)

            for alarm, value in triggered:
                sensor = snapshot.get(alarm.variable, {})
                unit = sensor.get("unit", "")
                # Main alarm message + a footer with user instructions.
                msg = (
                    f"WARNING: {alarm.name} -- {alarm.variable} = "
                    f"{value:.4g} {unit} "
                    f"(threshold: {alarm.condition} {alarm.threshold} {unit}, "
                    f"re-alerts every {alarm.interval_min}m)\n"
                    f"Reply `suppress {alarm.name}` to mute this alarm "
                    f"in this channel.  "
                    f"To edit alarm settings, access the BF4 fridge PC."
                )
                print(f"[{datetime.now()}] ALARM: {alarm.name}")

                self.alarm_mgr.log_trigger(alarm, value)

                plot_path = None
                try:
                    history = self.reader.read_history(
                        hours=max(1, alarm.interval_min / 60 * 3))
                    ts, vals = self._get_alarm_plot_data(
                        alarm.variable, history)
                    if ts:
                        plot_path = plot_alarm_data(
                            ts, vals, alarm.variable, alarm.threshold, unit,
                            alarm_name=alarm.name,
                        )
                except Exception as e:
                    print(f"  Could not generate alarm plot: {e}")

                # Snapshot the suppression set under the lock, then release
                # BEFORE making Slack API calls.  Holding the lock during
                # network I/O would block suppress/unsuppress commands.
                with self._suppress_lock:
                    suppressed_snapshot = set(self._suppressed.keys())
                targets = self._resolve_destinations(alarm)
                for target in targets:
                    if (alarm.name, target) in suppressed_snapshot:
                        print(f"  Suppressed {alarm.name} in {target}")
                        continue
                    self.slack.send_alarm(target, msg, plot_path)
                    with self._suppress_lock:
                        self._last_alarm_per_channel[target] = alarm.name

            for alarm, value in cleared:
                sensor = snapshot.get(alarm.variable, {})
                unit = sensor.get("unit", "")
                msg = (
                    f"CLEARED: {alarm.name} -- {alarm.variable} = "
                    f"{value:.4g} {unit} "
                    f"(back below {alarm.threshold} {unit})"
                )
                print(f"[{datetime.now()}] CLEARED: {msg}")
                # Auto-lift ALL suppressions for this alarm across every
                # channel + clear stale last-alarm tracking (Bug 4 fix).
                with self._suppress_lock:
                    lifted = [k for k in self._suppressed
                              if k[0] == alarm.name]
                    for k in lifted:
                        del self._suppressed[k]
                    stale = [ch for ch, name in
                             self._last_alarm_per_channel.items()
                             if name == alarm.name]
                    for ch in stale:
                        del self._last_alarm_per_channel[ch]
                if lifted:
                    print(f"  Lifted {len(lifted)} suppression(s) for "
                          f"{alarm.name}")
                for target in self._resolve_destinations(alarm):
                    self.slack.send_message(target, msg)

        except Exception as e:
            print(f"[{datetime.now()}] Error checking alarms: {e}")
            traceback.print_exc()

    def _resolve_alarm_name(self, user_input):
        """
        Case-insensitive alarm name lookup (Bug 2 fix).

        Returns the canonical alarm name from alarms.yaml, or None if
        no match.  E.g., user types 'p4_high' -> returns 'P4_high'.
        """
        if not user_input:
            return None
        lower = user_input.lower()
        for a in self.alarm_mgr.alarms:
            if a.name.lower() == lower:
                return a.name
        return None

    def suppress_alarm(self, alarm_name, channel_id):
        """
        Suppress an alarm in a specific channel.

        After this call, the alarm will not be sent to ``channel_id``
        until it CLEARS (condition returns to normal) and triggers fresh.

        If ``alarm_name`` is None or empty, suppress the most recent alarm
        that fired in this channel (convenience for "just shut up").

        Returns a human-readable status string for the caller to relay.
        """
        with self._suppress_lock:
            if not alarm_name:
                alarm_name = self._last_alarm_per_channel.get(channel_id)
                if not alarm_name:
                    return ("No alarm to suppress -- nothing has fired "
                            "in this channel recently.")

            # Case-insensitive name resolution (Bug 2 fix).
            canonical = self._resolve_alarm_name(alarm_name)
            if not canonical:
                known = sorted(a.name for a in self.alarm_mgr.alarms)
                return (f"No alarm named '{alarm_name}'. "
                        f"Known: {', '.join(known) or '(none)'}.")
            alarm_name = canonical

            # Bug 5 fix: check this channel is actually a destination.
            alarm_obj = next(a for a in self.alarm_mgr.alarms
                            if a.name == alarm_name)
            targets = self._resolve_destinations(alarm_obj)
            if channel_id not in targets:
                return (f"Alarm `{alarm_name}` does not fire to this "
                        f"channel.  Its destinations are: "
                        f"{', '.join(alarm_obj.destinations) or '(default)'}.")

            key = (alarm_name, channel_id)
            if key in self._suppressed:
                return f"Alarm `{alarm_name}` is already suppressed here."

            self._suppressed[key] = True

        print(f"[{datetime.now()}] Suppressed {alarm_name} in {channel_id}")
        return (f"Alarm `{alarm_name}` suppressed in this channel. "
                f"It will resume when the condition clears and re-triggers.")

    def unsuppress_alarm(self, alarm_name, channel_id):
        """Manually lift suppression for an alarm in a channel."""
        with self._suppress_lock:
            if not alarm_name:
                lifted = [k for k in list(self._suppressed)
                          if k[1] == channel_id]
                if not lifted:
                    return "Nothing is suppressed in this channel."
                for k in lifted:
                    del self._suppressed[k]
                names = ", ".join(k[0] for k in lifted)
                return f"Unsuppressed: {names}"

            # Case-insensitive lookup (Bug 2 fix).
            canonical = self._resolve_alarm_name(alarm_name)
            if canonical:
                alarm_name = canonical

            key = (alarm_name, channel_id)
            if key not in self._suppressed:
                return f"Alarm `{alarm_name}` is not suppressed here."
            del self._suppressed[key]
        return f"Alarm `{alarm_name}` unsuppressed in this channel."

    def fire_test_alarm(self, alarm_name):
        """
        Fire a single fake trigger of the named alarm.

        Used by the Slack ``test <alarm_name>`` command and (later) by the
        widget GUI's [fire] button.  Returns a short human-readable status
        string the caller can relay to the user.

        Behaviour: sends the same kind of message + plot a real trigger
        would produce, to the alarm's own destination.  Does NOT write to
        alarm_history.log (we do not want test events polluting the 24h
        trigger-rate stats).  Does NOT update cooldowns.
        """
        alarm = next((a for a in self.alarm_mgr.alarms if a.name == alarm_name), None)
        if alarm is None:
            names = ", ".join(a.name for a in self.alarm_mgr.alarms) or "(none)"
            return f"No alarm named '{alarm_name}'. Known: {names}"

        # Fabricate a value just past the threshold so the message looks real.
        fake_value = (alarm.threshold + 1.0 if alarm.condition == ">"
                      else alarm.threshold - 1.0)

        snapshot = {}  # not used for test, plot pulls history directly
        unit = ""
        try:
            # Try to pull the real unit for prettier message formatting.
            live = self.reader.get_all_live()
            sensor = live.get(alarm.variable, {})
            unit = sensor.get("unit", "")
        except Exception:
            pass

        msg = (
            f":test_tube: [TEST] {alarm.name} -- {alarm.variable} = "
            f"{fake_value:.4g} {unit} "
            f"(threshold: {alarm.condition} {alarm.threshold} {unit})\n"
            f"This is a manual test trigger; condition may not actually be met."
        )
        print(f"[{datetime.now()}] TEST ALARM fired: {alarm.name}")

        plot_path = None
        try:
            history = self.reader.read_history(hours=max(1, alarm.interval_min / 60 * 3))
            ts, vals = self._get_alarm_plot_data(alarm.variable, history)
            if ts:
                plot_path = plot_alarm_data(
                    ts, vals, alarm.variable, alarm.threshold, unit,
                    alarm_name=alarm.name,
                )
        except Exception as e:
            print(f"  Could not generate test alarm plot: {e}")

        targets = self._resolve_destinations(alarm)
        # tag_channel=False so test alarms do not spam @channel.
        for target in targets:
            self.slack.send_alarm(target, msg, plot_path, tag_channel=False)
        # Show the destination NAMES (not the cryptic Slack channel IDs) in
        # the confirmation -- far friendlier for the user.
        dest_names = ", ".join(alarm.destinations) if alarm.destinations \
            else "alarm_channel (global default)"
        return f"Fired test alarm '{alarm_name}' -> {dest_names}"

    def _get_alarm_plot_data(self, variable, history):
        """Extract timestamps and values for a specific variable from history."""
        if variable == "T_CompressorWaterOut":
            # Not a fridge stage in history["temps"] -- it lives in the
            # Status-log compressor series.  collect_status stores each key
            # as a (timestamps, values) tuple, which is exactly our return
            # shape (issue #26).
            return history["compressor"].get("cpatempwo", ([], []))
        if variable.startswith("T_"):
            stage = variable[2:]  # "T_MXC" -> "MXC"
            data = history["temps"].get(stage, {})
            ts = data.get("timestamps", [])
            vals = data.get("values", [])
            if stage in ["MXC", "Still"]:
                vals = [v * 1000 for v in vals]  # K -> mK
            return ts, vals
        elif variable.startswith("P"):
            idx = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "P5": 4, "P6": 5}.get(variable)
            if idx is not None:
                ts = history["pressures"]["timestamps"]
                vals = [row[idx] for row in history["pressures"]["values"]]
                return ts, vals
        elif variable == "Flow":
            return history["flow"]["timestamps"], history["flow"]["values"]
        return [], []

    def check_dm_commands(self):
        """Poll for new DMs and respond to commands."""
        try:
            # Get bot's own user ID (first time only)
            if self._bot_user_id is None:
                auth = self.slack.client.auth_test()
                self._bot_user_id = auth["user_id"]

            # Read recent messages in the DM channel
            kwargs = {"channel": self.channel_id, "limit": 10}
            if self._last_dm_timestamp:
                kwargs["oldest"] = self._last_dm_timestamp

            result = self.slack.client.conversations_history(**kwargs)
            messages = result.get("messages", [])

            if not messages:
                return

            # Update timestamp to newest message
            self._last_dm_timestamp = messages[0]["ts"]

            # Process messages (newest first, so reverse for chronological order)
            for msg in reversed(messages):
                # Skip bot's own messages
                if msg.get("user") == self._bot_user_id:
                    continue
                if msg.get("bot_id"):
                    continue

                text = msg.get("text", "").strip().lower()
                if not text:
                    continue

                print(f"[{datetime.now()}] DM command: '{text}'")
                self._handle_command(text)

        except Exception as e:
            print(f"[{datetime.now()}] Error checking DMs: {e}")
            traceback.print_exc()

    def _handle_command(self, text):
        """Parse and execute a DM command."""
        parts = text.split()
        cmd = parts[0]

        if cmd == "status":
            self._cmd_status()
        elif cmd in ("report", "compressor", "turbo"):
            try:
                hours, anchor = parse_report_request(parts)
            except ValueError as e:
                self.slack.send_message(self.channel_id, f":warning: {e}")
                return
            sender = {
                "report": self.send_fridge_report,
                "compressor": self.send_compressor_report,
                "turbo": self.send_turbo_report,
            }[cmd]
            sender(hours=hours, anchor=anchor)
        elif cmd == "alarms":
            self.slack.send_message(self.channel_id, self.alarm_mgr.list_alarms())
        elif cmd == "help":
            self.slack.send_message(self.channel_id, HELP_TEXT)
        else:
            self.slack.send_message(
                self.channel_id,
                f"Unknown command: `{text}`\nType `help` for available commands."
            )

    def _cmd_status(self):
        """Respond with a formatted live status message."""
        try:
            snapshot = self.reader.get_all_live()
            lines = ["*BF4 Live Status:*"]
            lines.append("")

            lines.append("*Temperatures:*")
            for key in ["T_MXC", "T_Still", "T_4K", "T_50K"]:
                d = snapshot[key]
                if d["value"] is not None:
                    lines.append(f"  {key[2:]}: {d['value']:.4g} {d['unit']}")
                else:
                    lines.append(f"  {key[2:]}: -- ({d['status']})")

            lines.append("")
            lines.append("*Pressures:*")
            for key in ["P1", "P2", "P3", "P4", "P5", "P6"]:
                d = snapshot[key]
                if d["value"] is not None:
                    lines.append(f"  {key}: {d['value']:.4g} {d['unit']}")
                else:
                    lines.append(f"  {key}: -- ({d['status']})")

            lines.append("")
            lines.append("*Other:*")
            d = snapshot["Flow"]
            lines.append(f"  Flow: {d['value']:.4g} {d['unit']}" if d["value"] else "  Flow: --")
            d = snapshot["Compressor"]
            lines.append(f"  Compressor: {'Running' if d['value'] else 'OFF'}")
            d = snapshot["T_CompressorWaterOut"]
            if d["value"] is not None:
                lines.append(f"  Compressor water out: {d['value']:.4g} {d['unit']}")
            else:
                lines.append(f"  Compressor water out: -- ({d['status']})")
            d = snapshot["PulseTube"]
            lines.append(f"  Pulse Tube: {'Running' if d['value'] else 'OFF'}")

            # Turbo pumps -- show status + overtemp flag (OK / WARM).
            # If the value is None the sensor is unreachable this poll.
            for idx in (1, 2):
                t = snapshot.get(f"Turbo{idx}")
                ot = snapshot.get(f"Turbo{idx}_ovtemp")
                if t is None:
                    continue
                state = (
                    "Running" if t["value"] is True else
                    "OFF"     if t["value"] is False else
                    f"-- ({t.get('status', '?')})"
                )
                if ot and ot["value"] is True:
                    state += "  [OVERTEMP!]"
                lines.append(f"  Turbo {idx}: {state}")

            self.slack.send_message(self.channel_id, "\n".join(lines))
        except Exception as e:
            self.slack.send_message(self.channel_id, f"Error reading fridge status: {e}")

    def write_heartbeat(self, path="bot_heartbeat.txt"):
        """Write a timestamp file so external watchdog can tell if bot is alive.

        Writes to a sibling .tmp file first and then os.replace()s it onto
        the real filename.  os.replace is atomic on Windows and POSIX, so a
        reader (the watchdog) either sees the previous complete file or the
        new complete file -- never the 0-byte window that plain open(path,
        "w") creates between truncate and write.  Without this, the watchdog
        occasionally caught an empty file mid-write and falsely flipped the
        bot to OFFLINE.
        """
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(datetime.now().isoformat())
            os.replace(tmp, path)
        except Exception as e:
            print(f"[{datetime.now()}] Heartbeat write failed: {e}")

    def check_scheduled_reports(self):
        """Check all scheduled reports from reports.yaml and send if due."""
        # Pick up any edits the user made via the GUI or editor.
        if self.report_mgr.reload_if_changed():
            print(f"[{datetime.now()}] reports.yaml reloaded "
                  f"({len(self.report_mgr.reports)} report(s))")
        now = datetime.now()
        for report in self.report_mgr.reports:
            if not report.is_due_now(now):
                continue
            # Only the destinations that have NOT already received today's
            # report -- so a partially-failed send retries just the missing
            # ones instead of re-sending to everyone.
            pending = report.pending_destinations(now)
            if not pending:
                continue
            print(f"[{now}] Scheduled report: {report.name} "
                  f"-> {', '.join(pending)}")

            for dest in pending:
                target = self._resolve_one(dest)
                if not target:
                    print(f"  Could not resolve destination '{dest}' "
                          f"-- skipping.")
                    continue
                try:
                    ok = self._send_scheduled_report(report, target)
                except Exception as e:
                    print(f"  Error sending '{report.name}' -> {dest}: {e}")
                    ok = False
                if ok:
                    # Persist this destination as it succeeds, so it is only
                    # ever sent once/day and a restart won't re-fire a
                    # delivered one (issue #19).
                    self.report_mgr.mark_sent(report, dest, now)
                    self._clear_report_failure(report.name, dest)
                else:
                    # Leave this destination unmarked so the next poll tick
                    # retries it, and alert the operator (throttled).
                    print(f"  '{report.name}' -> {dest} failed; "
                          f"will retry on the next poll.")
                    self._notify_report_failure(report, dest, now)

    def _send_scheduled_report(self, report, target):
        """Dispatch ONE scheduled report to ONE resolved Slack target.

        Returns True on success, False otherwise.  Centralises the
        report_type -> send_* mapping so check_scheduled_reports stays flat.
        """
        if report.report_type == "fridge":
            return self.send_fridge_report(
                hours=report.hours, channel_id=target,
                subfolder=SUBFOLDER_DAILY, scheduled=True)
        elif report.report_type == "compressor":
            return self.send_compressor_report(
                hours=report.hours, channel_id=target,
                subfolder=SUBFOLDER_DAILY)
        elif report.report_type == "turbo":
            return self.send_turbo_report(
                hours=report.hours, channel_id=target,
                subfolder=SUBFOLDER_DAILY)
        print(f"  Unknown report type: {report.report_type}")
        return False

    def _notify_report_failure(self, report, dest, now):
        """DM the operator that a scheduled report could not be delivered to
        ``dest``.  Throttled to at most once per (report, dest) per day so a
        multi-tick outage does not spam.  Best-effort: if the alert DM itself
        fails (e.g. Slack is fully down) we just log it -- the report is
        still retried every poll and delivered once Slack recovers.
        """
        key = (report.name, dest)
        if self._report_failure_notified.get(key) == now.date():
            return
        self._report_failure_notified[key] = now.date()
        msg = (f":warning: Scheduled report *{report.name}* could not be "
               f"sent to `{dest}` (Slack error; see bot log). The bot will "
               f"keep retrying every poll and will deliver it once Slack "
               f"recovers.")
        try:
            operator = self._resolve_one("operator_dm")
            if operator:
                self.slack.send_message(operator, msg)
        except Exception as e:
            print(f"  Could not send failure alert for '{report.name}' "
                  f"-> {dest}: {e}")

    def _clear_report_failure(self, name, dest):
        """Reset the failure-alert throttle after a successful delivery, so a
        future failure of the same (report, dest) alerts again."""
        self._report_failure_notified.pop((name, dest), None)

    def run(self):
        """Main loop. Runs forever, polling alarms and checking report schedule."""
        print(f"BF4-bot starting...")
        print(f"  DM channel: {self.channel_id}")
        print(f"  Alarm channel: {self.alarm_channel}")
        print(f"  Poll interval: {self.poll_interval}s")
        print(f"  Scheduled reports:")
        for r in self.report_mgr.reports:
            print(f"    - {r}")
        print(f"  Alarms: {len(self.alarm_mgr.alarms)}")
        print(f"  {self.alarm_mgr.list_alarms()}")
        print()

        while True:
            try:
                self.check_alarms()
                self.check_dm_commands()
                self.check_scheduled_reports()
            except Exception as e:
                print(f"[{datetime.now()}] Unexpected error in main loop: {e}")
                traceback.print_exc()

            time.sleep(self.poll_interval)
