"""
Load bot configuration files.

The project's YAML files live in a ``config/`` subfolder:
    config/config.yaml       -- bot behaviour (intervals, reports, allowlist)
    config/alarms.yaml       -- alarm definitions (edited by GUI)
    config/channels.yaml     -- Slack channel IDs + DM user IDs (non-secret)
    config/keys.yaml         -- API keys + Slack tokens (sensitive)

``load_keys`` merges ``keys.yaml`` and ``channels.yaml`` into a single dict
for the rest of the codebase, so callers do not need to know about the
split.  The split exists purely so users editing destinations never have
to open the file containing credentials.
"""

import json
import os
import yaml
from datetime import date, datetime

DEFAULT_CONFIG_FILE = "config/alarm_config.yaml"
DEFAULT_REPORT_CONFIG_FILE = "config/report_config.yaml"
DEFAULT_KEYS_FILE = "config/keys.yaml"
DEFAULT_CHANNELS_FILE = "config/channels.yaml"
DEFAULT_REPORTS_FILE = "config/reports.yaml"
DEFAULT_HARDWARE_FILE = "config/hardware.yaml"

# Persisted runtime state so a bot restart does NOT re-fire reports that
# already went out today.  Lives at project root alongside other state
# files (bot_heartbeat.txt, widget_position.json, etc.).  Gitignored.
DEFAULT_REPORT_STATE_FILE = "report_state.json"

# Sentinel key used when loading the LEGACY report_state.json format, where
# each report stored a single "sent" date instead of a per-destination map.
# _apply_persisted_state() expands it to the report's current destinations.
_LEGACY_ALL = "__legacy_all__"


def _load_yaml(path, required=True):
    """Small helper: read a YAML file and return its top-level dict.

    Missing file -> FileNotFoundError if required=True, else returns {}.
    """
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"If you just upgraded, run scripts/migrate_to_config_folder.py."
            )
        return {}
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def load_keys(keys_file=DEFAULT_KEYS_FILE, channels_file=DEFAULT_CHANNELS_FILE):
    """
    Load and merge keys.yaml (tokens) + channels.yaml (Slack IDs).

    Returns a flat dict.  Channel keys and the top-level dm_users map
    are merged in alongside the sensitive keys, so existing code that
    does e.g. ``keys["fluxonium_kids_channel"]`` or
    ``keys["bot_operator_user_id"]`` keeps working unchanged.

    ``channels_file`` is optional: if it does not exist (fresh clone,
    pre-migration) we still return the keys dict and let callers fall
    back to whatever defaults they already have.
    """
    keys = _load_yaml(keys_file, required=True)
    channels = _load_yaml(channels_file, required=False)
    # Shallow merge: channel entries add to the keys dict.  If a key
    # collision happens (it should not -- tokens in keys, IDs in channels),
    # channels.yaml values win because they are written last.
    merged = dict(keys)
    merged.update(channels)
    return merged


def load_config(config_file=DEFAULT_CONFIG_FILE):
    """Load and return the alarm / bot config dict from YAML."""
    return _load_yaml(config_file, required=True)


def load_report_config(report_config_file=DEFAULT_REPORT_CONFIG_FILE):
    """Load the Report editor's config (just ``report_destinations`` for now).

    Missing file is OK (returns {}), callers fall back to their own defaults.
    """
    return _load_yaml(report_config_file, required=False)


def load_hardware(hardware_file=DEFAULT_HARDWARE_FILE):
    """
    Load fridge hardware / infrastructure settings from hardware.yaml.

    This is a SEPARATE file from config.yaml on purpose: api_host, api_port,
    and log_prefix are rarely touched and don't belong next to user-facing
    knobs.  Missing file is OK -- the reader has built-in defaults.

    Returns a dict (possibly empty).  Callers use ``.get()`` with a default.
    """
    return _load_yaml(hardware_file, required=False)


def get_operator_user_id(keys):
    """
    Two-level lookup to find the current operator's Slack user ID.

    Reads ``keys['operator']`` (a nickname string like "chuyao"), then
    looks that nickname up in ``keys['dm_users']`` to get the user ID.

    Returns None if either lookup fails -- callers should treat that as
    "no operator configured".

    Why two levels: changing who the operator is now means editing ONE
    line (channels.yaml's 'operator:' field), not scattering user IDs
    across files.  New people go in dm_users once, can then be pointed
    at by the operator key OR alarm destinations (dm_<nickname>).
    """
    if not keys:
        return None
    nickname = keys.get("operator")
    if not nickname:
        return None
    dm_users = keys.get("dm_users") or {}
    return dm_users.get(nickname)


def resolve_channel(channel_ref, keys):
    """Resolve a channel reference -- either a direct ID or a key name from keys.yaml."""
    if channel_ref and channel_ref[0] in ("C", "D", "G"):
        return channel_ref  # Already a Slack channel ID
    return keys.get(channel_ref, channel_ref)


