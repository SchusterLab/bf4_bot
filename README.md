# BF4 Bot

A READ-ONLY Slack bot that monitors the BF4 Bluefors dilution refrigerator
and its support hardware (compressor, pulse tube, turbo pumps).  Sends
scheduled reports with plots, answers interactive Slack queries, and fires
alarms when thresholds are crossed.

> **SAFETY: This bot is strictly READ-ONLY. It NEVER writes to the fridge
> API, log files, or control software.  All file access is `open(file, "r")`.
> All API calls are HTTP GET.**

This README describes the codebase for developers / lab members who want
to understand how the bot works.  For a setup guide on a different PC,
fridge, or Slack workspace, see [WALKTHROUGH.md](WALKTHROUGH.md).  For a
short "what this bot does" description to paste into Slack, see
[BOT_DESCRIPTION.md](BOT_DESCRIPTION.md).

---

## 1. Architecture at a glance

Two long-running Python processes, coordinated through the filesystem:

```
       +----------------+           +-------------------+
       |   run_bot.py   |           |    watchdog.py    |
       |  (headless)    |           |  (Tk widget)      |
       |                |           |                   |
       | Slack Bolt     |           | Dark-theme card   |
       | Socket Mode    |           | on your desktop   |
       | Alarm poller   |           |                   |
       | Report sched.  |           | Reads heartbeat   |
       |                |           | + alarms.yaml     |
       +--------+-------+           +---------+---------+
                |                             |
          heartbeat every 30s --->            |
          alarm history writes                |
                                  pause flag watched
                                               |
             config/*.yaml  <-- edited by <----+
                                 both + GUI
```

Each process is launched by Windows Task Scheduler at user logon.  Neither
process directly talks to the other: they communicate through files in
the project root (`bot_heartbeat.txt`, `bot_paused.flag`,
`alarm_history.log`) and shared YAML configs under `config/`.

**Why two processes, not one?** The main bot runs alarm polling and Slack
Socket Mode; the watchdog renders a Tk status widget.  If the bot
crashes, the watchdog is still alive to notice, flip red, and DM the
operator.  If they were one process, a bot crash would also kill the
"bot is down" messenger.

---

## 2. Directory layout

```
BF4_BOT/
|-- run_bot.py                  Entry point for the main bot daemon.
|-- watchdog.py                 Entry point for the Tk status widget.
|-- bf4bot/                     The package -- all reusable logic.
|   |-- __init__.py             parse_duration / format_duration helpers.
|   |-- reader.py               READ-ONLY fridge data access.
|   |-- plotting.py             matplotlib plot generation.
|   |-- alarms.py               Alarm class + AlarmManager.
|   |-- alarm_editor.py         Tk GUI for editing alarms.yaml.
|   |-- report_editor.py        Tk GUI for editing reports.yaml.
|   |-- bot.py                  BF4Bot orchestrator -- alarm checks,
|   |                           scheduled reports, DM command routing.
|   |-- slack_client.py         Thin wrapper around slack_sdk with a
|   |                           retry/backoff layer for transient errors.
|   +-- config.py               YAML loaders for every config file.
|-- config/                     All user-editable settings.
|   |-- alarm_config.yaml       poll_interval, alarm_channel,
|   |                           alarm_destinations (GUI allowlist).
|   |-- report_config.yaml      report_destinations (GUI allowlist).
|   |-- alarms.yaml             Alarm definitions (GUI-managed).
|   |-- reports.yaml            Scheduled reports (GUI-managed).
|   |-- channels.yaml           Slack channel + user IDs, operator.
|   |-- keys.yaml               API keys + Slack tokens (gitignored).
|   +-- hardware.yaml           Fridge API host/port, log path.
|-- scheduler/                  Task Scheduler XML definitions.
|   |-- BF4Bot-Main.xml
|   |-- BF4Bot-Watchdog.xml
|   |-- BF4_Watchdog.bat        Desktop launcher shortcut.
|   +-- convert_xml_utf16.py    Re-encode XMLs to UTF-16 LE + BOM.
|-- scripts/                    One-shot / utility scripts.
|-- docs/                       Bluefors PDFs (API + control software).
|-- plots/                      Generated PNGs (gitignored).
|   |-- daily_report/           Scheduled reports.
|   |-- on_demand/              Ad-hoc DM/mention reports.
|   +-- alarms/                 Triggered alarm plots.
|-- study_notes/                Accumulated learning notes.
|-- reference/                  Original pre-bot scripts (read-only ref).
|-- SESSION_SUMMARY.md          Development history.
|-- TASK_SCHEDULER_SETUP.md     How to install auto-start.
|-- WALKTHROUGH.md              Setup on a new PC.
+-- BOT_DESCRIPTION.md          Short Slack-facing description.
```

Runtime-only files created at project root (all gitignored):

| File | Purpose |
|------|---------|
| `bf4bot.log` | bot stdout/stderr when headless |
| `watchdog.log` | widget stdout/stderr |
| `bot_heartbeat.txt` | ISO timestamp, updated every 30s |
| `bot_paused.flag` | exists = bot autonomous work paused |
| `bot_outage_notified.flag` | prevents repeat OFFLINE DMs |
| `widget_position.json` | last widget x,y position |
| `alarm_history.log` | JSONL, one line per alarm trigger |

