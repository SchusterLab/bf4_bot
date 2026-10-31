"""
Regression + feature tests for scheduled-report send resilience.

Covers three behaviours:
  1. A scheduled send that fails is NOT marked sent, so the poll loop
     retries it (originally a transient Slack failure at 9am silently
     dropped that day's report -- see report_state.json + issue #19).
  2. On failure the operator is DMed, throttled to once/(report,dest)/day.
  3. Per-destination retry: a report that reached some destinations but
     failed others retries ONLY the ones that did not get it, never
     re-sending to a destination already delivered today.

Plus ReportManager persistence: the per-destination state file round-trips
and the LEGACY single-date format is migrated on load.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_scheduled_report_retry.py -v
"""

import json
import os
import sys
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bf4bot.bot import BF4Bot           # noqa: E402
from bf4bot.config import ReportManager, ScheduledReport  # noqa: E402


# --------------------------------------------------------------------------
# check_scheduled_reports -- mark-on-success, per-destination, alerting
# --------------------------------------------------------------------------


def _report(name="Daily fridge report", dests=("operator_dm",)):
    """A real ScheduledReport that is always due (hour 0, daily)."""
    return ScheduledReport(name=name, destinations=list(dests), hour=0,
                           days="daily", hours=24, report_type="fridge")


class _FakeManager:
    """Mimics ReportManager's mark_sent (delegates to the report) and
    records which (name, dest) pairs were marked."""

    def __init__(self, reports):
        self.reports = reports
        self.marked = []

    def reload_if_changed(self):
        return False

    def mark_sent(self, report, dest, now):
        report.mark_sent(dest, now)
        self.marked.append((report.name, dest))


class _FakeSlack:
    def __init__(self):
        self.messages = []

    def send_message(self, channel, text):
        self.messages.append((channel, text))


def _make_bot(reports, send_impl):
    """Build a BF4Bot without __init__, wiring only what
    check_scheduled_reports touches.  ``send_impl(target)`` decides the
    outcome of each send (return True/False, or raise)."""
    bot = BF4Bot.__new__(BF4Bot)
    bot.report_mgr = _FakeManager(reports)
    bot._report_failure_notified = {}
    bot.slack = _FakeSlack()
    bot._resolve_one = lambda d: "D_OPERATOR" if d == "operator_dm" \
        else "C_" + d
    # report_type is "fridge" -> _send_scheduled_report calls this.
    bot.send_fridge_report = lambda **kw: send_impl(kw["channel_id"])
    return bot


def test_failed_send_not_marked_and_stays_pending():
    rep = _report()
    bot = _make_bot([rep], send_impl=lambda target: False)
    now = datetime.now()

    bot.check_scheduled_reports()

    assert bot.report_mgr.marked == []                 # nothing persisted
    assert rep.pending_destinations(now) == ["operator_dm"]  # still pending
    assert len(bot.slack.messages) == 1                # operator alerted once


def test_raising_send_not_marked():
    rep = _report()

    def _boom(target):
        raise RuntimeError("slack exploded")

    bot = _make_bot([rep], send_impl=_boom)
    now = datetime.now()

    bot.check_scheduled_reports()

    assert bot.report_mgr.marked == []
    assert rep.pending_destinations(now) == ["operator_dm"]


def test_successful_send_marked_once_no_resend():
    rep = _report()
    calls = []
    bot = _make_bot([rep],
                    send_impl=lambda target: calls.append(target) or True)

    bot.check_scheduled_reports()
    assert bot.report_mgr.marked == [("Daily fridge report", "operator_dm")]
    assert calls == ["D_OPERATOR"]

    # Second poll: already sent today -> no pending, no extra send/mark.
    bot.check_scheduled_reports()
    assert calls == ["D_OPERATOR"]
    assert bot.report_mgr.marked == [("Daily fridge report", "operator_dm")]


def test_retry_succeeds_on_second_poll_after_transient_failure():
    """The exact 9am-outage scenario: first poll fails, second succeeds."""
    rep = _report()
    state = {"n": 0}

    def _flaky(target):
        state["n"] += 1
        return state["n"] >= 2

    bot = _make_bot([rep], send_impl=_flaky)
    now = datetime.now()

    bot.check_scheduled_reports()          # fail
    assert bot.report_mgr.marked == []
    assert rep.pending_destinations(now) == ["operator_dm"]

    bot.check_scheduled_reports()          # succeed
    assert bot.report_mgr.marked == [("Daily fridge report", "operator_dm")]
    assert rep.pending_destinations(now) == []


