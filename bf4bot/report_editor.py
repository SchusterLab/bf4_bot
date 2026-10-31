"""
Report editor GUI -- a Tk popup window attached to the watchdog widget.

Mirrors the design of ``alarm_editor.py``: a table-like list of all
scheduled reports, plus a per-report edit/add dialog.  Edits save
through a ``ReportManager`` which writes ``config/reports.yaml``; the
running bot live-reloads that file on mtime change.

We import the color palette from alarm_editor so both editors look
alike.  Tk-specific helpers (labels, buttons, etc.) are duplicated
inline rather than pulled from alarm_editor -- the widgets are
simple enough that sharing would add more indirection than it saves.
"""

import os
import subprocess
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from bf4bot.config import ScheduledReport, ReportManager, load_report_config
# Reuse the dark-theme colours so the two editors feel consistent.
from bf4bot.alarm_editor import (
    COLOR_BG, COLOR_FG, COLOR_MUTED, COLOR_OK, COLOR_BUTTON,
    COLOR_BUTTON_HOVER,
    open_destinations_help_dialog,
    _apply_icon,
)


REPORT_TYPES = ["fridge", "compressor", "turbo"]
DAY_SCHEDULES = ["daily", "weekdays", "weekly-monday", "weekly-friday"]


# ===========================================================================
# ReportEditorWindow -- list view of all scheduled reports.
# ===========================================================================

class ReportEditorWindow:
    """
    Toplevel window showing one row per scheduled report.

    Reads ``config/reports.yaml`` on open and writes back through
    ReportManager.save() after any change.
    """

    def __init__(self, parent, reports_path=None, report_config_path=None,
                 channels_path=None):
        """
        parent              -- the Tk root / parent window.
        reports_path        -- absolute path to reports.yaml.
        report_config_path  -- absolute path to report_config.yaml.  Holds
                               the report_destinations allowlist (separate
                               from the alarm editor's allowlist).
        channels_path       -- channels.yaml path, forwarded to the edit
                               dialog's "Edit destinations" button.
        """
        self.parent = parent
        self.reports_path = reports_path or "config/reports.yaml"
        self.config_path = report_config_path or "config/report_config.yaml"
        self.channels_path = channels_path or os.path.join(
            os.path.dirname(self.config_path), "channels.yaml")

        # Fresh manager each time so manual YAML edits are picked up.
        self.report_mgr = ReportManager(reports_file=self.reports_path)

        # Report-specific destination allowlist, lives in report_config.yaml.
        # Kept SEPARATE from alarm_destinations so the two editors can
        # allow different targets.
        self.destinations = load_report_config(self.config_path).get(
            "report_destinations", ["operator_dm"])

        self.top = tk.Toplevel(parent)
        self.top.title("BF4 Report Editor")
        _apply_icon(self.top)
        self.top.configure(bg=COLOR_BG)
        self.top.geometry("760x380")
        self.top.transient(parent)

        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self):
        title_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=9)

        header = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="Scheduled Reports",
                 fg=COLOR_FG, bg=COLOR_BG, font=title_font).pack(side="left")
        tk.Label(
            header,
            text="Edits save to reports.yaml -> bot reloads within 30s",
            fg=COLOR_MUTED, bg=COLOR_BG, font=body_font).pack(side="right")

        self.rows_frame = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=4)
        self.rows_frame.pack(fill="both", expand=True)

        footer = tk.Frame(self.top, bg=COLOR_BG, padx=12, pady=10)
        footer.pack(fill="x")
        tk.Button(footer, text="+ Add report", command=self._on_add,
                  bg=COLOR_BUTTON, fg=COLOR_FG,
                  activebackground=COLOR_BUTTON_HOVER,
                  activeforeground=COLOR_FG, relief="flat", borderwidth=0,
                  padx=10, pady=4, font=body_font,
                  cursor="hand2").pack(side="left")
        tk.Button(footer, text="Close", command=self.close,
                  bg=COLOR_BUTTON, fg=COLOR_FG,
                  activebackground=COLOR_BUTTON_HOVER,
                  activeforeground=COLOR_FG, relief="flat", borderwidth=0,
                  padx=10, pady=4, font=body_font,
                  cursor="hand2").pack(side="right")

        self._refresh()

    def _refresh(self):
        """Rebuild the rows after any change."""
        for child in self.rows_frame.winfo_children():
            child.destroy()

        body_font = tkfont.Font(family="Segoe UI", size=9)
        code_font = tkfont.Font(family="Consolas", size=9)

        if not self.report_mgr.reports:
            tk.Label(self.rows_frame,
                     text="No reports scheduled.  Click '+ Add report' below.",
                     fg=COLOR_MUTED, bg=COLOR_BG, font=body_font,
                     pady=20).pack()
            return

        for index, report in enumerate(self.report_mgr.reports):
            self._build_row(index, report, body_font, code_font)

    def _build_row(self, index, report, body_font, code_font):
        row_bg = COLOR_BG if index % 2 == 0 else "#262626"
        row = tk.Frame(self.rows_frame, bg=row_bg, padx=6, pady=4)
        row.pack(fill="x", pady=1)

        # Render a compact summary.  Wrap each destination onto its own
        # line (matching alarm_editor issue #22) so the row stays narrow
        # and the per-row edit/delete buttons don't get pushed off-screen
        # when a report routes to several targets.
        prefix = (
            f"{report.name:<24} "
            f"{report.report_type:<10} "
            f"{report.hour:>2}:00  "
            f"{report.days:<14} "
            f"{report.hours:<3}h  "
        )
        indent = " " * len(prefix)
        if not report.destinations:
            dest_str = "(default)"
        elif len(report.destinations) == 1:
            dest_str = report.destinations[0]
        else:
            dest_str = ("\n" + indent + "-> ").join(report.destinations)
        summary = f"{prefix}-> {dest_str}"

        def on_edit(idx=index):
            ReportEditDialog(
                self.top, self._on_save_from_dialog,
                destinations_allowlist=self.destinations,
                existing_report=self.report_mgr.reports[idx],
                existing_index=idx,
                config_path=self.config_path,
                channels_path=self.channels_path,
            )

        def on_delete(idx=index, name=report.name):
            if not messagebox.askyesno(
                "Delete report?",
                f"Delete report '{name}'?\nThis cannot be undone."):
                return
            del self.report_mgr.reports[idx]
            self.report_mgr.save()
            self._refresh()

        # Pack buttons BEFORE the expanding label so they always reserve
        # their width on the right edge, even on narrow windows.
        for text, cmd in (("del", on_delete), ("edit", on_edit)):
            tk.Button(row, text=text, command=cmd,
                      bg=COLOR_BUTTON, fg=COLOR_FG,
                      activebackground=COLOR_BUTTON_HOVER,
                      activeforeground=COLOR_FG,
                      relief="flat", borderwidth=0,
                      padx=6, pady=0, font=body_font,
                      cursor="hand2").pack(side="right", padx=2)

        tk.Label(row, text=summary, fg=COLOR_FG, bg=row_bg,
                 font=code_font, anchor="w", justify="left").pack(
            side="left", padx=6, fill="x", expand=True)

    def _on_add(self):
        ReportEditDialog(
            self.top, self._on_save_from_dialog,
            destinations_allowlist=self.destinations,
            existing_report=None,
            existing_index=None,
            config_path=self.config_path,
            channels_path=self.channels_path,
        )

    def _on_save_from_dialog(self, report, index):
        if index is None:
            self.report_mgr.reports.append(report)
        else:
            self.report_mgr.reports[index] = report
        self.report_mgr.save()
        self._refresh()

    def close(self):
        self.top.destroy()


