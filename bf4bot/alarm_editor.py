"""
Alarm editor GUI -- a Tk popup window attached to the watchdog widget.

Design
------
Two Tk Toplevel windows that the watchdog can open:

  * AlarmEditorWindow -- a table-like list of all alarms from alarms.yaml,
    one row per alarm, with [edit] / [delete] buttons per row and an
    [+ Add alarm] button below.

  * AlarmEditDialog   -- a form for editing a single alarm (or adding a
    new one).  Destination is a multi-select listbox populated from the
    ``alarm_destinations`` allowlist in config.yaml.

Data flow
---------
All changes happen through an AlarmManager instance that is loaded in
memory by the editor:

    user clicks save  ->  self.alarm_mgr.alarms mutated in memory
                      ->  self.alarm_mgr.save()      (writes alarms.yaml)
                      ->  main bot picks up change within ~30s via
                          AlarmManager.reload_if_changed() in its poll loop.

This means the widget and the bot are NEVER coupled at runtime -- they
only share the YAML file on disk.  If the bot is down, you can still
edit alarms; when the bot comes back up, it reloads.

Heavy commenting throughout is intentional -- this module is meant to be
read as a Tk learning reference as well as a working editor.
"""

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from bf4bot.alarms import Alarm, AlarmManager
from bf4bot.config import load_config

# Re-using the same palette the watchdog uses so everything looks of a piece.
COLOR_BG = "#1e1e1e"
COLOR_FG = "#f0f0f0"
COLOR_MUTED = "#9a9a9a"
COLOR_OK = "#4caf50"
COLOR_WARN = "#ffb300"
COLOR_BAD = "#f44336"
COLOR_BUTTON = "#2d2d2d"
COLOR_BUTTON_HOVER = "#3a3a3a"

# Valid sensor names the user can pick from in the Variable dropdown.
# Kept as a fixed list because the reader exposes these and no others --
# letting the user type a typo like "T_MCX" would silently never trigger.
VALID_VARIABLES = [
    "T_MXC", "T_Still", "T_4K", "T_50K",
    "T_CompressorWaterOut", "T_CompressorHelium",
    "P1", "P2", "P3", "P4", "P5", "P6",
    "Flow",
    "Compressor", "PulseTube",
    "Turbo1", "Turbo2",
    "Turbo1_ovtemp", "Turbo2_ovtemp",
]

# Units the reader reports each variable in.  Verified against reader.py's
# get_all_live() implementation.  Used only for display -- the threshold
# field in YAML stays a pure number, units are never stored there.
VARIABLE_UNITS = {
    "T_MXC": "mK", "T_Still": "mK",       # reader converts K * 1e3
    "T_4K": "K", "T_50K": "K",
    "T_CompressorWaterOut": "C",          # bflegacy cpatempwo, already in C
    "T_CompressorHelium": "C",            # bflegacy cpatemph, already in C
    "P1": "mbar", "P2": "mbar", "P3": "mbar",
    "P4": "mbar", "P5": "mbar", "P6": "mbar",   # reader converts bar * 1e3
    "Flow": "mmol/s",
    "Compressor": "",                     # boolean -- no unit
    "PulseTube": "",
    "Turbo1": "", "Turbo2": "",
    "Turbo1_ovtemp": "", "Turbo2_ovtemp": "",
}

# Variables whose value is boolean (True = running / on, False = off).
# The edit dialog shows an "Alert when: OFF / ON" dropdown for these
# instead of a numeric threshold field.  Internally we still store a
# (condition, threshold) pair so the alarm engine is unchanged:
#   "Alert when OFF"  ->  condition="<",  threshold=1.0
#                         (fires when value=0, which is < 1)
#   "Alert when ON"   ->  condition=">",  threshold=0.0
#                         (fires when value=1, which is > 0)
BOOLEAN_VARIABLES = {
    "Compressor", "PulseTube",
    "Turbo1", "Turbo2",
    "Turbo1_ovtemp", "Turbo2_ovtemp",
}


# Path to the shared Tk window icon.  Package file lives in bf4bot/, so
# the project's assets/ folder is one directory up.
_ICON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "tree.ico")


def _apply_icon(top):
    """Set the tree-icon title bar / taskbar icon on a Toplevel.

    Wrapped in try/except so a missing or unreadable .ico just falls
    back to Tk's default feather -- never crashes the editor.
    """
    try:
        top.iconbitmap(_ICON_PATH)
    except tk.TclError:
        pass


