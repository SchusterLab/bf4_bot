"""
Alarm system for BF4 fridge monitoring.
Configurable thresholds with per-alarm cooldown timers.

Alarm definitions are stored in alarms.yaml (user-editable).

Alarm *trigger events* (i.e. every time an alarm actually fires) are appended
to ``alarm_history.log`` as JSON Lines -- one JSON object per line.  The
watchdog/widget reads this file to report "how many times has this alarm
fired in the last 24 hours?".  We use JSONL (not a big JSON array) because:

  * Appending a line is a single atomic ``open(..., "a")`` call -- no need to
    read+parse+rewrite the whole file on every alarm.
  * Partial corruption only loses one line, not the whole history.
  * It is trivially readable with a text editor if anything goes wrong.

Example line::

    {"ts": "2026-04-15T14:23:01", "name": "P4_high", "variable": "P4",
     "value": 812.3, "threshold": 800, "condition": ">"}
"""

import json
import os
import yaml
from datetime import datetime, timedelta


DEFAULT_ALARMS_FILE = "config/alarms.yaml"

# Path (relative to wherever the bot is launched from) for the trigger log.
# Kept at project root (not in config/) because it is runtime state,
# not user-editable configuration.
ALARM_HISTORY_FILE = "alarm_history.log"

# Persisted runtime state so a bot restart does NOT re-alert on an
# already-cooling-down alarm.  Stored at project root, JSON, atomic-write
# pattern (write to .tmp then os.replace).  Gitignored.
DEFAULT_ALARM_STATE_FILE = "alarm_state.json"


class Alarm:
    """A single alarm definition with cooldown tracking."""

    def __init__(self, name, variable, condition, threshold, interval_min,
                 enabled=True, destination=None):
        self.name = name
        self.variable = variable          # e.g., "P4", "T_MXC", "Flow"
        self.condition = condition        # ">" or "<"
        self.threshold = threshold        # numeric threshold
        self.interval_min = interval_min  # minutes between re-alerts
        self.enabled = enabled
        # destinations is ALWAYS a list of strings internally, even when the
        # YAML had a single string -- simpler for the routing code.
        # Each entry is one of:
        #   "operator_dm"    -- DM the bot_operator_user_id from keys.yaml
        #   "<key_name>"     -- name of a key in keys.yaml holding a channel ID
        #   "C0123..."       -- raw Slack channel ID (rare; prefer key_name)
        # Empty list -> fall back to the global alarm_channel in config.yaml.
        # The YAML layer accepts either a single string or a list; we
        # normalise to a list here.
        if destination is None:
            self.destinations = []
        elif isinstance(destination, str):
            self.destinations = [destination]
        else:
            self.destinations = [str(d) for d in destination]
        self.last_triggered = None        # datetime of last alert sent
        self.is_active = False            # currently in alarm state

    def check(self, value):
        """Check if the value triggers this alarm. Returns True if alarm condition is met."""
        if value is None or not self.enabled:
            return False
        if self.condition == ">":
            return value > self.threshold
        elif self.condition == "<":
            return value < self.threshold
        return False

    def should_alert(self):
        """Check if enough time has passed since last alert to send another."""
        if self.last_triggered is None:
            return True
        elapsed = datetime.now() - self.last_triggered
        return elapsed >= timedelta(minutes=self.interval_min)

    def mark_triggered(self):
        """Record that an alert was just sent."""
        self.last_triggered = datetime.now()
        self.is_active = True

    def clear(self):
        """Alarm condition is no longer met."""
        if self.is_active:
            self.is_active = False
            return True  # Was active, now cleared -- caller may want to notify
        return False

    def to_dict(self):
        """
        Serialise to the dict that YAML dump will write back to alarms.yaml.

        For readability, we store a single destination as a string, a list
        of multiple as a list.  Empty -> omit the field entirely.
        """
        d = {
            "name": self.name,
            "variable": self.variable,
            "condition": self.condition,
            "threshold": self.threshold,
            "interval_min": self.interval_min,
            "enabled": self.enabled,
        }
        if len(self.destinations) == 1:
            d["destination"] = self.destinations[0]
        elif len(self.destinations) > 1:
            d["destination"] = list(self.destinations)
        return d


