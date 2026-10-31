# BF4 Fridge Monitoring Bot

from datetime import datetime


def parse_duration(parts, default_hours=24):
    """
    Parse a duration argument from command parts into a float number of hours.

    Accepted forms (second token of `parts`):
        "30min" / "30m"  -> 0.5     (minutes)
        "24h"            -> 24.0    (hours)
        "7d"             -> 168.0   (days)
        "48"             -> 48.0    (bare number = hours)

    Returns `default_hours` (as a float) if the token is missing or unparseable.
    """
    if len(parts) < 2:
        return float(default_hours)
    arg = parts[1].strip().lower()
    try:
        if arg.endswith("min"):
            return float(arg[:-3]) / 60.0
        if arg.endswith("d"):
            return float(arg[:-1]) * 24.0
        if arg.endswith("h"):
            return float(arg[:-1])
        if arg.endswith("m"):  # "m" alone = minutes (checked AFTER "min")
            return float(arg[:-1]) / 60.0
        return float(arg)  # bare number = hours
    except ValueError:
        return float(default_hours)


def format_duration(hours):
    """
    Format a float number of hours as a short human label.

    Examples:
        0.5   -> "30min"
        1.0   -> "1h"
        24.0  -> "24h"
        48.0  -> "2d"
        168.0 -> "7d"
        36.0  -> "36h"     (not a whole-day multiple)
        2.5   -> "2.5h"
    """
    if hours < 1:
        minutes = int(round(hours * 60))
        return f"{minutes}min"
    # Whole-day multiples render as days
    if hours >= 24 and abs(hours - round(hours / 24) * 24) < 1e-9:
        return f"{int(round(hours / 24))}d"
    # Whole hours render without a decimal point
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours))}h"
    return f"{hours:g}h"


def parse_at_time(token):
    """
    Parse a yymmdd[hh[mm]] timestamp token into a datetime.

    Accepted lengths (partial precision -- minimum is day):
        6  -> yymmdd            (hh and mm default to 00)
        8  -> yymmddhh          (mm defaults to 00)
        10 -> yymmddhhmm

    The two-digit year yy maps to 20yy, matching the log-file date
    convention in reader._parse_log_date.

    Raises ValueError with a user-facing message on any malformed input:
    wrong length, a non-digit character, or an impossible calendar
    date/time (e.g. month 13, day 32, hour 25).
    """
    usage = ("Use yymmdd, yymmddhh, or yymmddhhmm (digits only), "
             "e.g. 2607141530 for 2026-07-14 15:30.")
    if token is None:
        raise ValueError(f"Missing timestamp. {usage}")
    s = str(token).strip()
    # isascii() matters: str.isdigit() also returns True for non-ASCII
    # "digit" characters (e.g. Unicode superscripts) that int() CANNOT
    # parse.  Without this guard such a token passes the length check and
    # then int() raises a raw "invalid literal for int()" instead of the
    # friendly message below.  It also keeps parsing ASCII-only, matching
    # the machine's cp1252 constraint.
    if not (s.isascii() and s.isdigit()) or len(s) not in (6, 8, 10):
        # Echo an ASCII-safe rendering: a non-ASCII token (e.g. Unicode
        # superscripts) must not ride into a cp1252 print()/log later.
        shown = s if s.isascii() else s.encode("ascii", "replace").decode("ascii")
        raise ValueError(f"Bad timestamp '{shown}'. {usage}")
    # Keep the int() conversions inside the try so ANY parse failure
    # (not just the datetime range check) surfaces as the friendly error.
    try:
        year = 2000 + int(s[0:2])
        month = int(s[2:4])
        day = int(s[4:6])
        hour = int(s[6:8]) if len(s) >= 8 else 0
        minute = int(s[8:10]) if len(s) >= 10 else 0
        return datetime(year, month, day, hour, minute)
    except ValueError as e:
        raise ValueError(f"Bad timestamp '{token}': {e}. {usage}")


def parse_report_request(parts, default_hours=24):
    """
    Parse an on-demand report command into (hours, anchor).

    ``parts`` is the whitespace-split command word list, e.g.
        ["report", "6h", "at", "2607141530"]
        ["compressor", "at", "260714"]
        ["turbo", "12h"]
        ["report"]

    Returns (hours_float, anchor_datetime_or_None):
      * hours comes from parse_duration (the token right after the command
        word); missing/unparseable -> default_hours.
      * anchor is the datetime after an "at" keyword, or None if "at" is
        absent (a normal "window ending now" report).

    Raises ValueError (with a user-facing message) when:
      * "at" is present but the following timestamp is missing or malformed;
      * a timestamp-shaped token (all-digit, length 6/8/10) appears WITHOUT
        a preceding "at" -- the natural "forgot the keyword" mistake, which
        would otherwise be read as billions of hours (OverflowError deep in
        read_history) or silently dropped, yielding the wrong window.

    The period token is still read from position 1, so "report at 260714"
    (no period) uses the default period and anchors at the given day.
    """
    lowered = [p.lower() for p in parts]
    at_idx = lowered.index("at") if "at" in lowered else None
    ts_idx = at_idx + 1 if at_idx is not None else None

    # Guard the "forgot 'at'" mistakes ("report 2607141530",
    # "report 6h 2607141530") and a timestamp wrongly placed in the period
    # slot.  An all-digit 6/8/10-length token is a timestamp shape and never
    # a sane duration.  The single token right after "at" is exempt -- that
    # IS the anchor and parse_at_time validates it below.
    for j, tok in enumerate(parts):
        if j == 0 or j == ts_idx:
            continue
        t = tok.strip()
        if t.isascii() and t.isdigit() and len(t) in (6, 8, 10):
            raise ValueError(
                f"Timestamp '{t}' needs 'at' before it. "
                f"Usage: report [period] at {t}"
            )

    hours = parse_duration(parts, default_hours=default_hours)
    anchor = None
    if at_idx is not None:
        if ts_idx >= len(parts):
            raise ValueError(
                "Missing timestamp after 'at'. "
                "Usage: report [period] at yymmddhhmm"
            )
        anchor = parse_at_time(parts[ts_idx])
    return hours, anchor


def history_is_empty(history, kind):
    """
    True if a report window contains NO samples for the given report kind.

    ``kind`` is one of "fridge", "compressor", "turbo".  Used to warn the
    user when an at-time window falls outside the range covered by the log
    files (so the plot comes back with empty panels).
    """
    if kind == "fridge":
        temps = history.get("temps", {})
        if any(temps[s].get("timestamps") for s in temps):
            return False
        if history.get("pressures", {}).get("timestamps"):
            return False
        if history.get("flow", {}).get("timestamps"):
            return False
        return True
    if kind == "compressor":
        comp = history.get("compressor", {})
        return not any(ts for ts, _ in comp.values())
    if kind == "turbo":
        turbo = history.get("turbo", {})
        return not any(ts for ts, _ in turbo.values())
    return False