def _bool_spec_to_ui(condition, threshold):
    """Given a stored (condition, threshold), decide what the 'Alert when'
    dropdown should show.  Defaults to 'OFF' if the pair looks weird."""
    if condition == ">" and threshold is not None and threshold < 0.5:
        return "ON"
    return "OFF"


def _bool_ui_to_spec(when):
    """Inverse: 'OFF' / 'ON' -> (condition, threshold) tuple to persist."""
    if when == "ON":
        return ">", 0.0
    return "<", 1.0


# ===========================================================================
# Shared: guided "Edit destinations" dialog used by both the Alarm editor
# and the Report editor.  Both need identical UX, so the logic lives here.
# ===========================================================================

def open_destinations_help_dialog(parent, config_path, channels_path,
                                  key_name="alarm_destinations"):
    """
    Pop up an instructional Toplevel that walks the user through editing
    their destination allowlist.

    Shared by the Alarm editor AND the Report editor.  Each editor has its
    OWN config file with its OWN destination-allowlist key:
      * alarm editor   -> alarm_config.yaml  , alarm_destinations:
      * report editor  -> report_config.yaml , report_destinations:

    No YAML is written automatically -- this function only SHOWS
    instructions and provides buttons to open the two relevant files in
    Notepad.  Manual editing is safer than scripted writes.

    parent        -- Tk widget to make the help Toplevel modal against.
    config_path   -- absolute path to the caller's config (alarm_config
                     or report_config).
    channels_path -- absolute path to channels.yaml.
    key_name      -- "alarm_destinations" or "report_destinations"; shown
                     in the instructional text so the user knows which
                     YAML key to edit.
    """
    import subprocess  # local import keeps module startup fast

    help_top = tk.Toplevel(parent)
    help_top.title("Edit destinations")
    _apply_icon(help_top)
    help_top.configure(bg=COLOR_BG)
    # Big enough to fit the full channel + DM instructions AND the buttons
    # at the bottom -- earlier versions had the buttons pushed offscreen.
    help_top.geometry("520x560")
    help_top.minsize(480, 480)
    help_top.transient(parent)
    help_top.grab_set()

    body_font = tkfont.Font(family="Segoe UI", size=10)
    small = tkfont.Font(family="Segoe UI", size=9)

    pad = tk.Frame(help_top, bg=COLOR_BG, padx=14, pady=12)
    pad.pack(fill="both", expand=True)

    # The config file name shown in instructions is inferred from the
    # caller's path so the user sees the exact filename they clicked on.
    config_name = os.path.basename(config_path)

    tk.Label(pad,
             text=f"Add / remove destinations in {config_name}",
             fg=COLOR_FG, bg=COLOR_BG,
             font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
             anchor="w").pack(fill="x")

    # anchor="nw" + fill="both" with expand=True so the label claims all
    # vertical space it needs.  A plain fill="x" would let Tk clip the
    # text when the window was too short.
    instructions = tk.Label(
        pad, bg=COLOR_BG, fg=COLOR_FG, font=body_font, justify="left",
        wraplength=480, anchor="nw",
        text=(
            f"Destinations are key names from channels.yaml, whitelisted\n"
            f"in {config_name}.  Use the buttons below to open each file.\n\n"
            f"To add a CHANNEL destination:\n"
            f"  * channels.yaml:    my_channel: C0XXXXXXXXX\n"
            f"  * {config_name}:    - my_channel\n\n"
            f"To add a PERSON to DM:\n"
            f"  * channels.yaml dm_users:   alice: U0XXXXXXXXX\n"
            f"  * {config_name}:            - dm_alice\n\n"
            f"After saving, close and REOPEN the whole alarm/report\n"
            f"editor window to see the new options."
        ),
    )
    instructions.pack(fill="both", expand=True, pady=(6, 10))

    def open_notepad(path):
        try:
            if not os.path.exists(path):
                open(path, "a").close()
            subprocess.Popen(["notepad", path])
        except Exception as e:
            messagebox.showerror("Could not open",
                                 f"Failed to open {path}:\n{e}")

    btns = tk.Frame(pad, bg=COLOR_BG)
    btns.pack(fill="x", pady=(0, 8))
    tk.Button(btns, text="Open channels.yaml",
              command=lambda: open_notepad(channels_path),
              bg=COLOR_BUTTON, fg=COLOR_FG,
              activebackground=COLOR_BUTTON_HOVER,
              activeforeground=COLOR_FG,
              relief="flat", borderwidth=0,
              padx=10, pady=4, font=small,
              cursor="hand2").pack(side="left", padx=4)
    tk.Button(btns, text=f"Open {config_name}",
              command=lambda: open_notepad(config_path),
              bg=COLOR_BUTTON, fg=COLOR_FG,
              activebackground=COLOR_BUTTON_HOVER,
              activeforeground=COLOR_FG,
              relief="flat", borderwidth=0,
              padx=10, pady=4, font=small,
              cursor="hand2").pack(side="left", padx=4)
    tk.Button(btns, text="Close",
              command=help_top.destroy,
              bg=COLOR_BUTTON, fg=COLOR_FG,
              activebackground=COLOR_BUTTON_HOVER,
              activeforeground=COLOR_FG,
              relief="flat", borderwidth=0,
              padx=10, pady=4, font=small,
              cursor="hand2").pack(side="right", padx=4)