class AlarmManager:
    """Manages a set of alarms, loads/saves from YAML."""

    def __init__(self, alarms_file=DEFAULT_ALARMS_FILE,
                 state_file=DEFAULT_ALARM_STATE_FILE):
        self.alarms_file = alarms_file
        self.state_file = state_file
        self.alarms = []
        # Remember the mtime (modification timestamp) of alarms.yaml so we
        # can cheaply detect "has the user edited this file?" without
        # re-parsing every poll.
        self._last_mtime = None
        self.load()
        # Apply persisted cooldown state from disk so a bot restart does
        # NOT re-alert a still-active alarm or lose an alarm's in-flight
        # cooldown.  This is the restart-case twin of the in-memory
        # transfer done in reload_if_changed().  See #2.
        self._apply_persisted_state()

    def _load_alarm_state(self):
        """Read {name: {last_triggered, is_active}} map from state_file.

        Missing or corrupt file is treated as empty -- the bot continues
        to run, just without restart-preserved cooldowns.
        """
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r") as f:
                raw = json.load(f)
            result = {}
            for name, v in raw.items():
                lt = v.get("last_triggered")
                ia = v.get("is_active", False)
                result[name] = {
                    "last_triggered": datetime.fromisoformat(lt) if lt else None,
                    "is_active": bool(ia),
                }
            return result
        except (OSError, json.JSONDecodeError, ValueError, TypeError,
                AttributeError) as e:
            print(f"[{datetime.now()}] {self.state_file} failed to load "
                  f"({type(e).__name__}: {e}); treating as empty.")
            return {}

    def _save_alarm_state(self):
        """Atomically write current cooldown/active state to state_file.

        Only current-alarm names are written -- renamed or deleted
        alarms get dropped automatically.
        """
        data = {}
        for a in self.alarms:
            data[a.name] = {
                "last_triggered": a.last_triggered.isoformat()
                                  if a.last_triggered else None,
                "is_active": a.is_active,
            }
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.state_file)
        except OSError as e:
            print(f"[{datetime.now()}] {self.state_file} save failed: {e}")

    def _apply_persisted_state(self):
        """Patch in-memory alarms with persisted last_triggered / is_active."""
        state = self._load_alarm_state()
        for a in self.alarms:
            s = state.get(a.name)
            if s is not None:
                a.last_triggered = s["last_triggered"]
                a.is_active = s["is_active"]

    def load(self):
        """Load alarms from YAML file.  Overwrites current in-memory state."""
        if not os.path.exists(self.alarms_file):
            self.alarms = []
            self._last_mtime = None
            return
        with open(self.alarms_file, "r") as f:
            data = yaml.safe_load(f) or {}
        self.alarms = []
        for entry in data.get("alarms", []):
            self.alarms.append(Alarm(
                name=entry["name"],
                variable=entry["variable"],
                condition=entry.get("condition", ">"),
                threshold=entry["threshold"],
                interval_min=entry.get("interval_min", 5),
                enabled=entry.get("enabled", True),
                destination=entry.get("destination"),   # None if absent -> global default
            ))
        # Record the mtime we just loaded so reload_if_changed() can detect edits.
        try:
            self._last_mtime = os.path.getmtime(self.alarms_file)
        except OSError:
            self._last_mtime = None

    def reload_if_changed(self):
        """
        Reload alarms.yaml if its mtime has advanced since the last load.

        Returns True if a reload actually happened, False otherwise.

        If the new file fails to parse (truncated mid-save, typo, etc.) we
        KEEP the last-good in-memory state rather than throwing an
        exception or wiping the alarms list.  The user sees a log line
        and can fix the YAML; the bot keeps running with the old alarms.

        Cooldown state (is_active, last_triggered) is PRESERVED across
        reloads by matching new alarms against old ones by name.  This
        avoids issue #2: without preservation, editing alarms.yaml
        while a sensor is past threshold causes a duplicate alert on
        the next tick because last_triggered resets to None.  Renaming
        an alarm deliberately resets its cooldown -- a rename is
        semantically a different alarm.
        """
        if not os.path.exists(self.alarms_file):
            return False
        try:
            mtime = os.path.getmtime(self.alarms_file)
        except OSError:
            return False
        if self._last_mtime is None or mtime > self._last_mtime:
            # Snapshot current state so we can roll back if the new file
            # fails to parse.
            prev_alarms = list(self.alarms)
            prev_mtime = self._last_mtime
            try:
                self.load()
                # Preserve cooldown + active state across reloads.
                # Matching on name means edits to threshold / condition
                # / destination / interval keep the cooldown going, so
                # a sensor that is currently past threshold does NOT
                # re-alert on every YAML save.  Rename resets state.
                prev_by_name = {a.name: a for a in prev_alarms}
                for new in self.alarms:
                    prev = prev_by_name.get(new.name)
                    if prev is not None:
                        new.last_triggered = prev.last_triggered
                        new.is_active = prev.is_active
                # Resync disk state so renamed/removed alarms don't
                # leave orphan entries behind.
                self._save_alarm_state()
                return True
            except Exception as e:
                print(f"[{datetime.now()}] alarms.yaml failed to reload "
                      f"({type(e).__name__}: {e}); keeping previous "
                      f"in-memory alarms.")
                self.alarms = prev_alarms
                self._last_mtime = prev_mtime
                return False
        return False

    def save(self):
        """Save current alarms to YAML file."""
        data = {"alarms": [a.to_dict() for a in self.alarms]}
        with open(self.alarms_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    def add_alarm(self, name, variable, condition, threshold, interval_min):
        """Add a new alarm and save."""
        alarm = Alarm(name, variable, condition, threshold, interval_min)
        self.alarms.append(alarm)
        self.save()
        return alarm

    def remove_alarm(self, name):
        """Remove an alarm by name and save."""
        self.alarms = [a for a in self.alarms if a.name != name]
        self.save()

    def check_all(self, live_snapshot):
        """
        Check all alarms against a live snapshot from FridgeReader.get_all_live().
        Returns list of (alarm, current_value) for alarms that should fire.
        """
        triggered = []
        cleared = []

        for alarm in self.alarms:
            if not alarm.enabled:
                continue

            # Look up the value from the snapshot
            sensor_data = live_snapshot.get(alarm.variable)
            if sensor_data is None:
                continue
            value = sensor_data.get("value")

            if alarm.check(value):
                # Alarm condition met
                if alarm.should_alert():
                    alarm.mark_triggered()
                    triggered.append((alarm, value))
            else:
                # Condition no longer met
                was_active = alarm.clear()
                if was_active:
                    cleared.append((alarm, value))

        # Persist cooldown + active state to disk whenever anything
        # changed this tick, so a bot restart does NOT lose the
        # cooldown or re-alert on an already-active alarm.  See #2.
        if triggered or cleared:
            self._save_alarm_state()

        return triggered, cleared

    def list_alarms(self):
        """Return a formatted string of all alarms, including their routing."""
        if not self.alarms:
            return "No alarms configured."
        lines = []
        for a in self.alarms:
            status = "ACTIVE" if a.is_active else ("ON" if a.enabled else "OFF")
            # Render destinations: "operator_dm, fluxonium_kids_channel" or
            # "(default)" if none specified.
            dest = ", ".join(a.destinations) if a.destinations else "(default)"
            # No leading whitespace: Slack markdown drops indent from the
            # first line but preserves it on subsequent lines, which made
            # alignment look off.  Keep each line left-aligned.
            lines.append(
                f"[{status}] {a.name}: {a.variable} {a.condition} "
                f"{a.threshold} (every {a.interval_min} min) -> {dest}"
            )
        return "\n".join(lines)

    def log_trigger(self, alarm, value, history_file=ALARM_HISTORY_FILE):
        """
        Append a single alarm-trigger event to the JSONL history file.

        Called from bot.check_alarms() every time an alarm actually fires
        (i.e. once per trigger, not once per poll -- cooldowns already
        dedupe this upstream).

        ``history_file`` is a parameter (not a hardcoded constant) so tests
        can redirect it to a temp path, but defaults to the canonical file.
        """
        # Build a small JSON-serialisable dict describing the trigger.
        # datetime.isoformat() gives us "2026-04-15T14:23:01.123456" which
        # is sortable as a string and parseable back via datetime.fromisoformat().
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "name": alarm.name,
            "variable": alarm.variable,
            "value": float(value) if value is not None else None,
            "threshold": alarm.threshold,
            "condition": alarm.condition,
        }
        # Open in append mode ("a") -- the file is created if missing.
        # json.dumps() does not add a newline, so we add "\n" to make this
        # one JSON object per line (the "JSONL" format).
        try:
            with open(history_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as e:
            # Never let a logging failure crash the alarm path -- just print.
            print(f"[{datetime.now()}] Could not append to {history_file}: {e}")


def read_recent_triggers(hours=24, history_file=ALARM_HISTORY_FILE):
    """
    Read ``history_file`` and return a dict ``{alarm_name: count}`` of how
    many times each alarm fired in the last ``hours`` hours.

    Used by the status widget to render "triggers in last 24h" per alarm.

    Safe on missing file (returns empty dict) and on malformed lines (skips
    them).  Does NOT raise -- the widget must stay responsive even if the
    log file is corrupt.
    """
    # If the bot has never triggered an alarm, the file simply does not exist.
    if not os.path.exists(history_file):
        return {}

    cutoff = datetime.now() - timedelta(hours=hours)
    counts = {}

    with open(history_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                # ISO timestamp -> datetime.  fromisoformat understands the
                # exact format we wrote with isoformat(timespec="seconds").
                ts = datetime.fromisoformat(event["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                # Skip any line that is not clean JSON or is missing "ts".
                continue
            if ts >= cutoff:
                name = event.get("name", "?")
                counts[name] = counts.get(name, 0) + 1

    return counts