def test_partial_failure_retries_only_failed_destination():
    rep = _report(dests=("bf4_channel", "fluxonium_kids_channel"))
    calls = []

    # bf4_channel succeeds; fluxonium fails on the first pass only.
    fail_fluxonium = {"active": True}

    def _send(target):
        calls.append(target)
        if target == "C_fluxonium_kids_channel":
            return not fail_fluxonium["active"]
        return True

    bot = _make_bot([rep], send_impl=_send)
    now = datetime.now()

    bot.check_scheduled_reports()
    # both attempted; only bf4_channel marked; fluxonium still pending
    assert calls == ["C_bf4_channel", "C_fluxonium_kids_channel"]
    assert bot.report_mgr.marked == [("Daily fridge report", "bf4_channel")]
    assert rep.pending_destinations(now) == ["fluxonium_kids_channel"]

    # Slack recovers; second poll retries ONLY fluxonium, never re-sends bf4.
    fail_fluxonium["active"] = False
    bot.check_scheduled_reports()
    assert calls == ["C_bf4_channel", "C_fluxonium_kids_channel",
                     "C_fluxonium_kids_channel"]
    assert bot.report_mgr.marked == [
        ("Daily fridge report", "bf4_channel"),
        ("Daily fridge report", "fluxonium_kids_channel"),
    ]
    assert rep.pending_destinations(now) == []


def test_failure_alert_throttled_once_per_day():
    rep = _report()
    bot = _make_bot([rep], send_impl=lambda target: False)

    bot.check_scheduled_reports()
    bot.check_scheduled_reports()
    bot.check_scheduled_reports()

    # Three failed polls -> still only ONE operator alert.
    assert len(bot.slack.messages) == 1
    chan, text = bot.slack.messages[0]
    assert chan == "D_OPERATOR"
    assert "Daily fridge report" in text


def test_clear_failure_allows_realert():
    """_clear_report_failure (called on a successful delivery) must reset the
    throttle so a later failure of the same dest alerts again."""
    rep = _report()
    bot = _make_bot([rep], send_impl=lambda target: False)
    now = datetime.now()

    bot._notify_report_failure(rep, "operator_dm", now)
    assert len(bot.slack.messages) == 1

    bot._clear_report_failure(rep.name, "operator_dm")
    bot._notify_report_failure(rep, "operator_dm", now)
    assert len(bot.slack.messages) == 2


# --------------------------------------------------------------------------
# ReportManager persistence -- per-destination round-trip + legacy migration
# --------------------------------------------------------------------------


def _write_reports_yaml(path, dests):
    if len(dests) == 1:
        dest_yaml = f"  destinations: {dests[0]}\n"
    else:
        dest_yaml = "  destinations:\n" + \
            "".join(f"  - {d}\n" for d in dests)
    path.write_text(
        "reports:\n"
        "- name: Daily fridge report\n"
        "  type: fridge\n"
        "  hour: 9\n"
        "  days: daily\n"
        "  hours: 24\n"
        + dest_yaml,
        encoding="utf-8",
    )


def test_new_format_state_round_trips(tmp_path):
    reports_file = tmp_path / "reports.yaml"
    state_file = tmp_path / "report_state.json"
    _write_reports_yaml(reports_file, ["bf4_channel", "operator_dm"])
    state_file.write_text(json.dumps({
        "Daily fridge report": {
            "bf4_channel": "2026-08-24",
            "operator_dm": "2026-08-25",
        }
    }), encoding="utf-8")

    mgr = ReportManager(reports_file=str(reports_file),
                        state_file=str(state_file))
    rep = mgr.reports[0]
    assert rep._last_sent_by_dest == {
        "bf4_channel": date(2026, 8, 24),
        "operator_dm": date(2026, 8, 25),
    }

    # A fresh mark persists, and re-reads identically.
    mgr.mark_sent(rep, "bf4_channel", datetime(2026, 8, 26, 9, 0))
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk["Daily fridge report"]["bf4_channel"] == "2026-08-26"
    assert on_disk["Daily fridge report"]["operator_dm"] == "2026-08-25"


def test_legacy_single_date_is_migrated_to_all_destinations(tmp_path):
    """Old {name: "date"} files must upgrade to per-destination state,
    treating the date as 'all current destinations sent that day' so the
    issue #19 restart guard survives the upgrade."""
    reports_file = tmp_path / "reports.yaml"
    state_file = tmp_path / "report_state.json"
    _write_reports_yaml(reports_file, ["bf4_channel", "operator_dm"])
    state_file.write_text(json.dumps({"Daily fridge report": "2026-08-25"}),
                          encoding="utf-8")

    mgr = ReportManager(reports_file=str(reports_file),
                        state_file=str(state_file))
    rep = mgr.reports[0]
    assert rep._last_sent_by_dest == {
        "bf4_channel": date(2026, 8, 25),
        "operator_dm": date(2026, 8, 25),
    }
    # Both destinations count as already sent that day.
    assert rep.pending_destinations(datetime(2026, 8, 25, 9, 0)) == []