# ===========================================================================
# AlarmEditorWindow -- the list/table view of all alarms.
# ===========================================================================

class AlarmEditorWindow:
    """
    A single Toplevel window showing one row per alarm.

    Owned by the watchdog.  Construct once per open; destroy on close.  If
    the user opens the editor, closes it, and opens it again, a fresh
    AlarmEditorWindow is built -- there is no persistent state in the class
    beyond the current window.
    """

    def __init__(self, parent, alarms_path, config_path, channels_path=None):
        """
        parent         -- the Tk root / parent window (watchdog's root).
        alarms_path    -- absolute path to alarms.yaml.
        config_path    -- absolute path to config.yaml (for allowlist).
        channels_path  -- absolute path to channels.yaml (for the "Edit
                          destinations" button; falls back next to config).
        """
        self.parent = parent
        self.alarms_path = alarms_path
        self.config_path = config_path
        # channels.yaml default: same folder as config.yaml.
        self.channels_path = channels_path or os.path.join(
            os.path.dirname(config_path), "channels.yaml")

        # Fresh AlarmManager each time we open the window so any on-disk
        # edits made elsewhere (by the user in Notepad, say) are picked up.
        self.alarm_mgr = AlarmManager(alarms_file=alarms_path)

        # Pull the destination allowlist from config.yaml.  If the field
        # is missing or empty we fall back to a minimal default so the
        # multi-select listbox has something to show.
        config = load_config(config_path)
        self.destinations = config.get(
            "alarm_destinations",
            ["operator_dm"],
        )

        # Build the window.
        self.top = tk.Toplevel(parent)
        self.top.title("BF4 Alarm Editor")
        _apply_icon(self.top)
        self.top.configure(bg=COLOR_BG)
        # Size that comfortably shows ~6 alarms and the Add button.
        self.top.geometry("720x420")
        # Place it roughly centered over the parent.
        self.top.transient(parent)

        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self):
        """Construct every static widget.  Alarm rows are built in _refresh()."""
        title_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=9)

        header = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="BF4 Alarms", fg=COLOR_FG, bg=COLOR_BG,
                 font=title_font).pack(side="left")
        tk.Label(header, text=f"Edits save to alarms.yaml -> bot reloads within 30s",
                 fg=COLOR_MUTED, bg=COLOR_BG, font=body_font).pack(side="right")

        # The middle area: a scroll-ready container for alarm rows.
        # Using a simple Frame (not Canvas+scrollbar) because 4-10 alarms fit
        # easily on screen.  If this ever scales to 50 alarms, swap in a
        # proper scrollable Canvas.
        self.rows_frame = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=4)
        self.rows_frame.pack(fill="both", expand=True)

        footer = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=10)
        footer.pack(fill="x")
        add_btn = tk.Button(footer, text="+ Add alarm", command=self._on_add,
                            bg=COLOR_BUTTON, fg=COLOR_FG,
                            activebackground=COLOR_BUTTON_HOVER,
                            activeforeground=COLOR_FG,
                            relief="flat", borderwidth=0,
                            padx=10, pady=4, font=body_font, cursor="hand2")
        add_btn.pack(side="left")

        close_btn = tk.Button(footer, text="Close", command=self.close,
                              bg=COLOR_BUTTON, fg=COLOR_FG,
                              activebackground=COLOR_BUTTON_HOVER,
                              activeforeground=COLOR_FG,
                              relief="flat", borderwidth=0,
                              padx=10, pady=4, font=body_font, cursor="hand2")
        close_btn.pack(side="right")

        self._refresh()

    # ------------------------------------------------------------------

    def _refresh(self):
        """
        Rebuild the alarm rows from self.alarm_mgr.alarms.

        Called on open and after any change (enable toggle, save, delete).
        Simpler than tracking individual row deltas -- the whole list is
        small and this is a rare-click UI.
        """
        # Clear existing rows (Tk does not have a "remove all children"
        # helper -- just destroy each one).
        for child in self.rows_frame.winfo_children():
            child.destroy()

        body_font = tkfont.Font(family="Segoe UI", size=9)
        code_font = tkfont.Font(family="Consolas", size=9)

        if not self.alarm_mgr.alarms:
            tk.Label(self.rows_frame,
                     text="No alarms configured.  Click '+ Add alarm' below.",
                     fg=COLOR_MUTED, bg=COLOR_BG, font=body_font,
                     pady=20).pack()
            return

        # One row per alarm.
        for index, alarm in enumerate(self.alarm_mgr.alarms):
            self._build_row(index, alarm, body_font, code_font)

    def _build_row(self, index, alarm, body_font, code_font):
        """Build and pack a single alarm's row."""
        # An outer frame draws the alternating-row background if we want
        # zebra striping later.  For now just a darker shade than the main.
        row_bg = COLOR_BG if index % 2 == 0 else "#262626"
        row = tk.Frame(self.rows_frame, bg=row_bg, padx=6, pady=4)
        row.pack(fill="x", pady=1)

        # Enabled-state toggle.  We use tk.BooleanVar so Tk handles the
        # checkbox state tracking for us; on change we write back to the
        # alarm and persist.
        enabled_var = tk.BooleanVar(value=alarm.enabled)

        def on_toggle(idx=index, var=enabled_var):
            # Mutate the in-memory alarm then save to disk.  This is the
            # fastest-feedback edit -- a single click changes enable state.
            self.alarm_mgr.alarms[idx].enabled = var.get()
            self.alarm_mgr.save()

        chk = tk.Checkbutton(row, variable=enabled_var, command=on_toggle,
                             bg=row_bg, fg=COLOR_FG, selectcolor=row_bg,
                             activebackground=row_bg,
                             activeforeground=COLOR_FG,
                             highlightthickness=0)
        chk.pack(side="left")

        # The summary text -- a single monospaced label so columns line up
        # a bit even without a real Treeview.  Uses the same "boolean vs
        # numeric" distinction the edit dialog uses so users see the same
        # phrasing both places.
        # Render destinations on one line if there's only one; otherwise
        # wrap each onto its own line so the row stays narrow and the
        # per-row edit/delete buttons don't get pushed off-screen when
        # an alarm routes to several destinations (issue #22).  Indent
        # continuation lines to roughly align under the first dest.
        if not alarm.destinations:
            dest_str = "(default)"
        elif len(alarm.destinations) == 1:
            dest_str = alarm.destinations[0]
        else:
            dest_str = ("\n" + " " * 53 + "-> ").join(alarm.destinations)
        if alarm.variable in BOOLEAN_VARIABLES:
            # For booleans we render the intent ("when OFF" / "when ON")
            # instead of the raw threshold, which is just an implementation
            # artifact of the < 1 / > 0 mapping.
            when = _bool_spec_to_ui(alarm.condition, alarm.threshold)
            summary = (
                f"{alarm.name:<14} "
                f"{alarm.variable:<14} "
                f"when {when:<4}  "
                f"every {alarm.interval_min}m  "
                f"-> {dest_str}"
            )
        else:
            unit = VARIABLE_UNITS.get(alarm.variable, "")
            summary = (
                f"{alarm.name:<14} "
                f"{alarm.variable:<14} "
                f"{alarm.condition} {alarm.threshold} {unit:<6}  "
                f"every {alarm.interval_min}m  "
                f"-> {dest_str}"
            )
        tk.Label(row, text=summary, fg=COLOR_FG, bg=row_bg,
                 font=code_font, anchor="w", justify="left").pack(
            side="left", padx=6, fill="x", expand=True)

        # Per-row buttons on the right.
        def on_edit(idx=index):
            AlarmEditDialog(
                self.top, self._on_save_from_dialog,
                destinations=self.destinations,
                existing_alarm=self.alarm_mgr.alarms[idx],
                existing_index=idx,
                config_path=self.config_path,
                channels_path=self.channels_path,
            )

        def on_delete(idx=index, name=alarm.name):
            if not messagebox.askyesno(
                "Delete alarm?",
                f"Delete alarm '{name}'?\nThis cannot be undone."):
                return
            del self.alarm_mgr.alarms[idx]
            self.alarm_mgr.save()
            self._refresh()

        for text, cmd in (("edit", on_edit), ("del", on_delete)):
            tk.Button(row, text=text, command=cmd,
                      bg=COLOR_BUTTON, fg=COLOR_FG,
                      activebackground=COLOR_BUTTON_HOVER,
                      activeforeground=COLOR_FG,
                      relief="flat", borderwidth=0,
                      padx=6, pady=0, font=body_font, cursor="hand2").pack(side="right", padx=2)

    # ------------------------------------------------------------------

    def _on_add(self):
        """Open the edit dialog in 'new alarm' mode."""
        AlarmEditDialog(
            self.top, self._on_save_from_dialog,
            destinations=self.destinations,
            existing_alarm=None,
            existing_index=None,
            config_path=self.config_path,
            channels_path=self.channels_path,
        )

    def _on_save_from_dialog(self, alarm, index):
        """
        Callback the edit dialog invokes when the user hits Save.

        alarm   -- the fully populated Alarm object the dialog built.
        index   -- the index in alarm_mgr.alarms to REPLACE, or None to APPEND.
        """
        if index is None:
            self.alarm_mgr.alarms.append(alarm)
        else:
            self.alarm_mgr.alarms[index] = alarm
        self.alarm_mgr.save()
        self._refresh()

    # ------------------------------------------------------------------

    def close(self):
        """User clicked Close (or X) -- destroy the window."""
        self.top.destroy()


