"""
BF4-bot Watchdog + Status Widget
================================

This script does two things at once:

1. **Shows a small always-on-top Tk window** on your Windows desktop.  Its
   visibility is your at-a-glance reminder that monitoring is running:

   * If the window is on your desktop -> the watchdog is alive.
   * If the window is NOT on your desktop -> something is very wrong
     (crash, killed process, logged out).  Go investigate.

2. **Sends Slack DMs on state change** (bot went offline / came back).
   This is the old watchdog behaviour, preserved.

Both tasks share a single process -- they do NOT need separate Task Scheduler
entries.  If this process dies, Task Scheduler's "Restart on failure" setting
brings it back within a minute and the window reappears.

Why a long-running process instead of the original "run every 5 min" cron?
-------------------------------------------------------------------------
The original watchdog.py was a one-shot script: wake up, read heartbeat, exit.
Cheap, but it has no UI.  A Tkinter window MUST own a running event loop, so
we flip the model: the script stays up forever, and inside its event loop we
poll the heartbeat file on a timer (every 2 seconds).  Same behaviour as before
from the user's perspective -- just embedded in a live UI.

Key Tkinter concepts used below
-------------------------------
* ``tk.Tk()``                -- creates the root window.
* ``root.attributes("-topmost", True)`` -- makes the window stay on top of
  other application windows.  This is the "can't miss it" property.
* Native Windows title bar is KEPT (we do NOT call overrideredirect) so the
  widget appears in the taskbar and Alt-Tab.  If the window drifts off-screen
  or hides behind another app, the taskbar entry is always clickable.
* ``root.after(ms, callback)`` -- Tkinter's single-threaded "run this later"
  primitive.  We use it to re-check the heartbeat every 2 seconds WITHOUT
  blocking the UI.  Do NOT use ``time.sleep`` or a background thread for
  updating Tk widgets -- Tk is not thread-safe.
* ``root.mainloop()``        -- starts the event loop.  Blocks until the window
  is destroyed.
"""

import json
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Path bookkeeping
# ---------------------------------------------------------------------------
# __file__ is the absolute path of this script.  Anchoring every file we read
# or write to this directory makes the widget work no matter what the current
# working directory is when Task Scheduler launches it.
HERE = os.path.dirname(os.path.abspath(__file__))

HEARTBEAT_FILE = os.path.join(HERE, "bot_heartbeat.txt")
LOG_FILE = os.path.join(HERE, "bf4bot.log")
POSITION_FILE = os.path.join(HERE, "widget_position.json")
# Presence of this file pauses the bot's autonomous work (alarm polling
# and scheduled reports).  Created/deleted by the widget's pause button.
# The bot still responds to DMs while paused.
PAUSE_FLAG_FILE = os.path.join(HERE, "bot_paused.flag")
# Cooperative shutdown: bot checks this at startup AND every poll tick.
# If it exists, bot exits with code 0 (clean exit = no scheduler restart).
SHUTDOWN_FLAG_FILE = os.path.join(HERE, "bot_shutdown.flag")
# Config files live under config/ after the migration.
ALARMS_FILE = os.path.join(HERE, "config", "alarms.yaml")

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------
POLL_INTERVAL_MS = 2000           # How often (ms) the widget refreshes status.
STALE_WARN_SECONDS = 60           # Heartbeat older than this -> yellow/STALE.
STALE_OFFLINE_SECONDS = 5 * 60    # Heartbeat older than this -> red/OFFLINE.

# Colour palette -- kept as constants so restyling is a one-line change.
COLOR_BG = "#1e1e1e"
COLOR_FG = "#f0f0f0"
COLOR_MUTED = "#9a9a9a"
COLOR_OK = "#4caf50"       # green
COLOR_WARN = "#ffb300"     # amber
COLOR_BAD = "#f44336"      # red
COLOR_BUTTON = "#2d2d2d"

# Project-local import -- we import AFTER defining paths so relative configs
# resolve correctly.  These are the helpers we added to bf4bot/alarms.py.
sys.path.insert(0, HERE)
from bf4bot.alarms import AlarmManager, read_recent_triggers  # noqa: E402
from bf4bot.slack_client import BF4SlackClient  # noqa: E402
from bf4bot.config import load_keys, get_operator_user_id  # noqa: E402
from bf4bot.alarm_editor import AlarmEditorWindow  # noqa: E402
from bf4bot.report_editor import ReportEditorWindow  # noqa: E402

# Each editor has its own config file with its own destination allowlist,
# so alarms and reports can route to different targets.
ALARM_CONFIG_FILE = os.path.join(HERE, "config", "alarm_config.yaml")
REPORT_CONFIG_FILE = os.path.join(HERE, "config", "report_config.yaml")
# Path to channels.yaml -- shared source of truth for Slack channel and
# user IDs.  Kept separate from keys.yaml so credentials never appear in
# the "edit destinations" flow.
CHANNELS_FILE = os.path.join(HERE, "config", "channels.yaml")
# Path to reports.yaml -- scheduled report definitions, managed by the
# Report editor window.
REPORTS_FILE = os.path.join(HERE, "config", "reports.yaml")


# ===========================================================================
# Helpers that are independent of the UI.  Kept as module-level functions so
# they are easy to unit-test without spinning up a Tk window.
# ===========================================================================