---

## 3. Fridge data sources

Two completely separate read-paths; the bot uses BOTH, for different
purposes.

### 3.1 Bluefors REST API (real-time)

**Endpoint**: `https://<host>:<port>/values/mapper/<path>`
(typically `https://localhost:49098`, configured in `config/hardware.yaml`.)

**Auth**: API key in header.  Key lives in `config/keys.yaml` as
`bluefors_api_key`.  The key must be a READ-ONLY key generated in the
Bluefors Control Software UI.

**Purpose**: live values for the alarm poll cycle and the `status`
command.  30-second cadence.

**Paths we use** (all through `bf4bot/reader.py`):

| Variable | REST path | Unit |
|----------|-----------|------|
| T_MXC, T_Still | `bf/temperatures/t<name>` | mK (reader converts K to mK) |
| T_4K, T_50K | `bf/temperatures/t<name>` | K |
| P1..P6 | `bf/pressures/p<n>` | mbar (reader converts bar to mbar) |
| Flow | `bf/flow` | mmol/s |
| Compressor | `bflegacy/double/cparun` | bool |
| PulseTube | `bf/pulsetube` | bool |
| Turbo1 | `bflegacy/double/tc400pumpstatn` | bool |
| Turbo2 | `bflegacy/double/tc400pumpstatn_2` | bool |
| Turbo1_ovtemp | `bflegacy/double/tc400ovtemppum` OR `...ovtempelec` | bool (OR) |
| Turbo2_ovtemp | same with `_2` suffix | bool (OR) |

The turbo endpoints were discovered empirically with
`scripts/probe_turbo_api.py` -- Bluefors's official docs only cover the
Temperature Controller, not the full Control Software API.

### 3.2 Log files (historical)

