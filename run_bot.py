"""
BF4 Fridge Monitoring Bot -- Entry point.

Usage:
    python run_bot.py                  # Run the bot (Socket Mode + alarm polling)
    python run_bot.py --report         # Send one report immediately and exit
    python run_bot.py --compressor     # Send one compressor report and exit
    python run_bot.py --turbo          # Send one turbo pump report and exit
    python run_bot.py --status         # Print live fridge status and exit
"""

import argparse
import sys
import os
import threading
import time
import traceback
from datetime import datetime

# --------------------------------------------------------------------------
# Headless-launch log redirection + rotation.
#
# Under Task Scheduler + pythonw.exe there is no real console, so any
# ``print()`` must be redirected to a file.  We used to do that with a
# raw ``open(..., "a")`` which grew forever (issue #12).  Now we use
# ``bf4bot.logging_setup.install_log_shim`` -- it wires up a
# ``RotatingFileHandler`` (5 MB per file, 5 backups) and routes stdout
# and stderr through it, preserving every existing ``print()`` call.
#
# The helper also preserves the old pythonw-only guard: under a normal
# ``python run_bot.py`` dev run in a terminal, it does nothing and
# prints go to the terminal as before.  See ``bf4bot/logging_setup.py``
# for details.
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from bf4bot.logging_setup import install_log_shim  # noqa: E402

install_log_shim(os.path.join(_HERE, "bf4bot.log"))
_exec_name = os.path.basename(sys.executable).lower() if sys.executable else ""
print(f"\n[{datetime.now()}] --- bot starting (headless, exe={_exec_name}) ---")

# ---------------------------------------------------------------------------
# Cooperative shutdown flag.
#
# If bot_shutdown.flag exists, the bot exits immediately with code 0.
# Exit code 0 = "clean exit" = Task Scheduler does NOT trigger
# RestartOnFailure.  This is how the widget's STOP BOT button keeps the
# bot dead: it writes the flag, then kills the process.  If the scheduler
# wins the race and restarts, the bot immediately finds the flag and
# exits cleanly again.  No further restarts.
#
# The flag is checked BEFORE heavy imports (slack_bolt, matplotlib, etc.)
# so the re-started bot dies in under 1 second without loading anything.
# ---------------------------------------------------------------------------
_SHUTDOWN_FLAG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bot_shutdown.flag")
if os.path.exists(_SHUTDOWN_FLAG):
    print(f"[{datetime.now()}] Shutdown flag found. Exiting cleanly (code 0).")
    sys.exit(0)

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bf4bot import format_duration, parse_report_request
from bf4bot.bot import BF4Bot, HELP_TEXT
from bf4bot.reader import FridgeReader
from bf4bot.slack_client import BF4SlackClient
from bf4bot.config import load_keys, get_operator_user_id

KEYS = load_keys()
BOT_TOKEN = KEYS["slack_bot_token"]
APP_TOKEN = KEYS["slack_app_token"]
# Two-level operator lookup: channels.yaml has 'operator: <nickname>' and
# 'dm_users: {<nickname>: U...}'.  get_operator_user_id does both steps.
OPERATOR_USER_ID = get_operator_user_id(KEYS)
if not OPERATOR_USER_ID:
    raise SystemExit(
        "No operator configured.  Check config/channels.yaml -- it needs "
        "an 'operator:' line pointing at a key in dm_users."
    )


def get_dm_channel():
    """Get the DM channel ID for the bot operator."""
    slack = BF4SlackClient(token=BOT_TOKEN)
    response = slack.client.conversations_open(users=[OPERATOR_USER_ID])
    return response["channel"]["id"]


def print_status():
    """Print current fridge status to console."""
    reader = FridgeReader(api_key=KEYS["bluefors_api_key"])
    snapshot = reader.get_all_live()
    print("BF4 Live Status:")
    print("-" * 40)
    for key, data in snapshot.items():
        val = data["value"]
        unit = data["unit"]
        status = data["status"]
        if val is None:
            print(f"  {key}: -- ({status})")
        elif isinstance(val, bool):
            print(f"  {key}: {'YES' if val else 'NO'} ({status})")
        elif isinstance(val, float):
            print(f"  {key}: {val:.4g} {unit} ({status})")
        else:
            print(f"  {key}: {val} {unit} ({status})")