def read_heartbeat_age_seconds():
    """
    Return seconds since the last heartbeat was written, or ``None`` if the
    file does not exist yet (bot has never started).

    The heartbeat file contains a single ISO-8601 timestamp written by
    bot.py every 30s.  We parse it with datetime.fromisoformat.
    """
    if not os.path.exists(HEARTBEAT_FILE):
        return None
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            last_beat = datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        # Empty file or mid-write -- treat as "no data this tick".
        return None
    return (datetime.now() - last_beat).total_seconds()


def classify_state(age_seconds):
    """
    Map an age-in-seconds to one of two states: "ok" or "offline".

    ``age_seconds`` may be None (no heartbeat file) -- that maps to "offline".
    Simplified from three states (ok/stale/offline) to two -- the "stale"
    in-between was confusing and rarely useful in practice.
    """
    if age_seconds is None or age_seconds > STALE_OFFLINE_SECONDS:
        return "offline"
    return "ok"


def load_window_position():
    """
    Read the saved (x, y) from widget_position.json, or return None.

    Returns None (= use default corner) if:
      * File does not exist or is corrupt.
      * Saved coordinates would place the widget mostly off-screen.
        This catches: second monitor disconnected, RDP at a different
        resolution, widget dragged to an edge and partially hidden.

    The bounds check runs against the CURRENT screen size (which Tk
    reports via winfo_screenwidth/height once the root is created).
    Since we call this BEFORE root exists, we use a temporary hidden
    Tk instance just to query screen dimensions, then destroy it.
    """
    if not os.path.exists(POSITION_FILE):
        return None
    try:
        with open(POSITION_FILE, "r") as f:
            data = json.load(f)
        x, y = int(data["x"]), int(data["y"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None

    # Query current screen size via a throwaway Tk instance.
    # (The real root window hasn't been created yet.)
    try:
        _probe = tk.Tk()
        _probe.withdraw()   # hide immediately
        sw = _probe.winfo_screenwidth()
        sh = _probe.winfo_screenheight()
        _probe.destroy()
    except Exception:
        # If we can't probe, trust the saved position and hope for the best.
        return (x, y)

    # The widget must have at least MIN_VISIBLE pixels on-screen in both
    # axes.  If not, discard the saved position so _default_corner() puts
    # it somewhere safe.
    MIN_VISIBLE = 100   # px -- enough to see and grab with the mouse
    # Widget dimensions (approximate; exact values are set later, but
    # these are close enough for a bounds check).
    w, h = 300, 142
    x_visible = min(x + w, sw) - max(x, 0)
    y_visible = min(y + h, sh) - max(y, 0)
    if x_visible < MIN_VISIBLE or y_visible < MIN_VISIBLE:
        # Off-screen or mostly hidden -- discard.
        return None
    return (x, y)


def save_window_position(x, y):
    """Persist the current widget position so it reopens where the user left it."""
    try:
        with open(POSITION_FILE, "w") as f:
            json.dump({"x": int(x), "y": int(y)}, f)
    except OSError:
        # Not worth crashing the widget over -- position will just reset next boot.
        pass


def open_log_in_notepad():
    """
    Launch Notepad on the bot log file.  Uses subprocess.Popen so we do NOT
    wait for Notepad to close -- the widget keeps updating.

    If the log file does not exist yet (bot never ran headless), Notepad will
    offer to create it; we pre-create an empty file to avoid that prompt.
    """
    if not os.path.exists(LOG_FILE):
        try:
            open(LOG_FILE, "a").close()
        except OSError:
            pass
    # Popen returns immediately.  On Windows, "notepad" is on PATH.
    subprocess.Popen(["notepad", LOG_FILE])


# ===========================================================================
# The widget itself.
# ===========================================================================

class StatusWidget:
    """
    Encapsulates the Tk window and all of its state.

    One-class design (rather than many globals) makes it easy to reason about
    the widget's lifecycle: create instance -> .run() -> .shutdown().
    """

    def __init__(self):
        # --- Pre-UI setup: Slack client for DMs on state change ---------
        # We keep the Slack client alive for the life of the widget so we
        # don't pay the TLS handshake cost on every state transition.
        keys = load_keys()
        self.slack = BF4SlackClient(token=keys["slack_bot_token"])
        # Two-level operator lookup: channels.yaml's 'operator' nickname
        # points into dm_users which holds the actual Slack user ID.
        self.operator_id = get_operator_user_id(keys)
        if not self.operator_id:
            print(f"[{datetime.now()}] WARNING: no operator configured; "
                  f"offline DMs will be disabled.  Check channels.yaml.")
        # Resolve the operator's DM channel once up front.  conversations_open
        # is idempotent so calling it on startup is safe.
        try:
            resp = self.slack.client.conversations_open(users=[self.operator_id])
            self.dm_channel = resp["channel"]["id"]
        except Exception as e:
            # If Slack is unreachable at startup we still want the widget
            # to come up (it may come back later).  DM-on-change will just
            # silently no-op until Slack is reachable.
            print(f"[{datetime.now()}] Could not open DM channel: {e}")
            self.dm_channel = None

        # AlarmManager gives us the list of configured alarms (names, states).
        # We reload this each tick so edits to alarms.yaml are picked up
        # without restarting the widget.  For now one instance is enough.
        self.alarm_mgr = AlarmManager(alarms_file=ALARMS_FILE)

        # Track what state we *last* reported so we only DM on transitions
        # (ok -> offline, offline -> ok), not every tick.
        self._last_state = None

        # Debounce transitions: require the new state to persist for
        # CONFIRM_TICKS consecutive ticks before we actually flip _last_state
        # and DM.  This absorbs single-tick blips -- e.g. when the watchdog
        # catches the heartbeat file mid-write and momentarily reads it as
        # missing.  At POLL_INTERVAL_MS = 2000ms, 2 ticks = ~4 seconds.
        self._pending_state = None
        self._pending_count = 0

        # When the user clicks STOP BOT, we set this flag so the widget
        # IMMEDIATELY shows OFFLINE + START BOT -- without waiting 5 min
        # for the heartbeat to expire.  Cleared when user clicks START.
        self._bot_stopped_by_user = False

        # STOP is cooperative: we write a flag, the bot exits itself at
        # its next poll tick (up to ~30s later).  While we wait, the
        # button is gray "STOPPING... please wait" and disabled, so the
        # user cannot race the shutdown by clicking START too early.
        # See GitHub issue #18.
        self._stop_pending = False
        self._stop_started_at = None
        # Max wait before giving up on a bot that won't exit.  Anything
        # longer than one poll interval + generous margin probably means
        # the bot is stuck and the user should be able to click again.
        self._stop_timeout_seconds = 90
        # Heartbeat age that means "bot is definitely dead": one poll
        # interval plus a 15s safety margin.
        self._stop_confirm_age_seconds = 45

        # Symmetric to STOP: after clicking START we wait until a fresh
        # heartbeat proves the new bot process is alive.  During that
        # window the button is also gray "please wait" so the user can't
        # race the launch by clicking STOP too early.
        self._start_pending = False
        self._start_started_at = None
        self._start_timeout_seconds = 60
        # Heartbeat age that means "bot is definitely alive": < 30s
        # (one poll interval) means it ticked recently.
        self._start_confirm_age_seconds = 30

        # --- Build the Tk root window ------------------------------------
        self.root = tk.Tk()
        # Title shows in the Windows taskbar entry and Alt-Tab list.
        self.root.title("BF4 Bot Watchdog")
        # Custom taskbar icon (shell32.dll icon #41, extracted via
        # scripts/extract_shell32_icons.ps1).  Replaces Tk's default
        # feather logo.  Wrapped in try/except so a missing .ico does
        # not crash the widget -- we just fall back to the feather.
        try:
            self.root.iconbitmap(os.path.join(HERE, "assets", "tree.ico"))
        except tk.TclError as e:
            print(f"[{datetime.now()}] Could not load tree.ico: {e}")
        # Stay on top of ordinary windows.
        self.root.attributes("-topmost", True)
        # Give it a dark background so its label colours render cleanly.
        self.root.configure(bg=COLOR_BG)

        # --- Size & position -------------------------------------------
        # Height for pause+stop row; width for two side-by-side buttons.
        self.width, self.height = 300, 142
        pos = load_window_position() or self._default_corner()
        # geometry string is "WIDTHxHEIGHT+X+Y" (literal '+' signs).
        self.root.geometry(f"{self.width}x{self.height}+{pos[0]}+{pos[1]}")

        # --- Build the widget contents ----------------------------------
        self._build_ui()

        # Persist position whenever the user moves the window via the
        # native title bar.  <Configure> fires on move AND resize; we
        # filter to just "position changed" by comparing to the last
        # saved coords.
        self._last_saved_pos = (self.root.winfo_x(), self.root.winfo_y())
        self.root.bind("<Configure>", self._on_configure)

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------

    def _default_corner(self):
        """Bottom-right corner of the primary screen, with a little margin."""
        # winfo_screenwidth/height queries the monitor Tk was placed on.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return (sw - self.width - 24, sh - self.height - 60)

    def _build_ui(self):
        """Create all the labels and buttons exactly once, during __init__."""
        # A single padded frame holds every child widget.  Using a Frame
        # instead of putting widgets directly on root makes it easy to
        # restyle the card (padding, border) in one place.
        self.frame = tk.Frame(self.root, bg=COLOR_BG, padx=10, pady=8)
        self.frame.pack(fill="both", expand=True)

        # Fonts: default Tk fonts are tiny; we set explicit Segoe UI for a
        # native Windows feel.
        title_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=9)

        # Row 1: [dot]  BF4 Bot   STATE
        # We pack the three pieces horizontally inside a sub-frame so they
        # align on one line.
        row1 = tk.Frame(self.frame, bg=COLOR_BG)
        row1.pack(fill="x")

        # The status "dot" is a small Canvas with a filled oval.  We use a
        # Canvas (not a Unicode bullet) so the source file stays pure ASCII
        # and there is no chance of a cp1252 crash if this text ever ends
        # up in a print().  We keep a reference to both the canvas AND the
        # oval item so we can recolour it each tick via itemconfig().
        self.dot_canvas = tk.Canvas(row1, width=14, height=14,
                                    bg=COLOR_BG, highlightthickness=0)
        # create_oval returns an item ID we can later recolour.  The box
        # (2,2)-(12,12) inside a 14x14 canvas gives a ~10px dot with a 2px
        # inset so it does not touch the edges.
        self.dot_item = self.dot_canvas.create_oval(
            2, 2, 12, 12, fill=COLOR_MUTED, outline="")
        self.dot_canvas.pack(side="left", padx=(0, 4))

        self.title_label = tk.Label(row1, text="  BF4 Bot", fg=COLOR_FG,
                                    bg=COLOR_BG, font=title_font)
        self.title_label.pack(side="left")

        # State text sits on the right side of the same row.
        self.state_label = tk.Label(row1, text="...", fg=COLOR_MUTED,
                                    bg=COLOR_BG, font=title_font)
        self.state_label.pack(side="right")

        # Row 2: "Heartbeat: 14s ago"
        self.heartbeat_label = tk.Label(self.frame, text="Heartbeat: --",
                                        fg=COLOR_MUTED, bg=COLOR_BG,
                                        font=body_font, anchor="w")
        self.heartbeat_label.pack(fill="x", pady=(4, 0))

        # Row 3: "Alarms: 4 armed"  -- hover to see breakdown.
        self.alarm_label = tk.Label(self.frame, text="Alarms: --",
                                    fg=COLOR_MUTED, bg=COLOR_BG,
                                    font=body_font, anchor="w")
        self.alarm_label.pack(fill="x", pady=(2, 0))
        # Hovering over the alarms row shows a tooltip listing each alarm
        # and its 24h trigger count.  We implement the tooltip ourselves
        # (Tk has no native one) -- see _show_alarm_tooltip.
        self._tooltip = None
        self.alarm_label.bind("<Enter>", self._show_alarm_tooltip)
        self.alarm_label.bind("<Leave>", self._hide_alarm_tooltip)

        # Row 4: buttons -- [logs] on the left, [X] on the right.
        row4 = tk.Frame(self.frame, bg=COLOR_BG)
        row4.pack(fill="x", pady=(6, 0))

        logs_btn = tk.Button(row4, text="logs", command=open_log_in_notepad,
                             bg=COLOR_BUTTON, fg=COLOR_FG, activebackground="#3a3a3a",
                             activeforeground=COLOR_FG, relief="flat", borderwidth=0,
                             padx=8, pady=1, font=body_font, cursor="hand2")
        logs_btn.pack(side="left")

        # "alarms" opens the alarm editor (a Toplevel window).  Only one
        # editor window is allowed at a time -- clicking the button again
        # while one is already open does nothing.
        alarms_btn = tk.Button(row4, text="alarms", command=self._on_alarms,
                               bg=COLOR_BUTTON, fg=COLOR_FG,
                               activebackground="#3a3a3a",
                               activeforeground=COLOR_FG,
                               relief="flat", borderwidth=0,
                               padx=8, pady=1, font=body_font, cursor="hand2")
        alarms_btn.pack(side="left", padx=(4, 0))

        # "reports" opens the Report editor, analogous to the alarm editor.
        reports_btn = tk.Button(row4, text="reports", command=self._on_reports,
                                bg=COLOR_BUTTON, fg=COLOR_FG,
                                activebackground="#3a3a3a",
                                activeforeground=COLOR_FG,
                                relief="flat", borderwidth=0,
                                padx=8, pady=1, font=body_font, cursor="hand2")
        reports_btn.pack(side="left", padx=(4, 0))

        close_btn = tk.Button(row4, text="X", command=self.shutdown,
                              bg=COLOR_BUTTON, fg=COLOR_FG, activebackground="#3a3a3a",
                              activeforeground=COLOR_FG, relief="flat", borderwidth=0,
                              padx=8, pady=1, font=body_font, cursor="hand2")
        close_btn.pack(side="right")

        # --- Control row (row5): PAUSE + STOP/START side by side ----------
        # Two distinct actions:
        #   PAUSE  = bot stays alive but skips alarm polling + reports.
        #            DMs still work.  Good for maintenance windows.
        #   STOP   = actually kill the bot process.  DMs stop working.
        #            START re-launches it via pythonw.
        row5 = tk.Frame(self.frame, bg=COLOR_BG)
        row5.pack(fill="x", pady=(6, 0))
        ctrl_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        # Smaller font for the gray "please wait" button shown during
        # STOPPING and STARTING windows.  Stored on self so the helper
        # that configures the button (_set_waiting_ui) can reach it.
        self._waiting_font = tkfont.Font(
            family="Segoe UI", size=9, weight="normal")
        self._ctrl_font = ctrl_font

        self.pause_btn = tk.Button(row5, text="PAUSE",
                                   command=self._on_toggle_pause,
                                   bg=COLOR_BUTTON, fg=COLOR_FG,
                                   activebackground="#3a3a3a",
                                   activeforeground=COLOR_FG,
                                   relief="flat", borderwidth=0,
                                   padx=12, pady=4, font=ctrl_font,
                                   cursor="hand2")
        self.pause_btn.pack(side="left", fill="x", expand=True)

        self.bot_ctrl_btn = tk.Button(row5, text="STOP BOT",
                                      command=self._on_toggle_bot,
                                      bg=COLOR_BUTTON, fg=COLOR_FG,
                                      activebackground="#3a3a3a",
                                      activeforeground=COLOR_FG,
                                      relief="flat", borderwidth=0,
                                      padx=12, pady=4, font=ctrl_font,
                                      cursor="hand2")
        self.bot_ctrl_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

    # -----------------------------------------------------------------
    # Window move -- persist position after user drags via title bar.
    # -----------------------------------------------------------------

    def _on_configure(self, event):
        """Save new position when <Configure> reports a move.

        <Configure> fires for ANY geometry change (move, resize, child
        widget layout).  We only care about root-window moves, so we
        filter by widget identity and by whether (x, y) actually changed.
        """
        if event.widget is not self.root:
            return
        pos = (self.root.winfo_x(), self.root.winfo_y())
        if pos != self._last_saved_pos:
            self._last_saved_pos = pos
            save_window_position(*pos)

    # -----------------------------------------------------------------
    # Tooltip -- a Toplevel window with no decoration, shown on hover.
    # -----------------------------------------------------------------

    def _show_alarm_tooltip(self, _event):
        """On hover over the 'Alarms: N armed' row, show a per-alarm breakdown."""
        # Re-read the alarms file in case the user edited it live.
        self.alarm_mgr.load()
        # Count how many times each alarm has fired in the last 24h.
        counts_24h = read_recent_triggers(hours=24)

        if not self.alarm_mgr.alarms:
            body = "No alarms configured."
        else:
            lines = []
            for a in self.alarm_mgr.alarms:
                tag = "on" if a.enabled else "off"
                n = counts_24h.get(a.name, 0)
                lines.append(f"{a.name:<12} [{tag}]  {n}/24h")
            body = "\n".join(lines)

        # Toplevel is a child window -- we strip its decoration and place
        # it just below the alarm row.
        self._tooltip = tk.Toplevel(self.root)
        self._tooltip.overrideredirect(True)
        self._tooltip.attributes("-topmost", True)
        x = self.root.winfo_x() + 10
        y = self.root.winfo_y() + self.alarm_label.winfo_y() + 18
        self._tooltip.geometry(f"+{x}+{y}")
        tk.Label(self._tooltip, text=body, justify="left",
                 bg="#2a2a2a", fg=COLOR_FG, padx=8, pady=4,
                 font=tkfont.Font(family="Consolas", size=9)).pack()

    def _hide_alarm_tooltip(self, _event):
        """Destroy the tooltip when the pointer leaves the alarms row."""
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    # -----------------------------------------------------------------
    # The periodic tick -- runs every POLL_INTERVAL_MS milliseconds.
    # -----------------------------------------------------------------

    def _tick(self):
        """
        Refresh the UI and fire Slack DMs on state transitions.

        Scheduled recursively via root.after() -- at the end of this method
        we schedule the next call, so the loop continues until the widget
        is destroyed.
        """
        try:
            age = read_heartbeat_age_seconds()
            state = classify_state(age)
            paused = os.path.exists(PAUSE_FLAG_FILE)

            # Step A: let pending STOP/START states maybe clear themselves
            # this tick, based on heartbeat evidence.  These helpers only
            # flip flags -- they do NOT touch the UI; rendering below
            # handles the resulting state in one place.
            if self._stop_pending:
                self._check_stop_progress(age)
            if self._start_pending:
                self._check_start_progress(age)

            # Pause button is independent of STOP/START and always reflects
            # the pause flag.
            if paused:
                self.pause_btn.config(text="RESUME",
                                      bg=COLOR_WARN, fg="#111")
            else:
                self.pause_btn.config(text="PAUSE",
                                      bg=COLOR_BUTTON, fg=COLOR_FG)

            # Step B: render dot + state label + bot button.  Pending
            # states (STOPPING / STARTING) take priority and show amber;
            # otherwise use the normal ok / paused / offline rendering.
            if self._stop_pending:
                self.dot_canvas.itemconfig(self.dot_item, fill=COLOR_WARN)
                self.state_label.config(text="STOPPING...", fg=COLOR_WARN)
                # Button was set by _set_waiting_ui(); leave it alone.
            elif self._start_pending:
                self.dot_canvas.itemconfig(self.dot_item, fill=COLOR_WARN)
                self.state_label.config(text="STARTING...", fg=COLOR_WARN)
                # Button was set by _set_waiting_ui(); leave it alone.
            else:
                # Shutdown flag is the authoritative source of truth:
                # if it exists, the bot is intentionally stopped --
                # even if the heartbeat file still has a recent
                # timestamp from just before the bot exited.  This
                # covers "widget was restarted after a STOP": the
                # widget loses its in-memory _bot_stopped_by_user flag
                # but the shutdown.flag file persists on disk.  Without
                # this check, a fresh widget would misread the stale
                # heartbeat as "bot alive" and show RUNNING.
                #
                # Conversely, if the flag is gone but we still think
                # the bot is stopped, someone restarted it externally
                # (.bat, bash, Task Scheduler) -- clear our override.
                if os.path.exists(SHUTDOWN_FLAG_FILE):
                    state = "offline"
                    self._bot_stopped_by_user = True
                elif self._bot_stopped_by_user:
                    self._bot_stopped_by_user = False

                if paused and state == "ok":
                    self.dot_canvas.itemconfig(self.dot_item, fill=COLOR_WARN)
                    self.state_label.config(text="PAUSED", fg=COLOR_WARN)
                elif state == "ok":
                    self.dot_canvas.itemconfig(self.dot_item, fill=COLOR_OK)
                    self.state_label.config(text="RUNNING", fg=COLOR_OK)
                else:
                    self.dot_canvas.itemconfig(self.dot_item, fill=COLOR_BAD)
                    self.state_label.config(text="OFFLINE", fg=COLOR_BAD)

                if state == "ok":
                    self.bot_ctrl_btn.config(text="STOP BOT",
                                             bg=COLOR_BAD, fg="#fff",
                                             state="normal",
                                             font=self._ctrl_font)
                else:
                    self.bot_ctrl_btn.config(text="START BOT",
                                             bg=COLOR_OK, fg="#111",
                                             state="normal",
                                             font=self._ctrl_font)

            # Heartbeat line -- human-readable age.
            if age is None:
                self.heartbeat_label.config(text="Heartbeat: never")
            else:
                self.heartbeat_label.config(text=f"Heartbeat: {_fmt_age(age)} ago")

            # Alarm summary (detail is in the hover tooltip).
            self.alarm_mgr.load()
            n_armed = sum(1 for a in self.alarm_mgr.alarms if a.enabled)
            n_total = len(self.alarm_mgr.alarms)
            self.alarm_label.config(text=f"Alarms: {n_armed}/{n_total} armed")

            # DM the operator on state transitions (ok <-> offline).
            # We send on entering "offline" and on returning to "ok".
            # Ignore "stale" as a transition trigger -- it is noisy.
            self._maybe_send_transition_dm(state, age)
        except Exception as e:
            # A thrown exception here would stop the after() chain and
            # freeze the widget.  Log and keep going.
            print(f"[{datetime.now()}] Widget tick error: {e}")

        # Schedule ourselves again.
        self.root.after(POLL_INTERVAL_MS, self._tick)

    # Require this many consecutive ticks in the new state before we treat
    # it as a real transition and DM.  At POLL_INTERVAL_MS = 2000ms, 2 ticks
    # is ~4 seconds -- enough to absorb single-tick blips (mid-write reads
    # of the heartbeat, transient I/O hiccups) without delaying real outage
    # detection noticeably.
    CONFIRM_TICKS = 2

    def _maybe_send_transition_dm(self, state, age):
        """Send a Slack DM only when the health state flips ok <-> offline.

        Transitions must be confirmed for CONFIRM_TICKS consecutive ticks
        before we DM; otherwise a single mid-write read of the heartbeat
        file (which returns age=None -> "offline") would falsely alert.
        """
        if self.dm_channel is None:
            return
        prev = self._last_state
        # First tick: set baseline, do not DM.
        if prev is None:
            self._last_state = state
            self._pending_state = None
            self._pending_count = 0
            return

        # State matches current baseline -- clear any pending flip.
        if state == prev:
            self._pending_state = None
            self._pending_count = 0
            return

        # State differs from baseline -- track how long it has been stable.
        if state == self._pending_state:
            self._pending_count += 1
        else:
            self._pending_state = state
            self._pending_count = 1

        # Wait for confirmation before firing a DM.
        if self._pending_count < self.CONFIRM_TICKS:
            return

        # Confirmed transition.
        try:
            # All watchdog-originated DMs are prefixed with "[Watchdog]" so
            # you can tell them apart from the bot's own startup messages.
            if prev != "offline" and state == "offline":
                age_str = "never" if age is None else f"{age/60:.0f} min ago"
                msg = (f":warning: *[Watchdog]* BF4-bot appears to be OFFLINE.\n"
                       f"Last heartbeat: {age_str}\n"
                       f"Check the bot process on the fridge PC.")
                self.slack.send_message(self.dm_channel, msg)
                print(f"[{datetime.now()}] Sent OFFLINE DM")
            elif prev == "offline" and state == "ok":
                self.slack.send_message(
                    self.dm_channel,
                    ":white_check_mark: *[Watchdog]* BF4-bot is back online.")
                print(f"[{datetime.now()}] Sent BACK-ONLINE DM")
        except Exception as e:
            # Slack hiccup -- do not crash.  We'll try again next transition.
            print(f"[{datetime.now()}] Could not send transition DM: {e}")

        self._last_state = state
        self._pending_state = None
        self._pending_count = 0

    # -----------------------------------------------------------------
    # Pause/resume -- toggles a flag file that the bot's poll loop reads.
    # -----------------------------------------------------------------

    def _on_toggle_pause(self):
        """Create or delete the pause flag.  Bot reacts within ~30s."""
        if os.path.exists(PAUSE_FLAG_FILE):
            try:
                os.remove(PAUSE_FLAG_FILE)
                print(f"[{datetime.now()}] Pause flag removed -- bot will resume.")
            except OSError as e:
                print(f"[{datetime.now()}] Could not remove pause flag: {e}")
        else:
            try:
                with open(PAUSE_FLAG_FILE, "w") as f:
                    f.write(datetime.now().isoformat())
                print(f"[{datetime.now()}] Pause flag created -- bot will pause.")
            except OSError as e:
                print(f"[{datetime.now()}] Could not create pause flag: {e}")

    # -----------------------------------------------------------------
    # Stop / Start bot -- actually kill or launch the run_bot.py process.
    # -----------------------------------------------------------------

    def _is_bot_running(self):
        """Check heartbeat to decide if bot is alive."""
        age = read_heartbeat_age_seconds()
        return age is not None and age < STALE_OFFLINE_SECONDS

    # On Windows, subprocess calls to wmic/schtasks briefly flash a cmd
    # window unless we suppress it.  CREATE_NO_WINDOW prevents that.
    _NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW

    def _process_exists(self, script_name):
        """
        Check if any pythonw.exe process with ``script_name`` in its
        command line is currently running.  Uses wmic; returns True/False.
        """
        import subprocess
        try:
            out = subprocess.check_output(
                ["wmic", "process", "where",
                 f"name='pythonw.exe' and commandline like '%{script_name}%'",
                 "get", "processid"],
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=self._NO_WINDOW,
            )
            # wmic always prints a header line; if there's a digit on any
            # other line, a matching process exists.
            for line in out.strip().splitlines()[1:]:
                if line.strip().isdigit():
                    return True
        except Exception:
            pass
        return False

    def _on_toggle_bot(self):
        """
        STOP or START the main bot.

        STOP means truly stop:
          1. Kill the run_bot.py pythonw processes.
          2. DISABLE the Task Scheduler task so it does NOT auto-restart.

        START means truly start:
          1. RE-ENABLE the Task Scheduler task.
          2. Launch the bot via schtasks /Run (not raw pythonw, so Task
             Scheduler tracks it and can restart on future crashes).
        """
        import subprocess
        if not self._bot_stopped_by_user:
            # --- STOP ---
            try:
                # Write a shutdown flag file.  The bot checks this:
                #   * At startup (before heavy imports) -- exits in <1s
                #   * Every poll tick (~30s) -- exits mid-run
                # Exit code 0 = clean exit = scheduler does NOT restart.
                # Even if the scheduler wins a race and restarts the bot,
                # the bot immediately finds the flag and exits again.
                with open(SHUTDOWN_FLAG_FILE, "w") as f:
                    f.write(datetime.now().isoformat())
                # Also disable the scheduler as belt-and-suspenders.
                subprocess.call(
                    ["schtasks", "/Change", "/TN", "BF4Bot-Main", "/DISABLE"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=self._NO_WINDOW,
                )
                print(f"[{datetime.now()}] Shutdown flag written + scheduler disabled.")
            except Exception as e:
                print(f"[{datetime.now()}] Could not stop bot: {e}")
            # Enter the STOPPING state.  The bot may still be alive for
            # up to one poll interval (~30s); during that window the
            # button is disabled and shows "STOPPING... please wait" so
            # the user cannot race a fast re-click and leave the flag
            # in a deleted state before the bot has checked it.  _tick
            # will promote us to the final "stopped" state once the
            # heartbeat confirms the bot has actually exited.
            self._stop_pending = True
            self._stop_started_at = datetime.now()
            self._set_waiting_ui()
        else:
            # --- START ---
            try:
                if os.path.exists(SHUTDOWN_FLAG_FILE):
                    os.remove(SHUTDOWN_FLAG_FILE)
                subprocess.call(
                    ["schtasks", "/Change", "/TN", "BF4Bot-Main", "/ENABLE"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=self._NO_WINDOW,
                )
                subprocess.call(
                    ["schtasks", "/Run", "/TN", "BF4Bot-Main"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=self._NO_WINDOW,
                )
                print(f"[{datetime.now()}] Shutdown flag removed + scheduler enabled + bot started.")
            except Exception as e:
                print(f"[{datetime.now()}] Could not start bot: {e}")
            # Enter the STARTING state.  The fresh bot process takes a
            # few seconds to launch and write its first heartbeat; during
            # that window the button is gray "please wait" and disabled,
            # mirroring STOPPING.  _check_start_progress() will flip us
            # to the final running state once the heartbeat is fresh.
            self._bot_stopped_by_user = False
            self._stop_pending = False
            self._stop_started_at = None
            self._start_pending = True
            self._start_started_at = datetime.now()
            self._set_waiting_ui()

    def _set_waiting_ui(self):
        """Render the gray 'please wait' button while a STOP or START is
        in flight.  The button is disabled so the user cannot click
        back-to-back (which would race the cooperative shutdown flag or
        the schtasks launch).  The dot + state label are rendered by
        _tick based on _stop_pending / _start_pending -- this helper
        only touches the button.
        """
        self.bot_ctrl_btn.config(text="please wait",
                                 bg=COLOR_BUTTON, fg="#888",
                                 state="disabled",
                                 font=self._waiting_font)

    def _check_stop_progress(self, age):
        """Called from _tick while _stop_pending is True.

        Clears the pending flag when the heartbeat is old enough to prove
        the bot has exited, OR when we have waited long enough to suspect
        the bot is stuck and the user should be allowed to try again.
        Does NOT touch the button or labels -- _tick renders them based
        on the flags this method leaves behind.
        """
        now = datetime.now()
        elapsed = (now - self._stop_started_at).total_seconds()
        bot_is_dead = (age is None) or (age >= self._stop_confirm_age_seconds)
        timed_out = elapsed >= self._stop_timeout_seconds
        if bot_is_dead or timed_out:
            if timed_out and not bot_is_dead:
                print(f"[{now}] STOP timed out after {elapsed:.0f}s -- "
                      f"bot still heart-beating (age={age:.0f}s).  "
                      f"Reverting UI so the user can try again.")
            self._stop_pending = False
            self._stop_started_at = None
            self._bot_stopped_by_user = True

    def _check_start_progress(self, age):
        """Called from _tick while _start_pending is True.

        Clears the pending flag when the heartbeat is fresh enough to
        prove the new bot process is alive, OR when we have waited long
        enough that the launch probably failed and the user should be
        allowed to try again.  Does NOT touch the button or labels.
        """
        now = datetime.now()
        elapsed = (now - self._start_started_at).total_seconds()
        bot_is_alive = (age is not None) and (age < self._start_confirm_age_seconds)
        timed_out = elapsed >= self._start_timeout_seconds
        if bot_is_alive:
            self._start_pending = False
            self._start_started_at = None
            # _bot_stopped_by_user is already False (set in START branch)
            # so _tick will render state="ok" -> red STOP BOT button.
        elif timed_out:
            age_desc = "never" if age is None else f"{age:.0f}s"
            print(f"[{now}] START timed out after {elapsed:.0f}s -- "
                  f"heartbeat age is {age_desc}.  Reverting UI so the "
                  f"user can try again.")
            self._start_pending = False
            self._start_started_at = None
            self._bot_stopped_by_user = True

    # -----------------------------------------------------------------
    # Alarm editor (opens a Toplevel via alarm_editor module)
    # -----------------------------------------------------------------

    def _on_alarms(self):
        """
        Open the alarm editor.  Re-opens on every click unless one is
        already up -- we keep a weak reference in self._editor to avoid
        spawning duplicates.
        """
        # If an editor is already open, raise it to the top rather than
        # spawning a second one.  winfo_exists() is Tk's built-in way to
        # ask "is this widget still alive?" after the user may have closed
        # it via the X.
        existing = getattr(self, "_editor", None)
        if existing is not None and existing.top.winfo_exists():
            existing.top.lift()
            existing.top.focus_force()
            return
        self._editor = AlarmEditorWindow(
            parent=self.root,
            alarms_path=ALARMS_FILE,
            config_path=ALARM_CONFIG_FILE,
            channels_path=CHANNELS_FILE,
        )

    def _on_reports(self):
        """Open the Report editor (or raise an existing one)."""
        existing = getattr(self, "_report_editor", None)
        if existing is not None and existing.top.winfo_exists():
            existing.top.lift()
            existing.top.focus_force()
            return
        self._report_editor = ReportEditorWindow(
            parent=self.root,
            reports_path=REPORTS_FILE,
            report_config_path=REPORT_CONFIG_FILE,
            channels_path=CHANNELS_FILE,
        )

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def run(self):
        """Start the Tk event loop.  Blocks until the window is destroyed."""
        # Kick off the first tick immediately.
        self._tick()
        # mainloop() does not return until root.destroy() is called.
        self.root.mainloop()

    def shutdown(self):
        """User clicked [X]: close the widget cleanly."""
        # Persist final position just in case a drag happened since last save.
        try:
            save_window_position(self.root.winfo_x(), self.root.winfo_y())
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Small formatting helper used by the heartbeat row.
# ---------------------------------------------------------------------------

def _fmt_age(seconds):
    """Turn a seconds-since-beat float into a compact string: '14s', '3m', '2h'."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def _another_watchdog_running():
    """Check if another watchdog.py pythonw process is already running.

    We use wmic to count processes whose command line contains "watchdog.py".
    Our OWN process will match, so we check for MORE THAN TWO matches
    (one launcher stub + one real interpreter = our own pair).
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "name='pythonw.exe' and commandline like '%watchdog.py%'",
             "get", "processid"],
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        pids = [line.strip() for line in out.strip().splitlines()[1:]
                if line.strip().isdigit()]
        # Our own process pair = 2 PIDs (launcher stub + real interpreter).
        # If there are more than 2, another watchdog is already up.
        return len(pids) > 2
    except Exception:
        return False


if __name__ == "__main__":
    # Install the rotating log shim (same helper run_bot.py uses).  It
    # replaces the old raw ``open(..., "a")`` redirect so watchdog.log
    # no longer grows forever -- issue #12.  Shim is a no-op under a
    # real terminal so ``python watchdog.py`` in dev still prints live.
    from bf4bot.logging_setup import install_log_shim
    install_log_shim(os.path.join(HERE, "watchdog.log"))
    print(f"\n[{datetime.now()}] --- watchdog starting (headless) ---")

    # Duplicate guard: if another watchdog is already running, exit quietly
    # rather than spawning a second Tk window + a second Slack DM sender.
    if _another_watchdog_running():
        print(f"[{datetime.now()}] Another watchdog is already running. Exiting.")
        sys.exit(0)

    StatusWidget().run()
