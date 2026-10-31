"""
Tests for the at-time report feature (issue #25).

Covers:
  * parse_at_time        -- yymmdd[hh[mm]] parsing, defaults, rejects.
  * parse_report_request -- period + optional "at <time>" anchor.
  * history_is_empty     -- empty-window detection per report kind.
  * read_history         -- fixed `end`/`anchor` window with a REAL upper
                            bound (a PAST window must not leak same-day
                            samples recorded AFTER the window ends -- the
                            core correctness point of the feature).

Run:
    .venv/Scripts/python.exe -m pytest tests/test_report_at_time.py -v
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bf4bot import (  # noqa: E402
    parse_at_time, parse_report_request, history_is_empty,
)
from bf4bot.reader import FridgeReader  # noqa: E402


# --------------------------------------------------------------------------
# parse_at_time
# --------------------------------------------------------------------------


def test_parse_at_time_full():
    assert parse_at_time("2607141530") == datetime(2026, 7, 14, 15, 30)


def test_parse_at_time_hour_precision():
    """8 digits -> minute defaults to 00."""
    assert parse_at_time("26071415") == datetime(2026, 7, 14, 15, 0)


def test_parse_at_time_day_precision():
    """6 digits -> hour and minute both default to 00."""
    assert parse_at_time("260714") == datetime(2026, 7, 14, 0, 0)


def test_parse_at_time_strips_whitespace():
    assert parse_at_time("  260714  ") == datetime(2026, 7, 14, 0, 0)


@pytest.mark.parametrize("bad", ["2607", "260714153", "26071", "", "12345"])
def test_parse_at_time_bad_length(bad):
    """Only 6/8/10 digit lengths are accepted."""
    with pytest.raises(ValueError):
        parse_at_time(bad)


def test_parse_at_time_non_digit():
    with pytest.raises(ValueError):
        parse_at_time("26071x1530")


def test_parse_at_time_unicode_digits_friendly():
    """str.isdigit() accepts Unicode superscripts that int() cannot parse.

    The error must still be the FRIENDLY 'Bad timestamp' message, not a
    raw 'invalid literal for int()'.  The token is built from \\u escapes
    so THIS source file stays pure ASCII (cp1252 machine constraint).
    """
    # 6 superscript "digits"; built via chr() so this source stays ASCII.
    token = "".join(chr(c) for c in (0x00b2, 0x00b3, 0x00b9,
                                     0x2070, 0x2074, 0x2075))
    assert token.isdigit() and len(token) == 6   # sanity: passes old gate
    with pytest.raises(ValueError) as ei:
        parse_at_time(token)
    msg = str(ei.value)
    assert "Bad timestamp" in msg
    assert "invalid literal" not in msg
    # Message must be ASCII-safe so it can never crash a cp1252 print/log.
    assert msg.isascii()


@pytest.mark.parametrize("bad", ["261301", "260732", "2607142560", "2607141399"])
def test_parse_at_time_impossible(bad):
    """month 13, day 32, hour 25, minute 99 -> ValueError."""
    with pytest.raises(ValueError):
        parse_at_time(bad)


# --------------------------------------------------------------------------
# parse_report_request
# --------------------------------------------------------------------------


def test_request_period_and_anchor():
    hours, anchor = parse_report_request(["report", "6h", "at", "2607141530"])
    assert hours == 6.0
    assert anchor == datetime(2026, 7, 14, 15, 30)


def test_request_anchor_no_period_defaults_24h():
    """'report at 260714' -> default period, anchored on that day."""
    hours, anchor = parse_report_request(["report", "at", "260714"])
    assert hours == 24.0
    assert anchor == datetime(2026, 7, 14, 0, 0)


def test_request_period_no_anchor():
    hours, anchor = parse_report_request(["turbo", "12h"])
    assert hours == 12.0
    assert anchor is None


def test_request_bare_command():
    hours, anchor = parse_report_request(["report"])
    assert hours == 24.0
    assert anchor is None


def test_request_missing_timestamp_raises():
    with pytest.raises(ValueError):
        parse_report_request(["report", "6h", "at"])


def test_request_bad_timestamp_raises():
    with pytest.raises(ValueError):
        parse_report_request(["report", "6h", "at", "nope"])


@pytest.mark.parametrize("parts", [
    ["report", "2607141530"],       # bare 10-digit timestamp, no 'at'
    ["report", "26071415"],         # bare 8-digit
    ["report", "260714"],           # bare 6-digit
    ["report", "6h", "2607141530"],  # period + trailing timestamp, no 'at'
    ["compressor", "260714"],
])
def test_request_timestamp_without_at_rejected(parts):
    """A timestamp-shaped token with no 'at' is rejected with a hint --
    NOT read as billions of hours (OverflowError) or silently ignored."""
    with pytest.raises(ValueError) as ei:
        parse_report_request(parts)
    assert "at" in str(ei.value).lower()


@pytest.mark.parametrize("parts,expected", [
    (["report", "48"], 48.0),        # bare number = hours still works
    (["report", "168"], 168.0),      # 3-digit duration
    (["report", "12h"], 12.0),
    (["report", "30min"], 0.5),
])
def test_request_bare_duration_preserved(parts, expected):
    """The guard must not break legitimate bare-number durations
    (only 6/8/10-length all-digit tokens are timestamp-shaped)."""
    hours, anchor = parse_report_request(parts)
    assert hours == expected
    assert anchor is None


# --------------------------------------------------------------------------
# history_is_empty
# --------------------------------------------------------------------------


def _empty_fridge():
    return {
        "temps": {"MXC": {"timestamps": [], "values": []}},
        "pressures": {"timestamps": [], "values": []},
        "flow": {"timestamps": [], "values": []},
    }


def test_history_is_empty_true():
    assert history_is_empty(_empty_fridge(), "fridge") is True


def test_history_is_empty_false_when_temps_present():
    h = _empty_fridge()
    h["temps"]["MXC"]["timestamps"] = [datetime(2026, 7, 14, 12, 0)]
    assert history_is_empty(h, "fridge") is False


def test_history_is_empty_false_when_flow_present():
    h = _empty_fridge()
    h["flow"]["timestamps"] = [datetime(2026, 7, 14, 12, 0)]
    assert history_is_empty(h, "fridge") is False


def test_history_is_empty_compressor():
    h = {"compressor": {"cpalp": ([], []), "cpahp": ([], [])}}
    assert history_is_empty(h, "compressor") is True
    h["compressor"]["cpalp"] = ([datetime(2026, 7, 14)], [1.0])
    assert history_is_empty(h, "compressor") is False


def test_history_is_empty_turbo():
    h = {"turbo": {"tc400actualspd": ([], [])}}
    assert history_is_empty(h, "turbo") is True
    h["turbo"]["tc400actualspd"] = ([datetime(2026, 7, 14)], [900.0])
    assert history_is_empty(h, "turbo") is False


# --------------------------------------------------------------------------
# read_history -- fixed window + REAL upper bound
# --------------------------------------------------------------------------


@pytest.fixture
def reader_factory(tmp_path):
    def make():
        return FridgeReader(api_key="dummy", log_prefix=str(tmp_path) + os.sep)
    return make


def _write_mxc_day(tmp_path, target_date, hours_range):
    """Write a CH6 (MXC) temp log with one sample at HH:00 for each hour.

    Folder + filename use the reader's yy-mm-dd convention; the log LINES
    use its DD-MM-YY convention (see reader._parse_log_date).
    """
    ds = target_date.strftime("%y-%m-%d")
    d = tmp_path / ds
    d.mkdir(exist_ok=True)
    log = d / f"CH6 T {ds}.log"
    line_date = target_date.strftime("%d-%m-%y")
    lines = [f"{line_date},{h:02d}:00:00,{0.01 + h * 0.001}"
             for h in hours_range]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_read_history_centered_window_bounds(reader_factory, tmp_path):
    """A past-anchored window keeps ONLY samples in [cutoff, end].

    This is the regression guard for the feature: before the upper-bound
    fix, samples recorded later that same day (16:00..23:00) would have
    leaked into a window ending at 15:00.
    """
    day = date(2026, 7, 14)
    _write_mxc_day(tmp_path, day, range(24))   # samples 00:00 .. 23:00
    anchor = datetime(2026, 7, 14, 12, 0)
    hours = 6
    end = anchor + timedelta(hours=hours / 2)  # centered -> ends at 15:00
    hist = reader_factory().read_history(hours=hours, end=end, anchor=anchor)

    ts = hist["temps"]["MXC"]["timestamps"]
    assert ts, "window should contain samples"
    # cutoff = end - 6h = 09:00 ; end = 15:00
    assert hist["cutoff"] == datetime(2026, 7, 14, 9, 0)
    assert hist["now"] == end
    assert hist["anchor"] == anchor
    assert min(ts) >= datetime(2026, 7, 14, 9, 0)
    assert max(ts) <= end
    # hours 09..15 inclusive kept; 16..23 excluded (upper bound), 00..08
    # excluded (lower bound).
    assert [t.hour for t in ts] == [9, 10, 11, 12, 13, 14, 15]


def test_read_history_window_spanning_midnight(reader_factory, tmp_path):
    """Window crossing a day boundary reads both day folders."""
    d1 = date(2026, 7, 14)
    d2 = date(2026, 7, 15)
    _write_mxc_day(tmp_path, d1, range(20, 24))   # 20:00..23:00
    _write_mxc_day(tmp_path, d2, range(0, 4))      # 00:00..03:00
    anchor = datetime(2026, 7, 15, 0, 0)
    hours = 4
    end = anchor + timedelta(hours=hours / 2)      # 02:00 on the 15th
    hist = reader_factory().read_history(hours=hours, end=end, anchor=anchor)
    ts = hist["temps"]["MXC"]["timestamps"]
    # cutoff = 22:00 on the 14th ; end = 02:00 on the 15th
    assert min(ts) == datetime(2026, 7, 14, 22, 0)
    assert max(ts) == datetime(2026, 7, 15, 2, 0)
    assert all(datetime(2026, 7, 14, 22, 0) <= t <= end for t in ts)


def test_read_history_default_now_has_none_anchor(reader_factory):
    """With no end/anchor, the window ends at now and anchor is None."""
    hist = reader_factory().read_history(hours=6)
    assert hist["anchor"] is None
    assert hist["cutoff"] < hist["now"]