class ScheduledReport:
    """A single scheduled report definition."""

    def __init__(self, name, destinations, hour, days, hours, report_type):
        self.name = name
        # destinations is always a list internally (like Alarm.destinations).
        if isinstance(destinations, str):
            self.destinations = [destinations]
        elif destinations is None:
            self.destinations = []
        else:
            self.destinations = [str(d) for d in destinations]
        self.hour = hour
        self.days = days
        self.hours = hours
        self.report_type = report_type
        # Per-destination "already sent today" dates, keyed by the raw
        # destination string (e.g. "operator_dm", "bf4_channel").  Tracked
        # per destination (not a single report-wide date) so a send that
        # reached SOME destinations but failed others retries only the ones
        # that did not get it, instead of re-sending to everyone.
        self._last_sent_by_dest = {}

    def is_due_now(self, now):
        """True if this report's schedule window (hour-of-day + day-of-week)
        is active at ``now`` -- independent of what has already been sent.

        Per-destination "already sent today" is checked separately via
        ``pending_destinations`` so a partially-failed send can retry.
        """
        # Use >= (i.e. now.hour < self.hour -> not yet) so a report due at
        # hour 9 still fires if the bot was down/paused during hour 9 and
        # comes back at hour 10.  Without this, missed reports are skipped
        # forever.
        if now.hour < self.hour:
            return False
        weekday = now.weekday()
        if self.days == "daily":
            return True
        elif self.days == "weekdays":
            return weekday < 5
        elif self.days == "weekly-monday":
            return weekday == 0
        elif self.days == "weekly-friday":
            return weekday == 4
        return False

    def already_sent_today(self, dest, now):
        """True if ``dest`` already received this report on ``now``'s date."""
        return self._last_sent_by_dest.get(dest) == now.date()

    def pending_destinations(self, now):
        """Destinations that have NOT yet received today's report."""
        return [d for d in self.destinations
                if not self.already_sent_today(d, now)]

    def mark_sent(self, dest, now):
        """Record that ``dest`` received this report on ``now``'s date."""
        self._last_sent_by_dest[dest] = now.date()

    def __str__(self):
        dest = ", ".join(self.destinations) if self.destinations else "(default)"
        return (f"{self.name}: {self.report_type} at {self.hour}:00 "
                f"({self.days}) -> {dest}")

    def to_dict(self):
        """Serialise for YAML round-trip (used by the Report editor GUI)."""
        d = {
            "name": self.name,
            "type": self.report_type,
            "hour": self.hour,
            "days": self.days,
            "hours": self.hours,
        }
        # destinations: single string if one, list if multiple, omitted if empty.
        if len(self.destinations) == 1:
            d["destinations"] = self.destinations[0]
        elif len(self.destinations) > 1:
            d["destinations"] = list(self.destinations)
        return d


def _report_from_entry(entry):
    """
    Build a ScheduledReport from a YAML entry.

    Accepts both the NEW schema (``destinations`` as string or list) and
    the OLD schema (``channel`` as a single string) so pre-split
    configs still load.
    """
    # Prefer 'destinations' (new) over 'channel' (legacy single).
    if "destinations" in entry:
        dests = entry["destinations"]
    elif "channel" in entry:
        dests = [entry["channel"]]
    else:
        dests = []
    return ScheduledReport(
        name=entry.get("name", "Unnamed"),
        destinations=dests,
        hour=entry.get("hour", 9),
        days=entry.get("days", "daily"),
        hours=entry.get("hours", 24),
        report_type=entry.get("type", "fridge"),
    )


def load_reports(config=None, keys=None, reports_file=DEFAULT_REPORTS_FILE):
    """
    Load all scheduled reports.

    Looks first at ``config/reports.yaml``.  For back-compat, if that file
    does not exist and ``config`` contains a ``reports:`` list, fall back
    to reading from there instead.

    Keeps the (config, keys) positional parameters for call-site
    compatibility with the pre-refactor signature; ``keys`` is currently
    unused (destinations are resolved at dispatch time by the bot, not
    at load time -- operator_dm / dm_<nick> need Slack API access).
    """
    reports = []
    if os.path.exists(reports_file):
        data = _load_yaml(reports_file, required=False)
        for entry in data.get("reports", []):
            reports.append(_report_from_entry(entry))
    elif config is not None:
        # Legacy path -- pre-split config.yaml still has a reports: list.
        for entry in config.get("reports", []):
            reports.append(_report_from_entry(entry))
    return reports


