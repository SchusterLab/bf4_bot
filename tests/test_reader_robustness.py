"""
Tests for bf4bot.reader -- robustness against corrupt log lines and
filesystem errors (issue #3).

Before the fix, ANY malformed line in a log file (truncated write,
filesystem hiccup, encoding surprise) would raise ValueError/IndexError
up through read_history() and kill the entire scheduled report.

After the fix:
  * Bad lines are skipped and logged; report completes with whatever
    good lines remain in the file.
  * OSError on the file itself (PermissionError, drive disconnect) is
    logged and returns empty data; report still generates.

These tests create tmp log files with various corruptions and assert
the reader returns PARTIAL data and does not raise.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_reader_robustness.py -v
"""

import os
import sys
from datetime import date

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bf4bot.reader import FridgeReader  # noqa: E402


# --------------------------------------------------------------------------
# Helpers: build a log_prefix-shaped directory tree and a tiny reader.
# --------------------------------------------------------------------------


@pytest.fixture
def reader_factory(tmp_path):
    """Return a callable that gives us a FridgeReader pointed at tmp_path.

    The reader needs log_prefix to end with "/".  We also provide
    api_key='dummy' so it does not try to read api_key.txt.
    """
    def make():
        return FridgeReader(
            api_key="dummy",
            log_prefix=str(tmp_path) + os.sep,
        )
    return make


def _day_dir(tmp_path, target_date):
    """Create the day-folder the reader expects (tmp_path/YY-MM-DD/) and return it."""
    date_str = target_date.strftime("%y-%m-%d")
    d = tmp_path / date_str
    d.mkdir(exist_ok=True)
    return d, date_str


# --------------------------------------------------------------------------
# Temperature log
# --------------------------------------------------------------------------


def test_temperature_log_skips_bad_lines(reader_factory, tmp_path):
    """One corrupt line in the middle should not kill the whole read."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"CH5 T {date_str}.log"
    # 3 good lines, 1 corrupt line, 1 good line.
    log.write_text(
        "17-04-26,12:00:00,4.2\n"
        "17-04-26,12:01:00,4.3\n"
        "17-04-26,12:02:00,4.4\n"
        "GARBAGE-CORRUPT-LINE\n"
        "17-04-26,12:03:00,4.5\n",
        encoding="utf-8",
    )

    ts, vals = reader_factory().read_temperature_log(target, channel=5)
    assert len(ts) == 4, "4 valid lines should be parsed, corrupt one skipped"
    assert vals == [4.2, 4.3, 4.4, 4.5]


def test_temperature_log_skips_bad_float(reader_factory, tmp_path):
    """A non-numeric value in the value column is skipped, not raised."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"CH5 T {date_str}.log"
    log.write_text(
        "17-04-26,12:00:00,4.2\n"
        "17-04-26,12:01:00,not-a-number\n"
        "17-04-26,12:02:00,4.4\n",
        encoding="utf-8",
    )

    ts, vals = reader_factory().read_temperature_log(target, channel=5)
    assert vals == [4.2, 4.4]


def test_temperature_log_missing_file_silent(reader_factory):
    """A day with no log file should return empty, not raise."""
    target = date(2026, 1, 1)  # no day-folder exists
    ts, vals = reader_factory().read_temperature_log(target, channel=5)
    assert ts == []
    assert vals == []


def test_temperature_log_permission_error(reader_factory, tmp_path, monkeypatch):
    """PermissionError on the log file is logged but does NOT raise."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"CH5 T {date_str}.log"
    log.write_text("17-04-26,12:00:00,4.2\n", encoding="utf-8")

    # Simulate a drive that raises PermissionError on open().  Match by
    # filename substring (not full path) because the reader builds paths
    # with forward slashes while pathlib uses backslashes on Windows.
    real_open = open

    def blocked_open(path, *args, **kwargs):
        if "CH5 T" in str(path):
            raise PermissionError("access denied (simulated)")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", blocked_open)

    # Should NOT raise.
    ts, vals = reader_factory().read_temperature_log(target, channel=5)
    assert ts == []
    assert vals == []


# --------------------------------------------------------------------------
# Pressure log (>35 columns expected)
# --------------------------------------------------------------------------


def test_pressure_log_skips_short_lines(reader_factory, tmp_path):
    """A truncated line with too few columns is skipped."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"maxigauge {date_str}.log"
    # Build one valid line (36 columns, indices 5/11/17/23/29/35 hold
    # floats), plus one truncated line.
    good_cols = (
        ["17-04-26", "12:00:00"]
        + [str(i * 0.1) for i in range(34)]
    )
    good_line = ",".join(good_cols)
    assert len(good_cols) == 36

    log.write_text(good_line + "\n" + "17-04-26,12:01:00,short\n",
                   encoding="utf-8")

    ts, vals = reader_factory().read_pressure_log(target)
    assert len(ts) == 1, "only the full-width line should parse"
    assert len(vals[0]) == 6