def run_daemon():
    """Run the full bot: Socket Mode for DM commands + background alarm polling."""
    dm_channel = get_dm_channel()
    bot = BF4Bot(channel_id=dm_channel)

    # --- Socket Mode app for instant DM responses ---
    app = App(token=BOT_TOKEN)

    # Human noun per command word, for the "Generating ..." acknowledgement.
    _REPORT_NOUNS = {"report": "fridge", "compressor": "compressor",
                     "turbo": "turbo"}

    def _run_report_command(cmd, parts, channel, thread_ts, notify):
        """Shared report dispatch for BOTH DM and @mention.

        ``cmd`` is one of report/compressor/turbo; ``notify`` posts an
        acknowledgement string (``say`` for DMs, the threaded ``reply`` for
        mentions).  Parses the period + optional ``at <time>`` anchor, then
        routes through the single bot.send_*_report path -- so DM and channel
        behave identically.  This shared path is what stops the at-time
        feature from diverging between handlers (the class of bug behind #24).
        """
        try:
            hours, anchor = parse_report_request(parts)
        except ValueError as e:
            notify(f":warning: {e}")
            return
        phrase = format_duration(hours)
        if anchor is not None:
            phrase += f" around {anchor.strftime('%Y-%m-%d %H:%M')}"
        notify(f"Generating {phrase} {_REPORT_NOUNS[cmd]} report...")
        sender = {
            "report": bot.send_fridge_report,
            "compressor": bot.send_compressor_report,
            "turbo": bot.send_turbo_report,
        }[cmd]
        sender(hours=hours, anchor=anchor, channel_id=channel,
               thread_ts=thread_ts)

    def is_operator(message):
        """True when the DM sender is the operator from channels.yaml."""
        return message.get("user") == OPERATOR_USER_ID

    def is_bot_message(message):
        """True if the message was sent by a bot (any bot, including us)."""
        # Slack Bolt sets "bot_id" on bot-authored messages.  We want to
        # ignore those to avoid the bot responding to its own output.
        return bool(message.get("bot_id")) or message.get("subtype") == "bot_message"

    @app.message("")
    def handle_dm(message, say):
        """
        Handle DMs.  Fridge-status / report commands are available to ANY
        user who DMs the bot (so collaborators can check in without needing
        operator access).  Operator-only features are gated separately.
        """
        if is_bot_message(message):
            return  # never respond to bots, including ourselves

        # Keep the raw (case-preserving) text for arguments that are names
        # (like alarm names, which can be "P4_high").  Only the command word
        # itself is lowercased for case-insensitive matching.
        raw_text = message.get("text", "").strip()
        if not raw_text:
            return
        raw_parts = raw_text.split()
        cmd = raw_parts[0].lower()
        parts = [p.lower() for p in raw_parts]

        # The DM channel this message arrived on.  Any reply/report we
        # generate should go BACK to this channel -- not to the operator's
        # DM -- so non-operator users get their own answers.
        dm_channel = message.get("channel")
        sender = message.get("user")
        is_op = sender == OPERATOR_USER_ID
        print(f"[{datetime.now()}] DM from {sender} "
              f"({'operator' if is_op else 'other'}): {raw_text}")

        # Whole-handler guard: ANY uncaught exception from a DM command
        # would otherwise propagate up to slack_bolt and could leave the
        # user's DM hanging with no reply.  Catch, log the traceback
        # (goes to bf4bot.log), and tell the user the command failed.
        try:
            if cmd == "status":
                _reply_live_status(dm_channel)
            elif cmd in ("report", "compressor", "turbo"):
                # DMs are not threaded -> thread_ts=None.
                _run_report_command(cmd, parts, dm_channel, None, say)
            elif cmd in ("list-alarms", "alarms"):
                say(bot.alarm_mgr.list_alarms())
            elif cmd in ("list-reports", "reports", "report-list"):
                say(_list_reports())
            elif cmd == "suppress":
                name = raw_parts[1] if len(raw_parts) > 1 else None
                say(bot.suppress_alarm(name, dm_channel))
            elif cmd == "unsuppress":
                name = raw_parts[1] if len(raw_parts) > 1 else None
                say(bot.unsuppress_alarm(name, dm_channel))
            elif cmd == "help":
                say(HELP_TEXT)
            else:
                say(f"Unknown command: `{raw_text}`\n"
                    f"Type `help` for available commands.")
        except Exception as e:
            print(f"[{datetime.now()}] DM command '{cmd}' from {sender} "
                  f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
            # Best-effort reply -- if this itself fails we just give up.
            try:
                say(f":warning: Command `{cmd}` failed: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _list_reports():
        """Return a formatted string of all scheduled reports."""
        reports = bot.report_mgr.reports
        if not reports:
            return "No scheduled reports configured."
        lines = ["*Scheduled reports:*"]
        for r in reports:
            dest = ", ".join(r.destinations) if r.destinations else "(default)"
            lines.append(
                f"{r.name}: {r.report_type} at {r.hour}:00 "
                f"({r.days}), {r.hours}h window -> {dest}")
        return "\n".join(lines)

    def _reply_live_status(target_channel):
        """Send a formatted live-status message to the given channel."""
        try:
            snapshot = bot.reader.get_all_live()
        except Exception as e:
            bot.slack.send_message(target_channel, f"Could not read fridge: {e}")
            return
        lines = ["*BF4 Live Status:*", ""]
        lines.append("*Temperatures:*")
        for key in ["T_MXC", "T_Still", "T_4K", "T_50K"]:
            d = snapshot[key]
            if d["value"] is not None:
                lines.append(f"  {key[2:]}: {d['value']:.4g} {d['unit']}")
            else:
                lines.append(f"  {key[2:]}: -- ({d['status']})")
        lines.append("")
        lines.append("*Pressures:*")
        for key in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            d = snapshot[key]
            if d["value"] is not None:
                lines.append(f"  {key}: {d['value']:.4g} {d['unit']}")
            else:
                lines.append(f"  {key}: -- ({d['status']})")
        lines.append("")
        d = snapshot["Flow"]
        lines.append(f"  Flow: {d['value']:.4g} {d['unit']}"
                     if d["value"] else "  Flow: --")
        d = snapshot["Compressor"]
        lines.append(f"  Compressor: {'Running' if d['value'] else 'OFF'}")
        d = snapshot["T_CompressorWaterOut"]
        if d["value"] is not None:
            lines.append(f"  Compressor water out: {d['value']:.4g} {d['unit']}")
        else:
            lines.append(f"  Compressor water out: -- ({d['status']})")
        d = snapshot["PulseTube"]
        lines.append(f"  Pulse Tube: {'Running' if d['value'] else 'OFF'}")
        for idx in (1, 2):
            t = snapshot.get(f"Turbo{idx}")
            ot = snapshot.get(f"Turbo{idx}_ovtemp")
            if t is None:
                continue
            state = ("Running" if t["value"] is True else
                     "OFF" if t["value"] is False else
                     f"-- ({t.get('status', '?')})")
            if ot and ot["value"] is True:
                state += "  [OVERTEMP!]"
            lines.append(f"  Turbo {idx}: {state}")
        bot.slack.send_message(target_channel, "\n".join(lines))

    # --- @mention handler for channel messages ---
    @app.event("app_mention")
    def handle_mention(event, say):
        """Respond to @BF4-bot commands in channels. Replies in-thread if mentioned in a thread."""
        raw_msg = event.get("text", "")
        import re
        # Strip the leading "<@U...>" mention tag.  Preserve original case
        # for arguments that name things (alarms) -- only the command word
        # itself needs to be case-insensitive.
        stripped = re.sub(r"<@[A-Z0-9]+>", "", raw_msg).strip()

        channel = event.get("channel")
        # If the message is in a thread, reply in that thread
        thread_ts = event.get("thread_ts") or event.get("ts")

        def reply(msg):
            say(text=msg, thread_ts=thread_ts)

        if not stripped:
            reply(HELP_TEXT)
            return

        raw_parts = stripped.split()
        cmd = raw_parts[0].lower()
        parts = [p.lower() for p in raw_parts]

        print(f"[{datetime.now()}] @mention in {channel}: '{stripped}'")

        # Whole-handler try/except so a single broken command does not
        # leave the @mention hanging with no reply.  All the per-command
        # work happens inside _dispatch_mention; errors here just get
        # logged and a single :warning: reply goes back to the channel.
        try:
            _dispatch_mention(cmd, parts, stripped, channel, thread_ts, reply)
        except Exception as e:
            print(f"[{datetime.now()}] @mention command '{cmd}' raised "
                  f"{type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                reply(f":warning: Command `{cmd}` failed: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _dispatch_mention(cmd, parts, stripped, channel, thread_ts, reply):
        """Run a single @mention command.  Any exception propagates to the
        caller's try/except (handle_mention)."""
        # Case-preserving argument tokens.  handle_mention has its own
        # raw_parts local, but a sibling function's locals are NOT in scope
        # here -- referencing it made suppress/unsuppress raise NameError in
        # channels (worked in DMs only).  Derive from `stripped` (a param);
        # keep original case so alarm names echo back as the user typed them.
        raw_parts = stripped.split()
        if cmd == "status":
            snapshot = bot.reader.get_all_live()
            lines = ["*BF4 Live Status:*", ""]
            lines.append("*Temperatures:*")
            for key in ["T_MXC", "T_Still", "T_4K", "T_50K"]:
                d = snapshot[key]
                if d["value"] is not None:
                    lines.append(f"  {key[2:]}: {d['value']:.4g} {d['unit']}")
                else:
                    lines.append(f"  {key[2:]}: -- ({d['status']})")
            lines.append("")
            lines.append("*Pressures:*")
            for key in ["P1", "P2", "P3", "P4", "P5", "P6"]:
                d = snapshot[key]
                if d["value"] is not None:
                    lines.append(f"  {key}: {d['value']:.4g} {d['unit']}")
                else:
                    lines.append(f"  {key}: -- ({d['status']})")
            lines.append("")
            d = snapshot["Flow"]
            lines.append(f"  Flow: {d['value']:.4g} {d['unit']}" if d["value"] else "  Flow: --")
            d = snapshot["Compressor"]
            lines.append(f"  Compressor: {'Running' if d['value'] else 'OFF'}")
            d = snapshot["T_CompressorWaterOut"]
            if d["value"] is not None:
                lines.append(f"  Compressor water out: {d['value']:.4g} {d['unit']}")
            else:
                lines.append(f"  Compressor water out: -- ({d['status']})")
            d = snapshot["PulseTube"]
            lines.append(f"  Pulse Tube: {'Running' if d['value'] else 'OFF'}")
            for idx in (1, 2):
                t = snapshot.get(f"Turbo{idx}")
                ot = snapshot.get(f"Turbo{idx}_ovtemp")
                if t is None:
                    continue
                state = ("Running" if t["value"] is True else
                         "OFF" if t["value"] is False else
                         f"-- ({t.get('status', '?')})")
                if ot and ot["value"] is True:
                    state += "  [OVERTEMP!]"
                lines.append(f"  Turbo {idx}: {state}")
            reply("\n".join(lines))
        elif cmd in ("report", "compressor", "turbo"):
            # Route through the SAME shared path as DMs (was inlined here,
            # which is exactly how the compressor @mention diverged in #24).
            _run_report_command(cmd, parts, channel, thread_ts, reply)
        elif cmd in ("list-alarms", "alarms"):
            reply(bot.alarm_mgr.list_alarms())
        elif cmd in ("list-reports", "reports", "report-list"):
            reply(_list_reports())
        elif cmd == "suppress":
            name = raw_parts[1] if len(raw_parts) > 1 else None
            reply(bot.suppress_alarm(name, channel))
        elif cmd == "unsuppress":
            name = raw_parts[1] if len(raw_parts) > 1 else None
            reply(bot.unsuppress_alarm(name, channel))
        elif cmd == "help":
            reply(HELP_TEXT)
        else:
            reply(f"Unknown command: `{stripped}`\nType `help` for available commands.")

    # --- Background thread for alarm polling + scheduled reports ---
    # A "pause" file at the project root disables the autonomous duties
    # (alarm polling + scheduled reports).  DM commands keep working.
    # Heartbeat still gets written so the watchdog knows the bot is alive.
    PAUSE_FLAG = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "bot_paused.flag")
    _last_pause_state = [False]  # list so nested func can mutate it

    def alarm_loop():
        print(f"[{datetime.now()}] Alarm polling started (every {bot.poll_interval}s)")
        while True:
            # Everything lives inside try/except/finally so NO exception
            # -- even one raised by os.path.exists on a bad filesystem
            # state -- can kill this thread silently.  And the sleep in
            # the finally block guarantees a delay between iterations
            # even if the except branch itself raises, avoiding a
            # tight-spin that would burn CPU and spam the log (issue #1).
            try:
                # Check shutdown flag every tick.  If the widget wrote
                # it while we were already running, exit cleanly (code
                # 0) so the scheduler does NOT restart us.
                if os.path.exists(_SHUTDOWN_FLAG):
                    print(f"[{datetime.now()}] Shutdown flag detected mid-run. "
                          f"Exiting cleanly.")
                    os._exit(0)  # os._exit kills all threads immediately
                bot.write_heartbeat()
                paused = os.path.exists(PAUSE_FLAG)
                if paused != _last_pause_state[0]:
                    print(f"[{datetime.now()}] Bot {'PAUSED' if paused else 'RESUMED'} "
                          f"via {PAUSE_FLAG}")
                    _last_pause_state[0] = paused
                if not paused:
                    bot.check_alarms()
                    bot.check_scheduled_reports()
            except Exception as e:
                print(f"[{datetime.now()}] Alarm loop error: {e}")
                traceback.print_exc()
            finally:
                time.sleep(bot.poll_interval)

    alarm_thread = threading.Thread(target=alarm_loop, daemon=True)
    alarm_thread.start()

    # --- Start Socket Mode (blocks main thread) ---
    print(f"[{datetime.now()}] BF4-bot starting...")
    print(f"  DM channel: {dm_channel}")
    print(f"  Alarm channel: {bot.alarm_channel}")
    print(f"  Poll interval: {bot.poll_interval}s")
    print(f"  DM commands: instant (Socket Mode)")
    print(f"  Scheduled reports:")
    for r in bot.report_mgr.reports:
        print(f"    - {r}")
    print(f"  Alarms: {len(bot.alarm_mgr.alarms)} configured")
    print()

    bot.slack.send_message(
        dm_channel,
        f"BF4-bot is online. {len(bot.alarm_mgr.alarms)} alarm(s) active.\n"
        f"Type `help` for commands."
    )

    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BF4 Fridge Monitoring Bot")
    parser.add_argument("--report", action="store_true", help="Send one fridge report and exit")
    parser.add_argument("--compressor", action="store_true", help="Send one compressor report and exit")
    parser.add_argument("--turbo", action="store_true", help="Send one turbo pump report and exit")
    parser.add_argument("--status", action="store_true", help="Print live status and exit")
    parser.add_argument("--hours", type=int, default=24, help="Hours of data for reports (default: 24)")
    parser.add_argument("--poll", type=int, default=30, help="Alarm poll interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.report:
        channel = get_dm_channel()
        bot = BF4Bot(channel_id=channel)
        bot.send_fridge_report(hours=args.hours)
    elif args.compressor:
        channel = get_dm_channel()
        bot = BF4Bot(channel_id=channel)
        bot.send_compressor_report(hours=args.hours)
    elif args.turbo:
        channel = get_dm_channel()
        bot = BF4Bot(channel_id=channel)
        bot.send_turbo_report(hours=args.hours)
    else:
        run_daemon()