class ReportManager:
    """
    Load/save/live-reload wrapper around reports.yaml.

    Mirrors the design of AlarmManager: keeps an in-memory list of
    ScheduledReport objects, writes atomically on save(), detects
    file changes via mtime in reload_if_changed().
    """

    def __init__(self, reports_file=DEFAULT_REPORTS_FILE,
                 state_file=DEFAULT_REPORT_STATE_FILE):
        self.reports_file = reports_file
        self.state_file = state_file
        self.reports = []
        self._last_mtime = None
        self.load()
        # Apply persisted "already sent today" state from disk so a bot
        # restart past the scheduled `hour` does NOT re-fire reports that
        # already went out today.  This is the restart-case twin of the
        # in-memory transfer done in reload_if_changed().  See #19.
        self._apply_persisted_state()

    def _load_report_state(self):
        """Read the persisted send-state from state_file.

        Returns ``{report_name: {dest: date}}``.  Two on-disk formats are
        accepted:
          * NEW (per-destination):
                {"Daily fridge report": {"operator_dm": "2026-08-25"}}
          * LEGACY (single report-wide date, pre per-destination retry):
                {"Daily fridge report": "2026-08-25"}
            A legacy value is returned under the _LEGACY_ALL sentinel key
            and expanded to the report's current destinations in
            _apply_persisted_state() (which knows them).
        Missing/corrupt -> {}.
        """
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r") as f:
                raw = json.load(f)
            out = {}
            for name, v in raw.items():
                if isinstance(v, dict):          # new per-destination format
                    parsed = {d: date.fromisoformat(s)
                              for d, s in v.items() if s}
                    if parsed:
                        out[name] = parsed
                elif v:                          # legacy single date string
                    out[name] = {_LEGACY_ALL: date.fromisoformat(v)}
            return out
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"[{datetime.now()}] {self.state_file} failed to load "
                  f"({type(e).__name__}: {e}); treating as empty.")
            return {}

    def _save_report_state(self):
        """Atomically write current per-destination send-state to state_file.

        Only writes reports/destinations that actually have a date -- orphan
        entries (renamed/removed reports, or destinations removed from a
        report) are dropped.
        """
        data = {}
        for r in self.reports:
            sent = {dest: d.isoformat()
                    for dest, d in r._last_sent_by_dest.items()
                    if d is not None and dest in r.destinations}
            if sent:
                data[r.name] = sent
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.state_file)
        except OSError as e:
            print(f"[{datetime.now()}] {self.state_file} save failed: {e}")

    def _apply_persisted_state(self):
        """Patch in-memory reports with persisted per-destination state."""
        state = self._load_report_state()
        for r in self.reports:
            entry = state.get(r.name)
            if not entry:
                continue
            if _LEGACY_ALL in entry:
                # Legacy single-date entry: treat as all CURRENT destinations
                # having been sent that day, preserving the #19 restart guard
                # across the upgrade to per-destination state.
                d = entry[_LEGACY_ALL]
                r._last_sent_by_dest = {dest: d for dest in r.destinations}
            else:
                r._last_sent_by_dest = dict(entry)

    def mark_sent(self, report, dest, now):
        """Record that ``report`` was delivered to ``dest`` and persist it,
        so a bot restart preserves the per-destination "already sent today"
        knowledge (issue #19).
        """
        report.mark_sent(dest, now)
        self._save_report_state()

    def load(self):
        self.reports = load_reports(reports_file=self.reports_file)
        try:
            self._last_mtime = os.path.getmtime(self.reports_file) \
                if os.path.exists(self.reports_file) else None
        except OSError:
            self._last_mtime = None

    def save(self):
        data = {"reports": [r.to_dict() for r in self.reports]}
        os.makedirs(os.path.dirname(self.reports_file) or ".", exist_ok=True)
        with open(self.reports_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        try:
            self._last_mtime = os.path.getmtime(self.reports_file)
        except OSError:
            self._last_mtime = None

    def reload_if_changed(self):
        """
        Re-read reports.yaml if its mtime has advanced.

        On parse failure, keep the last-good in-memory state rather than
        wiping the reports list -- so a mid-save or typo does not break
        scheduled reports until the user notices.
        """
        if not os.path.exists(self.reports_file):
            return False
        try:
            mtime = os.path.getmtime(self.reports_file)
        except OSError:
            return False
        if self._last_mtime is None or mtime > self._last_mtime:
            prev_reports = list(self.reports)
            prev_mtime = self._last_mtime
            try:
                self.load()
                # Preserve per-destination "already sent today" state across
                # reloads.  Without this, reconstructing ScheduledReport
                # objects resets _last_sent_by_dest to {} and any report
                # past its `hour` threshold today fires again on the next
                # tick (issue #19).  Match by name: renaming a report
                # deliberately resets its history; any other edit
                # (destination / hour / days / hours) preserves it.  A
                # newly-added destination simply has no entry yet, so it is
                # treated as not-sent and goes out on the next due tick.
                prev_by_name = {r.name: r for r in prev_reports}
                for new in self.reports:
                    prev = prev_by_name.get(new.name)
                    if prev is not None and prev._last_sent_by_dest:
                        new._last_sent_by_dest = dict(prev._last_sent_by_dest)
                # Resync the disk state file with the new name set so
                # renamed/removed reports don't leave orphan entries.
                self._save_report_state()
                return True
            except Exception as e:
                from datetime import datetime
                print(f"[{datetime.now()}] reports.yaml failed to reload "
                      f"({type(e).__name__}: {e}); keeping previous "
                      f"in-memory reports.")
                self.reports = prev_reports
                self._last_mtime = prev_mtime
                return False
        return False