def test_pressure_log_skips_corrupt_column(reader_factory, tmp_path):
    """A non-numeric value in a pressure column is skipped, not raised."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"maxigauge {date_str}.log"

    # Corrupt line: column 5 is "NaN-not-a-number"
    bad_cols = ["17-04-26", "12:00:00"] + ["0.1"] * 34
    bad_cols[5] = "NaN-not-a-number"
    bad_line = ",".join(bad_cols)

    good_cols = ["17-04-26", "12:01:00"] + [str(i * 0.01) for i in range(34)]
    good_line = ",".join(good_cols)

    log.write_text(bad_line + "\n" + good_line + "\n", encoding="utf-8")

    ts, vals = reader_factory().read_pressure_log(target)
    assert len(ts) == 1, "only the good line should parse"


# --------------------------------------------------------------------------
# Status log (key/value pairs; outer try/except + inner try/except both exist)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Compressor / PulseTube state (issue #5) -- explicit value checks.
# --------------------------------------------------------------------------


class _StubReader(FridgeReader):
    """FridgeReader whose api_read is replaced with a canned response.

    We never want a real HTTP call during tests.
    """

    def __init__(self, response):
        # Skip FridgeReader.__init__ -- we don't need api_key etc.
        self.api_host = "stub"
        self.api_port = "0"
        self.log_prefix = ""
        self.api_key = "dummy"
        self._canned = response

    def api_read(self, path):
        return self._canned


def test_compressor_value_zero_returns_false():
    """Integer 0 (compressor OFF) must return False, not None."""
    r = _StubReader({"value": 0, "status": "SYNCHRONIZED", "read_only": True})
    val, status = r.get_compressor_state()
    assert val is False
    assert status == "SYNCHRONIZED"


def test_compressor_value_string_zero_returns_false():
    """String '0' should behave the same as int 0."""
    r = _StubReader({"value": "0", "status": "SYNCHRONIZED", "read_only": True})
    val, _ = r.get_compressor_state()
    assert val is False


def test_compressor_value_one_returns_true():
    """Running compressor reports True."""
    r = _StubReader({"value": "1", "status": "SYNCHRONIZED", "read_only": True})
    val, _ = r.get_compressor_state()
    assert val is True


def test_compressor_empty_value_returns_none():
    """Empty string = no reading available."""
    r = _StubReader({"value": "", "status": "SYNCHRONIZED", "read_only": True})
    val, _ = r.get_compressor_state()
    assert val is None


def test_compressor_none_value_returns_none():
    """None value = no reading available."""
    r = _StubReader({"value": None, "status": "SYNCHRONIZED", "read_only": True})
    val, _ = r.get_compressor_state()
    assert val is None


def test_compressor_invalid_status_returns_none():
    """Status INVALID must return None even if the value looks fresh.

    Guards against reporting a stale cached reading as real -- the
    compressor alarm should not trust a value flagged INVALID.
    """
    r = _StubReader({"value": "1", "status": "INVALID", "read_only": True})
    val, status = r.get_compressor_state()
    assert val is None
    assert status == "INVALID"


def test_pulse_tube_value_zero_returns_false():
    """Same robustness check for get_pulse_tube."""
    r = _StubReader({"value": 0, "status": "SYNCHRONIZED", "read_only": True})
    val, _ = r.get_pulse_tube()
    assert val is False


def test_pulse_tube_invalid_status_returns_none():
    """Pulse tube honours INVALID status too."""
    r = _StubReader({"value": "1", "status": "INVALID", "read_only": True})
    val, _ = r.get_pulse_tube()
    assert val is None


def test_status_log_skips_bad_outer_line(reader_factory, tmp_path):
    """Bad date in the outer parse should skip line, not crash reader."""
    target = date(2026, 4, 17)
    d, date_str = _day_dir(tmp_path, target)
    log = d / f"Status_{date_str}.log"

    # First line: bad date format (should raise ValueError in
    # _parse_log_date -> caught by outer try/except now).
    # Second line: valid.
    log.write_text(
        "BADDATE,12:00:00,cpatempwi,42.0\n"
        "17-04-26,12:01:00,cpatempwi,43.0\n",
        encoding="utf-8",
    )

    result = reader_factory().read_status_log(target, keys=["cpatempwi"])
    ts, vals = result["cpatempwi"]
    assert len(ts) == 1
    assert vals == [43.0]