# ===========================================================================
# ReportEditDialog -- single-report form for add + edit.
# ===========================================================================

class ReportEditDialog:
    """
    Form for editing or creating one scheduled report.

    Fields:
      name          (Entry)
      type          (OptionMenu: fridge / compressor / turbo)
      hour          (Spinbox 0-23)
      days          (OptionMenu: daily / weekdays / weekly-monday / weekly-friday)
      hours (duration)  (Entry, integer)
      destinations  (Listbox multiselect)
    """

    def __init__(self, parent, on_save, destinations_allowlist,
                 existing_report=None, existing_index=None,
                 config_path=None, channels_path=None):
        self.on_save = on_save
        self.destinations_allowlist = destinations_allowlist
        self.existing_index = existing_index
        self.config_path = config_path or "config/report_config.yaml"
        self.channels_path = channels_path or "config/channels.yaml"

        self.top = tk.Toplevel(parent)
        self.top.title("Add report" if existing_report is None
                       else f"Edit: {existing_report.name}")
        _apply_icon(self.top)
        self.top.configure(bg=COLOR_BG)
        self.top.geometry("460x480")
        self.top.transient(parent)
        self.top.grab_set()

        if existing_report is None:
            self._init_values = dict(
                name="", report_type="fridge", hour="9",
                days="daily", hours="24",
                destinations=[],
            )
        else:
            self._init_values = dict(
                name=existing_report.name,
                report_type=existing_report.report_type,
                hour=str(existing_report.hour),
                days=existing_report.days,
                hours=str(existing_report.hours),
                destinations=list(existing_report.destinations),
            )

        self._build_ui()

    def _build_ui(self):
        body_font = tkfont.Font(family="Segoe UI", size=10)
        label_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        form = tk.Frame(self.top, bg=COLOR_BG, padx=14, pady=12)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)

        def label(text, r):
            tk.Label(form, text=text, fg=COLOR_FG, bg=COLOR_BG,
                     font=label_font, anchor="w").grid(
                row=r, column=0, sticky="nw", pady=4)

        def entry(var, r):
            tk.Entry(form, textvariable=var, bg="#2a2a2a", fg=COLOR_FG,
                     insertbackground=COLOR_FG, relief="flat",
                     font=body_font).grid(
                row=r, column=1, sticky="ew", pady=4, padx=(8, 0))

        # Row 0: name
        label("Name", 0)
        self.name_var = tk.StringVar(value=self._init_values["name"])
        entry(self.name_var, 0)

        # Row 1: type
        label("Type", 1)
        self.type_var = tk.StringVar(value=self._init_values["report_type"])
        tk.OptionMenu(form, self.type_var, *REPORT_TYPES).grid(
            row=1, column=1, sticky="ew", pady=4, padx=(8, 0))

        # Row 2: hour
        label("Hour (0-23)", 2)
        self.hour_var = tk.StringVar(value=self._init_values["hour"])
        tk.Spinbox(form, from_=0, to=23, textvariable=self.hour_var,
                   bg="#2a2a2a", fg=COLOR_FG, insertbackground=COLOR_FG,
                   relief="flat", font=body_font,
                   buttonbackground=COLOR_BUTTON).grid(
            row=2, column=1, sticky="ew", pady=4, padx=(8, 0))

        # Row 3: days
        label("Schedule", 3)
        self.days_var = tk.StringVar(value=self._init_values["days"])
        tk.OptionMenu(form, self.days_var, *DAY_SCHEDULES).grid(
            row=3, column=1, sticky="ew", pady=4, padx=(8, 0))

        # Row 4: hours (duration)
        label("Data duration (h)", 4)
        self.hours_var = tk.StringVar(value=self._init_values["hours"])
        entry(self.hours_var, 4)

        # Row 5: destinations
        label("Destinations", 5)
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
        for i, d in enumerate(self.destinations_allowlist):
            if d in self._init_values["destinations"]:
                self.dest_listbox.selection_set(i)
        tk.Button(dest_frame, text="Edit\nlist",
                  command=self._on_edit_destinations,
                  bg=COLOR_BUTTON, fg=COLOR_FG,
                  activebackground=COLOR_BUTTON_HOVER,
                  activeforeground=COLOR_FG,
                  relief="flat", borderwidth=0,
                  padx=8, pady=2, font=body_font,
                  cursor="hand2").pack(side="left", padx=(6, 0), fill="y")
        dest_frame.grid(row=5, column=1, sticky="ew", pady=4, padx=(8, 0))

        tk.Label(form,
                 text=("Pick one or more.  Same allowlist as alarms -- "
                       "edit via the alarm editor's 'Edit list' button."),
                 fg=COLOR_MUTED, bg=COLOR_BG,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 wraplength=340, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # Buttons
        btn_frame = tk.Frame(form, bg=COLOR_BG)
        btn_frame.grid(row=7, column=0, columnspan=2,
                       sticky="ew", pady=(10, 0))
        tk.Button(btn_frame, text="Save", command=self._on_save,
                  bg=COLOR_OK, fg="#111", activebackground="#3fa043",
                  relief="flat", borderwidth=0,
                  padx=12, pady=4, font=body_font,
                  cursor="hand2").pack(side="right", padx=4)
        tk.Button(btn_frame, text="Cancel", command=self.top.destroy,
                  bg=COLOR_BUTTON, fg=COLOR_FG,
                  activebackground=COLOR_BUTTON_HOVER,
                  activeforeground=COLOR_FG,
                  relief="flat", borderwidth=0,
                  padx=12, pady=4, font=body_font,
                  cursor="hand2").pack(side="right", padx=4)

    def _on_edit_destinations(self):
        """Open the shared guided help dialog (same UX as alarm editor)."""
        open_destinations_help_dialog(
            self.top,
            config_path=self.config_path,
            channels_path=self.channels_path,
            key_name="report_destinations",
        )

    def _on_save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Missing name",
                                 "Report name cannot be empty.")
            return
        try:
            hour = int(self.hour_var.get())
            if not 0 <= hour <= 23:
                raise ValueError
        except ValueError:
            messagebox.showerror("Bad hour",
                                 "Hour must be an integer 0-23.")
            return
        try:
            hours = int(self.hours_var.get())
            if hours < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Bad duration",
                                 "Duration (hours) must be a positive integer.")
            return

        sel = self.dest_listbox.curselection()
        destinations = [self.destinations_allowlist[i] for i in sel]
        if not destinations:
            messagebox.showerror("No destination",
                                 "Pick at least one destination.")
            return

        report = ScheduledReport(
            name=name,
            destinations=destinations,
            hour=hour,
            days=self.days_var.get(),
            hours=hours,
            report_type=self.type_var.get(),
        )
        self.on_save(report, self.existing_index)
        self.top.destroy()