# ===========================================================================
# AlarmEditDialog -- a single-alarm form for add and edit.
# ===========================================================================

class AlarmEditDialog:
    """
    Modal-ish form for editing or creating one alarm.

    On Save it calls ``on_save(alarm, index)`` where index is the original
    index (for edits) or None (for add).  On Cancel it just closes.

    Fields:
      - name          (Entry)
      - variable      (OptionMenu / dropdown)
      - condition     (Radiobuttons: > or <)
      - threshold     (Entry, numeric validation at save-time)
      - interval_min  (Entry, numeric)
      - destinations  (Listbox multiselect)
      - enabled       (Checkbutton)
    """

    def __init__(self, parent, on_save, destinations,
                 existing_alarm=None, existing_index=None,
                 config_path=None, channels_path=None):
        self.on_save = on_save
        self.destinations_allowlist = destinations
        self.existing_index = existing_index

        self.top = tk.Toplevel(parent)
        self.top.title("Add alarm" if existing_alarm is None else f"Edit: {existing_alarm.name}")
        _apply_icon(self.top)
        self.top.configure(bg=COLOR_BG)
        self.top.geometry("420x480")
        self.top.transient(parent)
        # Modal-ish: grab focus so the user can't accidentally interact
        # with the editor window behind us.
        self.top.grab_set()

        # Pre-fill fields either from the existing alarm or with defaults.
        if existing_alarm is None:
            self._init_values = dict(
                name="", variable="T_MXC", condition=">",
                threshold="", interval_min="5",
                destinations=["operator_dm"],
                enabled=True,
                bool_when="OFF",
            )
        else:
            self._init_values = dict(
                name=existing_alarm.name,
                variable=existing_alarm.variable,
                condition=existing_alarm.condition,
                threshold=str(existing_alarm.threshold),
                interval_min=str(existing_alarm.interval_min),
                destinations=list(existing_alarm.destinations),
                enabled=existing_alarm.enabled,
                # Pre-compute the "Alert when OFF/ON" UI label for boolean
                # variables -- otherwise we'd have to infer it in UI code.
                bool_when=_bool_spec_to_ui(
                    existing_alarm.condition, existing_alarm.threshold),
            )

        # config_path / channels_path are needed by the "edit destinations"
        # button so it can open the right YAML files in Notepad.  We
        # deliberately do NOT expose keys.yaml here -- that file contains
        # credentials and has no reason to be touched for channel edits.
        self.config_path = config_path or os.path.join(
            os.getcwd(), "config", "alarm_config.yaml")
        self.channels_path = channels_path or os.path.join(
            os.getcwd(), "config", "channels.yaml")

        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self):
        body_font = tkfont.Font(family="Segoe UI", size=10)
        label_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self._body_font = body_font  # stashed for dynamic row rebuilds

        # Use a grid inside a padded Frame so labels and inputs line up.
        form = tk.Frame(self.top, bg=COLOR_BG, padx=14, pady=12)
        form.pack(fill="both", expand=True)
        self._form = form

        # Keep row indices explicit so we can show/hide rows by grid
        # coordinate when the user flips between numeric and boolean
        # variables.  Each "row" occupies one grid row; numeric mode uses
        # rows 2 & 3, boolean mode replaces them with a single row 2.
        ROW_NAME = 0
        ROW_VARIABLE = 1
        ROW_CONDITION = 2   # numeric-only
        ROW_THRESHOLD = 3   # numeric-only
        ROW_BOOLWHEN = 2    # boolean-only (overwrites the condition row)
        ROW_INTERVAL = 4
        ROW_DEST = 5
        ROW_DEST_HINT = 6
        ROW_ENABLED = 7
        ROW_BUTTONS = 8
        self._row_ids = dict(
            cond=ROW_CONDITION, thresh=ROW_THRESHOLD, bool_when=ROW_BOOLWHEN,
        )
        form.columnconfigure(1, weight=1)

        def label(text, r):
            tk.Label(form, text=text, fg=COLOR_FG, bg=COLOR_BG,
                     font=label_font, anchor="w").grid(
                row=r, column=0, sticky="nw", pady=4)

        # --- Name ----
        label("Name", ROW_NAME)
        self.name_var = tk.StringVar(value=self._init_values["name"])
        tk.Entry(form, textvariable=self.name_var,
                 bg="#2a2a2a", fg=COLOR_FG, insertbackground=COLOR_FG,
                 relief="flat", font=body_font).grid(
            row=ROW_NAME, column=1, sticky="ew", pady=4, padx=(8, 0))

        # --- Variable (OptionMenu) ----
        # The StringVar triggers _on_variable_changed() whenever the user
        # picks a different sensor.  That callback is what swaps the
        # threshold field for the boolean "alert when" dropdown.
        label("Variable", ROW_VARIABLE)
        self.variable_var = tk.StringVar(value=self._init_values["variable"])
        var_menu = tk.OptionMenu(form, self.variable_var, *VALID_VARIABLES,
                                 command=self._on_variable_changed)
        var_menu.config(bg="#2a2a2a", fg=COLOR_FG,
                        activebackground=COLOR_BUTTON_HOVER,
                        activeforeground=COLOR_FG,
                        relief="flat", highlightthickness=0, font=body_font)
        var_menu.grid(row=ROW_VARIABLE, column=1, sticky="ew", pady=4, padx=(8, 0))

        # --- Condition radio buttons (rows 2, numeric only) ----
        self._cond_label = tk.Label(
            form, text="Condition", fg=COLOR_FG, bg=COLOR_BG,
            font=label_font, anchor="w")
        self._cond_label.grid(row=ROW_CONDITION, column=0, sticky="nw", pady=4)
        self.condition_var = tk.StringVar(value=self._init_values["condition"])
        self._cond_frame = tk.Frame(form, bg=COLOR_BG)
        for val, lbl in ((">", "> (above)"), ("<", "< (below)")):
            tk.Radiobutton(self._cond_frame, text=lbl, value=val,
                           variable=self.condition_var,
                           bg=COLOR_BG, fg=COLOR_FG, selectcolor=COLOR_BG,
                           activebackground=COLOR_BG,
                           activeforeground=COLOR_FG,
                           highlightthickness=0,
                           font=body_font).pack(side="left", padx=4)
        self._cond_frame.grid(row=ROW_CONDITION, column=1, sticky="ew",
                              pady=4, padx=(8, 0))

        # --- Threshold + unit label (row 3, numeric only) ----
        # The inner sub-frame holds the entry and the unit so they sit on
        # one line and the unit stays right-aligned to the entry.
        self._thresh_label = tk.Label(
            form, text="Threshold", fg=COLOR_FG, bg=COLOR_BG,
            font=label_font, anchor="w")
        self._thresh_label.grid(row=ROW_THRESHOLD, column=0, sticky="nw", pady=4)
        self.threshold_var = tk.StringVar(value=self._init_values["threshold"])
        self._thresh_frame = tk.Frame(form, bg=COLOR_BG)
        threshold_entry = tk.Entry(self._thresh_frame,
                                   textvariable=self.threshold_var,
                                   bg="#2a2a2a", fg=COLOR_FG,
                                   insertbackground=COLOR_FG,
                                   relief="flat", font=body_font)
        threshold_entry.pack(side="left", fill="x", expand=True)
        # Unit label gets updated whenever the variable changes.
        self._unit_label = tk.Label(self._thresh_frame, text="",
                                    fg=COLOR_MUTED, bg=COLOR_BG,
                                    font=body_font)
        self._unit_label.pack(side="left", padx=(6, 0))
        self._thresh_frame.grid(row=ROW_THRESHOLD, column=1, sticky="ew",
                                pady=4, padx=(8, 0))

        # --- Boolean-only row (row 2, replaces condition+threshold) ----
        # Hidden by default; _on_variable_changed() grid-configures it
        # when the user picks a bool variable.  We keep a Label + dropdown
        # pair so the geometry matches the other rows.
        self._bool_label = tk.Label(
            form, text="Alert when", fg=COLOR_FG, bg=COLOR_BG,
            font=label_font, anchor="w")
        self.bool_when_var = tk.StringVar(value=self._init_values["bool_when"])
        self._bool_menu = tk.OptionMenu(form, self.bool_when_var, "OFF", "ON")
        self._bool_menu.config(bg="#2a2a2a", fg=COLOR_FG,
                               activebackground=COLOR_BUTTON_HOVER,
                               activeforeground=COLOR_FG,
                               relief="flat", highlightthickness=0,
                               font=body_font)

        ROW_INTERVAL = 4
        ROW_DEST = 5
        ROW_DEST_HINT = 6
        ROW_ENABLED = 7
        ROW_BUTTONS = 8

        # --- Interval (minutes) ----
        label("Interval (min)", ROW_INTERVAL)
        self.interval_var = tk.StringVar(value=self._init_values["interval_min"])
        tk.Entry(form, textvariable=self.interval_var,
                 bg="#2a2a2a", fg=COLOR_FG, insertbackground=COLOR_FG,
                 relief="flat", font=body_font).grid(
            row=ROW_INTERVAL, column=1, sticky="ew", pady=4, padx=(8, 0))

        # --- Destinations (multi-select listbox) + "edit destinations" btn ----
        label("Destinations", ROW_DEST)
        dest_frame = tk.Frame(form, bg=COLOR_BG)
        self.dest_listbox = tk.Listbox(
            dest_frame, selectmode="multiple", exportselection=False,
            bg="#2a2a2a", fg=COLOR_FG,
            selectbackground="#3b5998", selectforeground=COLOR_FG,
            relief="flat", highlightthickness=0,
            height=max(3, min(6, len(self.destinations_allowlist))),
            font=body_font,
        )
        self.dest_listbox.pack(side="left", fill="both", expand=True)
        for d in self.destinations_allowlist:
            self.dest_listbox.insert("end", d)
        # Pre-select any destinations this alarm already uses.
        for i, d in enumerate(self.destinations_allowlist):
            if d in self._init_values["destinations"]:
                self.dest_listbox.selection_set(i)
        # "Edit" button vertically stacked next to the listbox -- opens
        # the guided help+Notepad dialog that walks the user through
        # editing keys.yaml + config.yaml.
        edit_btn = tk.Button(dest_frame, text="Edit\nlist",
                             command=self._on_edit_destinations,
                             bg=COLOR_BUTTON, fg=COLOR_FG,
                             activebackground=COLOR_BUTTON_HOVER,
                             activeforeground=COLOR_FG,
                             relief="flat", borderwidth=0,
                             padx=8, pady=2, font=body_font, cursor="hand2")
        edit_btn.pack(side="left", padx=(6, 0), fill="y")
        dest_frame.grid(row=ROW_DEST, column=1, sticky="ew",
                        pady=4, padx=(8, 0))

        # Hint label beneath the listbox.
        hint = tk.Label(
            form,
            text=("Pick one or more.  Click 'Edit list' to add/remove "
                  "destinations (opens alarm_config.yaml + channels.yaml)."),
            fg=COLOR_MUTED, bg=COLOR_BG,
            font=tkfont.Font(family="Segoe UI", size=8),
            wraplength=340, justify="left",
        )
        hint.grid(row=ROW_DEST_HINT, column=0, columnspan=2, sticky="w",
                  pady=(0, 6))

        # --- Enabled checkbox ----
        self.enabled_var = tk.BooleanVar(value=self._init_values["enabled"])
        tk.Checkbutton(
            form, text="Enabled",
            variable=self.enabled_var,
            bg=COLOR_BG, fg=COLOR_FG, selectcolor=COLOR_BG,
            activebackground=COLOR_BG, activeforeground=COLOR_FG,
            highlightthickness=0, font=body_font,
        ).grid(row=ROW_ENABLED, column=0, columnspan=2, sticky="w", pady=4)

        # --- Buttons ----
        btn_frame = tk.Frame(form, bg=COLOR_BG)
        btn_frame.grid(row=ROW_BUTTONS, column=0, columnspan=2,
                       sticky="ew", pady=(10, 0))
        tk.Button(btn_frame, text="Save", command=self._on_save,
                  bg=COLOR_OK, fg="#111", activebackground="#3fa043",
                  relief="flat", borderwidth=0,
                  padx=12, pady=4, font=body_font, cursor="hand2").pack(side="right", padx=4)
        tk.Button(btn_frame, text="Cancel", command=self._on_cancel,
                  bg=COLOR_BUTTON, fg=COLOR_FG,
                  activebackground=COLOR_BUTTON_HOVER,
                  activeforeground=COLOR_FG,
                  relief="flat", borderwidth=0,
                  padx=12, pady=4, font=body_font, cursor="hand2").pack(side="right", padx=4)

        # Make the initial numeric/boolean layout correct.  This sets the
        # unit label text and hides/shows threshold vs. bool-when rows.
        self._on_variable_changed(self.variable_var.get())

    # ------------------------------------------------------------------

    def _on_variable_changed(self, _selected=None):
        """
        Called whenever the user picks a different Variable from the dropdown.

        Swaps the form between "numeric" mode (condition radios + threshold
        entry with unit) and "boolean" mode (single Alert-when dropdown).
        Also updates the unit label next to the threshold field.

        Tk's ``grid_remove`` hides a widget while preserving its grid
        config; ``grid`` (with no args when cells were previously set up)
        restores it.  We use that for the row-swap instead of rebuilding
        the form on every variable change.
        """
        var = self.variable_var.get()
        is_bool = var in BOOLEAN_VARIABLES
        rc = self._row_ids["cond"]

        if is_bool:
            # Hide the numeric rows.
            self._cond_label.grid_remove()
            self._cond_frame.grid_remove()
            self._thresh_label.grid_remove()
            self._thresh_frame.grid_remove()
            # Show the single "Alert when" row in place of the condition row.
            self._bool_label.grid(row=rc, column=0, sticky="nw", pady=4)
            self._bool_menu.grid(row=rc, column=1, sticky="ew",
                                 pady=4, padx=(8, 0))
        else:
            self._bool_label.grid_remove()
            self._bool_menu.grid_remove()
            self._cond_label.grid()
            self._cond_frame.grid()
            self._thresh_label.grid()
            self._thresh_frame.grid()
            # Update the unit label so the user sees, e.g., "mK" next to
            # the threshold entry when T_MXC is selected.
            self._unit_label.config(text=VARIABLE_UNITS.get(var, ""))

    def _on_edit_destinations(self):
        """Open the guided destinations help dialog (shared with Report editor)."""
        open_destinations_help_dialog(
            self.top,
            config_path=self.config_path,
            channels_path=self.channels_path,
        )

    # ------------------------------------------------------------------

    def _on_save(self):
        """
        Validate fields, build the Alarm, call on_save callback, close.

        Validation is pragmatic -- we do not pretend to be a form library.
        If the user mistypes we show a messagebox and leave the form open
        so they can fix the bad field.
        """
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Missing name", "Alarm name cannot be empty.")
            return
        if " " in name:
            messagebox.showerror("Invalid name",
                                 "Alarm name cannot contain spaces.")
            return

        variable = self.variable_var.get()
        is_bool = variable in BOOLEAN_VARIABLES

        # Threshold + condition handling depends on variable type.
        if is_bool:
            # Boolean variables: translate "OFF"/"ON" choice into the
            # internal (condition, threshold) pair.  See _bool_ui_to_spec
            # for the mapping rationale.
            condition, threshold = _bool_ui_to_spec(self.bool_when_var.get())
        else:
            condition = self.condition_var.get()
            try:
                threshold = float(self.threshold_var.get())
            except ValueError:
                messagebox.showerror("Bad threshold",
                                     f"Threshold must be a number, got "
                                     f"'{self.threshold_var.get()}'.")
                return

        try:
            interval_min = int(self.interval_var.get())
        except ValueError:
            messagebox.showerror("Bad interval",
                                 f"Interval must be a whole number of "
                                 f"minutes, got '{self.interval_var.get()}'.")
            return
        if interval_min < 1:
            messagebox.showerror("Bad interval",
                                 "Interval must be at least 1 minute.")
            return

        # Selected destinations come back as listbox indices -- convert
        # to actual names.
        selected_indices = self.dest_listbox.curselection()
        destinations = [self.destinations_allowlist[i] for i in selected_indices]
        if not destinations:
            messagebox.showerror("No destination",
                                 "Pick at least one destination.  If the "
                                 "list is empty, add entries to "
                                 "alarm_config.yaml's alarm_destinations: "
                                 "allowlist.")
            return

        alarm = Alarm(
            name=name,
            variable=variable,
            condition=condition,
            threshold=threshold,
            interval_min=interval_min,
            enabled=bool(self.enabled_var.get()),
            destination=destinations,
        )
        self.on_save(alarm, self.existing_index)
        self.top.destroy()

    def _on_cancel(self):
        """User clicked Cancel -- discard changes and close."""
        self.top.destroy()