**Location**: `S:/_BlueFors4/Logs/YY-MM-DD/` (a shared network drive
mapped to the fridge PC's local log folder).  Configured in
`config/hardware.yaml` as `log_prefix`.

**Purpose**: plots for report and alarm messages that show trends over
time.  We NEVER write -- just `open(path, "r")`.

**Format**: CSV, one file per measurement per day, ~20s cadence.

| Filename pattern | Contents |
|------------------|----------|
| `CH1 T <date>.log` | 50K stage temperature (K) |
| `CH2 T <date>.log` | 4K stage temperature (K) |
| `CH5 T <date>.log` | Still temperature (K) |
| `CH6 T <date>.log` | MXC temperature (K) |
| `maxigauge <date>.log` | All P1..P6 (mbar) |
| `Flowmeter <date>.log` | He-3 flow |
| `Status_<date>.log` | Compressor internals, turbo status |
| `Channels <date>.log` | Valve / pump on/off states |

Read logic is in `bf4bot/reader.py::read_history`, which stitches
multiple days together when a report window crosses midnight.

---

## 4. Slack integration

### 4.1 Socket Mode

We use **Slack Bolt with Socket Mode** (not Events API / public HTTPS)
because the fridge PC is behind the lab firewall -- no public IP to post
events to.  Socket Mode opens a persistent WebSocket from the bot out to
Slack; events travel over that long-lived connection.

Tokens required (both in `config/keys.yaml`):
* `slack_bot_token` (`xoxb-...`) -- for API calls (chat.postMessage etc.)
* `slack_app_token` (`xapp-...`) -- for the Socket Mode connection

### 4.2 Destination model

Everything that can receive a bot message is a named "destination".  Four
forms resolve at runtime:

| Name pattern | Meaning |
|--------------|---------|
| `operator_dm` | DM the current operator (two-level lookup via channels.yaml) |
| `dm_<nickname>` | DM a user listed in channels.yaml's `dm_users` map |
| `<key_name>` | Lookup a channel ID defined in channels.yaml |
| raw `C0...` / `D0...` / `G0...` | Treated as a literal Slack channel ID |

Alarms and reports each have their OWN allowlist of destination names in
`alarm_config.yaml` / `report_config.yaml`.  The GUI editors show only
the names in the respective allowlist.

### 4.3 Operator two-level lookup

```yaml
# channels.yaml
operator: chuyao
dm_users:
  chuyao: U0XXXXXXXXX
```

When a destination is `operator_dm`, the bot does:
1. `keys["operator"]` -- "chuyao"
2. `keys["dm_users"]["chuyao"]` -- "U0XXXXXXXXX"
3. `conversations.open(users=["U0XXXXXXXXX"])` -- DM channel "D0..."
4. Post to that channel.

Changing operators = edit one line (`operator: alice`).

### 4.4 Who can DM the bot?

**Any Slack workspace member** can DM the bot and get a reply.  Commands:
`status`, `report [duration]`, `compressor [duration]`, `turbo [duration]`,
`alarms`, `reports`, `suppress [name]`, `unsuppress [name]`, `help`.
Non-operator DMs reply into the asker's own DM channel.

**Operator-only events** (unprompted):
* Scheduled reports go to their configured destinations.
* Alarms go to their per-alarm destinations.
* Watchdog online/offline DMs go to the operator only.

### 4.5 Alarm suppression

Any user can tell the bot to stop repeating an alarm **in their channel**:

* `suppress` -- suppress whatever alarm last fired here.
* `suppress P4_high` -- suppress a specific alarm (case-insensitive).
* `unsuppress` / `unsuppress P4_high` -- manually lift suppression.

Suppression is **per-channel**: suppressing in your DM does not affect
other channels.  It auto-lifts when the alarm **clears** (condition
returns to normal), so the next fresh trigger fires normally.  Resets
on bot restart (intentional for safety -- a fresh start re-announces).

---

## 5. Alarm system

Defined in `config/alarms.yaml`.  Each alarm has:
* `name` -- unique label
* `variable` -- sensor name (see table in section 3.1)
* `condition` -- `>` or `<`
* `threshold` -- numeric
* `interval_min` -- cooldown minutes between re-alerts
* `enabled` -- true/false
* `destination` -- string or list (routes per section 4.2)

Boolean variables (`Compressor`, `PulseTube`, `Turbo1`, `Turbo2`,
etc.) are expressed using the same `condition` + `threshold` fields --
the GUI converts "alert when OFF" to `condition: "<"`, `threshold: 1.0`.

**Flow**: `BF4Bot.check_alarms()` runs every `poll_interval` seconds.
For each enabled alarm whose condition is met AND cooldown has elapsed:
1. Append event to `alarm_history.log` (JSONL).
2. Generate a plot via `plotting.plot_alarm_data`.
3. Resolve destinations via `_resolve_destinations`.
4. Send message + plot to each.

**Live reload**: `AlarmManager.reload_if_changed()` checks the file's
mtime each tick; re-parses if changed.  On parse error, keeps old state.

---

## 6. Reports

Defined in `config/reports.yaml`.  Each has `name`, `type` (fridge /
compressor / turbo), `hour`, `days`, `hours` (duration), `destinations`.

Checked by `BF4Bot.check_scheduled_reports()` each poll.  Scheduled
report plots land in `plots/daily_report/`; on-demand plots in
`plots/on_demand/`.  Filenames encode the data window:
`fridge_2026-04-14_0900_to_2026-04-15_0900.png`.

---

## 7. Watchdog widget

Tk window, always-on-top, draggable, position persisted.  Shows heartbeat
age, alarm count, and state (RUNNING / STALE / OFFLINE / PAUSED).

Buttons: `[logs]`, `[alarms]`, `[reports]`, `[X]`, and a full-width
`[PAUSE]` / `[RESUME]`.  Pause creates `bot_paused.flag`; the bot's
poll loop skips alarm checks + scheduled reports while the flag exists.
DM commands keep working.

Watchdog-originated Slack DMs are tagged `*[Watchdog]*` to distinguish
them from bot startup messages.

---

## 8. Task Scheduler

Two tasks: `BF4Bot-Main` + `BF4Bot-Watchdog`.
* Triggered on user logon.
* Restart on failure every 1 min, up to 99 attempts.
* Priority 9 (IDLE class) -- yields CPU to everything else.
* Run as interactive user (so widget can draw).

XMLs are UTF-16 LE + BOM (Task Scheduler requirement).

See [TASK_SCHEDULER_SETUP.md](TASK_SCHEDULER_SETUP.md) for full details.

---

## 9. Running and stopping

| Action | Method |
|--------|--------|
| Print fridge status | `python run_bot.py --status` |
| Generate one report | `python run_bot.py --report --hours 24` |
| Start bot (debug, with terminal) | `python run_bot.py` |
| Start bot (headless) | double-click `BF4_Bot.bat` on desktop, or widget START BOT |
| Start widget | double-click `BF4_Watchdog.bat` on desktop |
| Stop bot | widget STOP BOT button (writes shutdown flag + disables scheduler) |
| Start bot after stop | widget START BOT button (deletes flag + enables scheduler) |
| Pause autonomous work (alarms+reports) | widget PAUSE button (bot stays alive for DMs) |
| Stop everything | `taskkill //F //IM pythonw.exe` (Git Bash) |
| Tail logs | `tail -f bf4bot.log` |

---

## 10. Related docs

* [WALKTHROUGH.md](WALKTHROUGH.md) -- fresh-PC setup guide.
* [BOT_DESCRIPTION.md](BOT_DESCRIPTION.md) -- short Slack-facing summary.
* [TASK_SCHEDULER_SETUP.md](TASK_SCHEDULER_SETUP.md) -- auto-start setup.
* [KNOWN_ISSUES.md](KNOWN_ISSUES.md) -- backlog of non-critical bugs/improvements.
* [SESSION_SUMMARY.md](SESSION_SUMMARY.md) -- development history.
* [study_notes/](study_notes/) -- learning references (jargon, Slack,
  Windows Task Scheduler, bash commands, log file structure).
