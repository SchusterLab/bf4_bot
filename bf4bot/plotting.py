"""
Plot generation for BF4 fridge reports.
Generates PNG images that can be uploaded to Slack.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import numpy as np
import os
import threading
from datetime import datetime

# matplotlib's pyplot interface keeps a PROCESS-GLOBAL "current figure" and
# shares font/rasterizer caches.  This bot renders plots from two threads
# (the background alarm/scheduled-report thread and the Slack command
# thread), so we (1) build every figure through the object-oriented
# Figure/FigureCanvasAgg API -- never pyplot's plt.subplots/plt.close -- and
# (2) serialize the layout+rasterize step under a module lock.  Together
# these remove the cross-thread global-state hazard.
_RENDER_LOCK = threading.Lock()

# Bluefors native UI colors
COLORS = {
    "Still": "#E8A317",
    "50K": "#CC3333",
    "MXC": "#2255CC",
    "4K": "#66AA22",
    "P1": "#CC3333",
    "P2": "#2255CC",
    "P3": "#E8A317",
    "P4": "#2255CC",
    "P5": "#66AA22",
    "P6": "#22AAAA",
    "Flow": "#CC3333",
}

OUTPUT_DIR = "C:/Users/slab/Code/BF4_BOT/plots"

# Subfolders under OUTPUT_DIR that group PNGs by their purpose.  The
# subfolder a plot lands in is chosen by the caller via the ``subfolder``
# kwarg; plotting.py itself does not care which one is used.
SUBFOLDER_DAILY = "daily_report"     # scheduled reports from config.yaml
SUBFOLDER_ON_DEMAND = "on_demand"    # ad-hoc from DM/mention commands
SUBFOLDER_ALARMS = "alarms"          # triggered alarms (real and test)


def _ensure_subfolder(subfolder):
    """Make sure OUTPUT_DIR/<subfolder> exists and return its path."""
    path = os.path.join(OUTPUT_DIR, subfolder)
    os.makedirs(path, exist_ok=True)
    return path


def _window_filename(prefix, history):
    """
    Build a report filename that encodes the DATA window plus a generation
    timestamp.  Example:
    'fridge_2026-04-14_1030_to_2026-04-15_1030_103042_517430.png'.

    ``prefix`` is a short type label ("fridge", "compressor", "turbo").
    ``history`` is the dict from reader.read_history() -- contains cutoff+now.

    The trailing HHMMSS_microseconds stamp guarantees a UNIQUE path per
    render.  Previously the name encoded only the window at minute precision,
    so two reports of the same window in the same minute (e.g. a DM report
    followed by a channel @mention, or one scheduled report fanned out to
    several destinations) wrote to the SAME file -- the second render
    overwrote the first while it may still have been uploading.  Uniqueness
    removes that overwrite/race entirely (at the cost of no longer
    auto-deduplicating identical-window PNGs on disk).
    """
    fmt = "%Y-%m-%d_%H%M"
    start = history["cutoff"].strftime(fmt)
    end = history["now"].strftime(fmt)
    stamp = datetime.now().strftime("%H%M%S_%f")
    return f"{prefix}_{start}_to_{end}_{stamp}.png"


def _alarm_filename(alarm_name, variable_name, trigger_time=None):
    """
    Build an alarm filename that encodes the alarm name and trigger time.
    Example: 'P4_high_2026-04-15_1635.png'.

    Falls back to variable_name if alarm_name is not supplied (older callers).
    """
    if trigger_time is None:
        trigger_time = datetime.now()
    name_part = alarm_name or variable_name
    return f"{name_part}_{trigger_time.strftime('%Y-%m-%d_%H%M')}.png"


def _report_title(name, history):
    """
    Build the two-line suptitle for a report plot.

    ``name`` is the human label ("BF4 Fridge Status", etc.).  Line 1 reads
    either "Last <label>" (a trailing window ending now) or
    "<label> around <anchor>" (an at-time report anchored on a past time).
    Line 2 is always the concrete window start..end.
    """
    cutoff = history["cutoff"]
    now = history["now"]
    label = history.get("label", f"{history.get('hours', '')}h")
    anchor = history.get("anchor")
    fmt = "%Y-%m-%d %H:%M"
    if anchor is not None:
        line1 = f"{name} -- {label} around {anchor.strftime(fmt)}"
    else:
        line1 = f"{name} -- Last {label}"
    return f"{line1}\n{cutoff.strftime(fmt)} to {now.strftime(fmt)}"


def _format_ax(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="x", rotation=45)
    ax.grid(True, alpha=0.3)


def plot_fridge_status(history, output_path=None, subfolder=SUBFOLDER_ON_DEMAND):
    """
    Generate the 8-panel fridge status plot.  Returns path to PNG.

    Filename encodes the data window (cutoff..now), not the generation time,
    so two reports with the same window overwrite each other rather than
    accumulating near-duplicates.  ``subfolder`` selects which plots/ subdir
    the file lands in -- daily_report for scheduled, on_demand for ad-hoc.
    """
    if output_path is None:
        out_dir = _ensure_subfolder(subfolder)
        output_path = os.path.join(out_dir, _window_filename("fridge", history))

    fig = Figure(figsize=(16, 18))
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 2)
    fig.suptitle(_report_title("BF4 Fridge Status", history),
                 fontsize=14, fontweight="bold")

    # --- Row 0: flange temperatures, paired on dual axes.  Each pair shares
    # a unit but spans very different magnitudes (MXC ~10mK vs Still ~800mK;
    # 4K vs 50K), so a shared axis would flatten one trace -- twinx gives each
    # its own scale.  Left/right y-labels are coloured to their trace.
    # [0,0] MXC (left, mK) + Still (right, mK)
    ax = axes[0, 0]
    ts = history["temps"]["MXC"]["timestamps"]
    vals = history["temps"]["MXC"]["values"]
    if ts:
        ax.plot(ts, [v * 1000 for v in vals], color=COLORS["MXC"], linewidth=0.8)
    ax.set_ylabel("MXC (mK)", color=COLORS["MXC"])
    ax.tick_params(axis="y", labelcolor=COLORS["MXC"])
    ax.set_title("MXC & Still Flange", fontweight="bold")
    _format_ax(ax)
    ax2 = ax.twinx()
    ts = history["temps"]["Still"]["timestamps"]
    vals = history["temps"]["Still"]["values"]
    if ts:
        ax2.plot(ts, [v * 1000 for v in vals], color=COLORS["Still"], linewidth=0.8)
    ax2.set_ylabel("Still (mK)", color=COLORS["Still"])
    ax2.tick_params(axis="y", labelcolor=COLORS["Still"])
    # Draw MXC (primary axis) on top of Still (twin): raise the primary's
    # z-order above the twin, then hide the primary's opaque background so
    # the Still trace still shows through from behind.
    ax.set_zorder(ax2.get_zorder() + 1)
    ax.patch.set_visible(False)

    # [0,1] 4K (left, K) + 50K (right, K)
    ax = axes[0, 1]
    ts = history["temps"]["4K"]["timestamps"]
    vals = history["temps"]["4K"]["values"]
    if ts:
        ax.plot(ts, vals, color=COLORS["4K"], linewidth=0.8)
    ax.set_ylabel("4K (K)", color=COLORS["4K"])
    ax.tick_params(axis="y", labelcolor=COLORS["4K"])
    ax.set_title("4K & 50K Flange", fontweight="bold")
    _format_ax(ax)
    ax2 = ax.twinx()
    ts = history["temps"]["50K"]["timestamps"]
    vals = history["temps"]["50K"]["values"]
    if ts:
        ax2.plot(ts, vals, color=COLORS["50K"], linewidth=0.8)
    ax2.set_ylabel("50K (K)", color=COLORS["50K"])
    ax2.tick_params(axis="y", labelcolor=COLORS["50K"])

    ts_pres = history["pressures"]["timestamps"]
    pres = np.array(history["pressures"]["values"]) if history["pressures"]["values"] else np.empty((0, 6))

    # --- Row 1 ---
    # [1,0] P2 (left, log mbar) + He-3 flow (right, mmol/s)
    ax = axes[1, 0]
    if len(ts_pres) > 0:
        ax.plot(ts_pres, pres[:, 1], color=COLORS["P2"], linewidth=0.8)
    ax.set_ylabel("P2 (mbar)", color=COLORS["P2"])
    ax.tick_params(axis="y", labelcolor=COLORS["P2"])
    ax.set_yscale("log")
    ax.set_title("P2 (before turbo) & He-3 Flow", fontweight="bold")
    _format_ax(ax)
    ax2 = ax.twinx()
    ts_flow = history["flow"]["timestamps"]
    vals_flow = history["flow"]["values"]
    if ts_flow:
        ax2.plot(ts_flow, vals_flow, color=COLORS["Flow"], linewidth=0.8)
    ax2.set_ylabel("Flow (mmol/s)", color=COLORS["Flow"])
    ax2.tick_params(axis="y", labelcolor=COLORS["Flow"])

    # [1,1] P3 + P4 (front) with P5 tank as a faint background reference.
    # P5 keeps its own right y-axis (tank pressure is orders of magnitude
    # above the cold-trap gauges); it is drawn on the twin at high
    # transparency, and the primary axis (P3/P4) is raised in front so its
    # traces sit on top of the faint P5 band.
    ax = axes[1, 1]
    ax2 = ax.twinx()
    if len(ts_pres) > 0:
        ax2.plot(ts_pres, pres[:, 4], color=COLORS["P5"], linewidth=1.2,
                 alpha=0.25, label="P5 (tank)")
    ax2.set_ylabel("P5 (mbar)", color=COLORS["P5"])
    ax2.tick_params(axis="y", labelcolor=COLORS["P5"])
    if len(ts_pres) > 0:
        ax.plot(ts_pres, pres[:, 2], color=COLORS["P3"], linewidth=0.8, label="P3 (after cold trap)")
        ax.plot(ts_pres, pres[:, 3], color=COLORS["P4"], linewidth=0.8, label="P4 (before cold trap)")
    ax.set_ylabel("P3, P4 (mbar)")
    ax.set_title("P3, P4 & P5", fontweight="bold")
    _format_ax(ax)
    # Primary (P3/P4) in front of the twin (faint P5 background).
    ax.set_zorder(ax2.get_zorder() + 1)
    ax.patch.set_visible(False)
    # One legend spanning both axes; keep its swatches opaque even though the
    # plotted P5 line is faint.
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    if h1 or h2:
        leg = ax.legend(h1 + h2, l1 + l2, fontsize=9)
        for lh in leg.legend_handles:
            lh.set_alpha(1.0)

    # --- Row 2: P6 (left) and P1 (right), each its own panel ---
    ax = axes[2, 0]
    if len(ts_pres) > 0:
        ax.plot(ts_pres, pres[:, 5], color=COLORS["P6"], linewidth=0.8)
    ax.set_ylabel("Pressure (mbar)")
    ax.set_title("P6 (manifold)", color=COLORS["P6"], fontweight="bold")
    ax.set_yscale("log")
    _format_ax(ax)

    ax = axes[2, 1]
    if len(ts_pres) > 0:
        # P1 (OVC) often has its gauge off -- a flat trace.  Detect that
        # (near-zero spread) and show an empty "sensor off" series instead
        # of a misleading flat line, preserving the old combined-panel logic.
        p1_vals = pres[:, 0]
        if np.std(p1_vals) > 1e-6:
            ax.plot(ts_pres, p1_vals, color=COLORS["P1"], linewidth=0.8, label="P1 (OVC)")
        else:
            ax.plot([], [], color=COLORS["P1"], linewidth=0.8, label="P1 (OVC - sensor off)")
        ax.legend(fontsize=9)
    ax.set_ylabel("Pressure (mbar)")
    ax.set_title("P1 (OVC)", color=COLORS["P1"], fontweight="bold")
    ax.set_yscale("log")
    _format_ax(ax)

    # --- Row 3: compressor temperatures over the report window ---
    comp = history.get("compressor", {})

    # [3,0] water + oil (same rendering as the standalone compressor plot)
    ax = axes[3, 0]
    ts, vals = comp.get("cpatempwi", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, label="Input water")
    ts, vals = comp.get("cpatempwo", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8, label="Output water")
    ts, vals = comp.get("cpatempo", ([], []))
    if ts: ax.plot(ts, vals, color="#E8A317", linewidth=0.8, label="Oil")
    ax.set_ylabel("Temperature (C)")
    ax.set_title("Compressor Water & Oil Temp", fontweight="bold")
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=9)
    _format_ax(ax)

    # [3,1] helium temperature
    ax = axes[3, 1]
    ts, vals = comp.get("cpatemph", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8)
    ax.set_ylabel("Temperature (C)")
    ax.set_title("Compressor Helium Temp", fontweight="bold")
    _format_ax(ax)

    with _RENDER_LOCK:
        fig.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    return output_path


def plot_compressor(history, output_path=None, subfolder=SUBFOLDER_ON_DEMAND):
    """Generate the 4-panel compressor plot. Returns path to PNG."""
    if output_path is None:
        out_dir = _ensure_subfolder(subfolder)
        output_path = os.path.join(out_dir, _window_filename("compressor", history))

    comp = history["compressor"]

    fig = Figure(figsize=(16, 10))
    FigureCanvasAgg(fig)
    axes = fig.subplots(2, 2)
    fig.suptitle(_report_title("BF4 Compressor Status", history),
                 fontsize=14, fontweight="bold")

    ax = axes[0, 0]
    ts, vals = comp.get("cpatempwi", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, label="Input water")
    ts, vals = comp.get("cpatempwo", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8, label="Output water")
    ts, vals = comp.get("cpatempo", ([], []))
    if ts: ax.plot(ts, vals, color="#E8A317", linewidth=0.8, label="Oil")
    ax.set_ylabel("Temperature (C)")
    ax.set_title("Water and Oil Temperatures", fontweight="bold")
    ax.legend(fontsize=9)
    _format_ax(ax)

    ax = axes[0, 1]
    ts, vals = comp.get("cpatemph", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8)
    ax.set_ylabel("Temperature (C)")
    ax.set_title("Helium Temperature", fontweight="bold")
    _format_ax(ax)

    ax = axes[1, 0]
    ts, vals = comp.get("cpalp", ([], []))
    if ts: ax.plot(ts, vals, color="#66AA22", linewidth=0.8, label="Low side")
    ts, vals = comp.get("cpahp", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, label="High side")
    ax.set_ylabel("Pressure (psi)")
    ax.set_title("Compressor Pressures", fontweight="bold")
    ax.legend(fontsize=9)
    _format_ax(ax)

    ax = axes[1, 1]
    ts, vals = comp.get("cpacurrent", ([], []))
    if ts: ax.plot(ts, vals, color="#22AAAA", linewidth=0.8)
    ax.set_ylabel("Current (A)")
    ax.set_title("Motor Current", fontweight="bold")
    _format_ax(ax)

    with _RENDER_LOCK:
        fig.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    return output_path


def plot_turbo_pumps(history, output_path=None, subfolder=SUBFOLDER_ON_DEMAND):
    """Generate the turbo pump status plot (1 row x 3 columns). Returns path to PNG."""
    if output_path is None:
        out_dir = _ensure_subfolder(subfolder)
        output_path = os.path.join(out_dir, _window_filename("turbo", history))

    turbo = history.get("turbo", {})

    fig = Figure(figsize=(20, 6))
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 3)
    fig.suptitle(_report_title("BF4 Turbo Pump Status", history),
                 fontsize=14, fontweight="bold")

    # Panel 1: Rotational speed
    ax = axes[0]
    ts, vals = turbo.get("tc400actualspd", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, label="Turbo 1")
    ts, vals = turbo.get("tc400actualspd_2", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8, label="Turbo 2")
    ax.set_ylabel("Speed (Hz)")
    ax.set_title("Rotational Frequency", fontweight="bold")
    ax.legend(fontsize=9)
    _format_ax(ax)

    # Panel 2: Drive power
    ax = axes[1]
    ts, vals = turbo.get("tc400drvpower", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, label="Turbo 1")
    ts, vals = turbo.get("tc400drvpower_2", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8, label="Turbo 2")
    ax.set_ylabel("Power (W)")
    ax.set_title("Drive Power", fontweight="bold")
    ax.legend(fontsize=9)
    _format_ax(ax)

    # Panel 3: Status + overtemp combined (On/Off y-axis)
    ax = axes[2]
    on_off_labels = {0: "Off", 1: "On"}
    ts, vals = turbo.get("tc400pumpstatn", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=1.5, label="T1 running")
    ts, vals = turbo.get("tc400pumpstatn_2", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=1.5, label="T2 running")
    ts, vals = turbo.get("tc400ovtempelec", ([], []))
    if ts: ax.plot(ts, vals, color="#2255CC", linewidth=0.8, linestyle="--", label="T1 overtemp elec")
    ts, vals = turbo.get("tc400ovtemppum", ([], []))
    if ts: ax.plot(ts, vals, color="#66AA22", linewidth=0.8, linestyle="--", label="T1 overtemp pump")
    ts, vals = turbo.get("tc400ovtempelec_2", ([], []))
    if ts: ax.plot(ts, vals, color="#CC3333", linewidth=0.8, linestyle="--", label="T2 overtemp elec")
    ts, vals = turbo.get("tc400ovtemppum_2", ([], []))
    if ts: ax.plot(ts, vals, color="#E8A317", linewidth=0.8, linestyle="--", label="T2 overtemp pump")
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Off", "On"])
    ax.set_ylim(-0.1, 1.1)
    ax.set_title("Status & Overtemp Warnings", fontweight="bold")
    ax.legend(fontsize=8, loc="center right")
    _format_ax(ax)

    with _RENDER_LOCK:
        fig.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    return output_path


def plot_alarm_data(timestamps, values, variable_name, threshold, unit,
                    output_path=None, alarm_name=None, trigger_time=None):
    """
    Generate a single-panel plot for an alarm event.  Returns path to PNG.

    ``alarm_name`` (e.g. "P4_high") is preferred for filenames; falls back
    to ``variable_name`` if not supplied.  ``trigger_time`` defaults to now.
    """
    if output_path is None:
        out_dir = _ensure_subfolder(SUBFOLDER_ALARMS)
        output_path = os.path.join(
            out_dir,
            _alarm_filename(alarm_name, variable_name, trigger_time),
        )

    fig = Figure(figsize=(10, 5))
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    ax.plot(timestamps, values, color="#CC3333", linewidth=1.0)
    ax.axhline(y=threshold, color="#888888", linestyle="--", linewidth=1.0, label=f"Threshold: {threshold} {unit}")
    ax.set_ylabel(f"{variable_name} ({unit})")
    ax.set_title(f"ALARM: {variable_name} exceeded {threshold} {unit}", fontweight="bold", color="#CC3333")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="x", rotation=45)

    with _RENDER_LOCK:
        fig.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    return output_path
