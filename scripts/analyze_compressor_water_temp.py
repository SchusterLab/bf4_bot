"""Analyse compressor cooling-water temperatures from the Status logs.

READ-ONLY: only opens Status_*.log files (via FridgeReader.read_status_log).
No API access, no writes to the fridge.

Prints a percentile summary for the outlet (cpatempwo), inlet (cpatempwi),
oil (cpatempo) and helium (cpatemph) temperatures over the past N days, and
saves a two-panel histogram of the OUTLET water temp to
docs/analysis/outlet_water_temp_histogram.png.

This backs the T_CompressorWaterOut high-temp alarm (issue #26): re-run it to
refresh the "what is normal?" picture without re-deriving it by hand.

Usage (from repo root, in the project venv):
    python scripts/analyze_compressor_water_temp.py [DAYS]
DAYS defaults to 30.
"""
import os
import sys
from datetime import date, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Repo root = parent of this scripts/ dir; put it on the path so "bf4bot"
# imports work regardless of the current working directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from bf4bot.reader import FridgeReader  # noqa: E402

KEYS = ["cpatempwo", "cpatempwi", "cpatempo", "cpatemph"]
LABELS = {
    "cpatempwo": "outlet water",
    "cpatempwi": "inlet water",
    "cpatempo": "oil",
    "cpatemph": "helium",
}
PCTS = [0, 1, 5, 25, 50, 75, 90, 95, 99, 99.9, 100]


def percentile(sorted_vals, pct):
    """Linear-interpolated percentile (numpy-free)."""
    if not sorted_vals:
        return float("nan")
    if pct <= 0:
        return sorted_vals[0]
    if pct >= 100:
        return sorted_vals[-1]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])
    return sorted_vals[lo]


def main(days):
    # api_key is unused for historical log reads; pass a placeholder so
    # __init__ does not try to open api_key.txt.
    reader = FridgeReader(api_key="unused-log-reads-only")

    end_day = date.today()
    start_day = end_day - timedelta(days=days)

    series = {k: [] for k in KEYS}
    day = start_day
    while day <= end_day:
        data = reader.read_status_log(day, KEYS)
        for k in KEYS:
            series[k].extend(data[k][1])
        day += timedelta(days=1)

    n = len(series["cpatempwo"])
    print(f"Window: {start_day} .. {end_day}  ({days} days)")
    print(f"Samples (cpatempwo): {n:,}\n")
    if n == 0:
        print("No data in window -- is the S: drive mapped?")
        return 1

    hdr = "  ".join(f"p{p:g}" for p in PCTS)
    for k in KEYS:
        vals = sorted(series[k])
        m = len(vals)
        if m == 0:
            print(f"{k:>10} ({LABELS[k]}): NO DATA")
            continue
        mean = sum(vals) / m
        std = (sum((v - mean) ** 2 for v in vals) / m) ** 0.5
        pcts = "  ".join(f"{percentile(vals, p):.2f}" for p in PCTS)
        print(f"{k} ({LABELS[k]}, C): mean={mean:.2f} std={std:.2f} "
              f"min={vals[0]:.2f} max={vals[-1]:.2f}")
        print(f"    {hdr}")
        print(f"    {pcts}\n")

    # --- histogram of the OUTLET water temp ---
    vals = series["cpatempwo"]
    thresholds = [(30, "#eda100"), (32, "#008300"), (35, "#d03b3b")]
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13, 5))

    lo_c = int(min(vals))
    axL.hist(vals, bins=[lo_c + 0.1 * i for i in range(int((30 - lo_c) / 0.1) + 1)],
             color="#2a78d6", edgecolor="white", linewidth=0.3)
    axL.set_xlim(lo_c, 30)
    axL.set_title(f"Core distribution (linear y)\nn={n:,} samples over {days} days")
    axL.set_xlabel("outlet water temp (C)")
    axL.set_ylabel("samples per 0.1 C bin")

    hi_c = int(max(vals)) + 2
    axR.hist(vals, bins=[lo_c + 0.25 * i for i in range(int((hi_c - lo_c) / 0.25) + 1)],
             color="#2a78d6", edgecolor="white", linewidth=0.3)
    axR.set_yscale("log")
    axR.set_xlim(lo_c, hi_c)
    axR.set_title("Full range incl. tail (log y)")
    axR.set_xlabel("outlet water temp (C)")
    axR.set_ylabel("samples (log)")
    ymax = axR.get_ylim()[1]
    for thr, col in thresholds:
        frac = 100.0 * sum(1 for v in vals if v > thr) / n
        axR.axvline(thr, color=col, linestyle="--", linewidth=1.4)
        axR.text(thr, ymax * 0.5, f"{thr}C\n{frac:.3f}% >", color=col,
                 fontsize=8, ha="center")

    fig.suptitle("Compressor OUTLET water temperature (cpatempwo) -- past "
                 f"{days} days ({start_day} to {end_day})",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])

    out_dir = os.path.join(REPO_ROOT, "docs", "analysis")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "outlet_water_temp_histogram.png")
    fig.savefig(out, dpi=130)
    print(f"Saved histogram: {out}")
    return 0


if __name__ == "__main__":
    days_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    sys.exit(main(days_arg))
