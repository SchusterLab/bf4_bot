# BF4 Bot — Session 2 Summary (2026-04-15)

Big structural session.  Key additions (see git log for full detail):

## New features

* **Windows Task Scheduler auto-start.** Two tasks (`BF4Bot-Main`,
  `BF4Bot-Watchdog`) fire at user logon.  XMLs in `scheduler/`.  Run
  `taskkill //F //IM pythonw.exe; schtasks //Run //TN ...` to manually
  restart without logging out.

* **Watchdog status widget on desktop** -- always-on-top Tk window
  (`watchdog.py`).  Shows heartbeat age, alarm count, and current bot
  state (RUNNING / STALE / OFFLINE / PAUSED).  Buttons for `logs`,
  `alarms`, `reports`, `X`, plus a big full-width `PAUSE`/`RESUME`.

* **`pause` flag file** (`bot_paused.flag`).  Widget pause button
  creates/deletes it.  Bot's poll loop skips alarm checks + scheduled
  reports when the flag is present; DM commands keep working.

* **Alarm editor GUI** in watchdog (`bf4bot/alarm_editor.py`).  Tk table
  of alarms + per-row edit/delete dialogs.  Variables dropdown,
  boolean "Alert when OFF/ON" UX for Compressor/PulseTube/Turbos,
  unit labels next to threshold, multi-select destinations listbox.

* **Report editor GUI** (`bf4bot/report_editor.py`).  Same structure as
  alarm editor -- schedule name/hour/days/duration/type + multi-select
  destinations.

* **Live reload** for `config/alarms.yaml` and `config/reports.yaml`.
  Bot picks up GUI saves within ~30s without restart.  On YAML parse
  failure the bot keeps the previous in-memory state.

* **Multi-destination routing.**  Each alarm and each report has a LIST
  of destinations now.  Resolution handles:
  - `operator_dm` -> two-level lookup (`channels.yaml.operator` ->
    `channels.yaml.dm_users[nick]` -> U-id)
  - `dm_<nick>` -> DM any user listed in `dm_users`
  - `<key_name>` -> channel ID from `channels.yaml`

* **Turbo pumps alarmable.**  Bot's `get_all_live()` now exposes
  `Turbo1`, `Turbo2`, `Turbo1_ovtemp`, `Turbo2_ovtemp` (booleans),
  plumbed from Bluefors REST API at `bflegacy/double/tc400*` -- paths
  discovered empirically via `scripts/probe_turbo_api.py`.

* **Any user can DM the bot** for `status` / `report` / `compressor` /
  `turbo` / `alarms` / `help`.  Reply goes back to that user's DM, not
  the operator's.  Operator-only events (watchdog offline/online alerts)
  still go only to the operator.

* **Slack API retry layer** in `bf4bot/slack_client.py` wraps
  `send_message` and `upload_image`.  Exponential backoff on 429/5xx,
  respects `retry-after` headers.

* **Plot subfolders.**  `plots/daily_report/`, `plots/on_demand/`,
  `plots/alarms/`.  Filenames encode the data window (`fridge_<start>_to_<end>.png`)
  or the trigger time (`<alarm_name>_<datetime>.png`).

* **Watchdog DMs tagged `[Watchdog]`** so they're visually distinct
  from the bot's own startup log lines.

## Config file layout (after refactor)

```
BF4_BOT/config/
├── alarm_config.yaml    # poll_interval, alarm_channel, alarm_destinations
├── report_config.yaml   # report_destinations
├── alarms.yaml          # alarm definitions (GUI-managed)
├── reports.yaml         # scheduled reports (GUI-managed)
├── channels.yaml        # Slack IDs + operator nickname + dm_users
├── keys.yaml            # API key + Slack tokens (sensitive)
└── hardware.yaml        # fridge API host/port, log_prefix
```

## Late additions (after initial summary was written)

* **Per-channel alarm suppress/unsuppress via Slack.**  Any user can
  DM or @mention `suppress` / `suppress <name>` to mute a repeating
  alarm in that specific channel.  Auto-lifts when alarm condition
  clears.  Thread-safe (lock), case-insensitive, validates destination.

* **Cooperative shutdown flag** (`bot_shutdown.flag`).  Widget's STOP
  BOT writes the flag; bot checks it at startup (before imports) AND
  every poll tick, exits with code 0.  Clean exit = scheduler does NOT
  restart.  START BOT deletes the flag + re-enables scheduler.

* **Any user can DM the bot** for status/report/compressor/turbo/alarms.
  Reply goes to the asking user's own DM.  Operator-only: scheduled
  reports, alarms, watchdog offline/online DMs.

* **`reports` command** lists scheduled reports and their destinations.

* **STOP/START bot from widget.**  Two buttons: PAUSE (skips autonomous
  work, DMs still work) and STOP BOT / START BOT (kills/launches the
  process + controls Task Scheduler enable/disable).

* **Instruction footers on alarm + report messages.**  Alarms include
  "Reply suppress <name> to mute" + "edit on BF4 PC".  Scheduled
  reports include "edit on BF4 PC".

* **Full codebase review**: 18 issues found, 4 critical/medium fixed
  immediately, 14 logged in KNOWN_ISSUES.md for future sessions.

* **Pre-commit hook** in `.git/hooks/pre-commit` blocks commits that
  contain real Slack IDs (U0.../C0.../D0...) or tokens (xoxb-/xapp-).

* **Widget bounds-check**: saved position validated against screen size
  on startup; resets to bottom-right if off-screen.

## Pending verifications (TODO next session)

See [memory/pending_verifications.md].

**Logon-trigger auto-start test.**  Tasks work via manual `schtasks /Run`;
never observed them actually firing at a real Windows login.  Next time
the user logs out and back in organically, run:
  * `tasklist | grep -i pythonw` (expect 4 processes)
  * `tail -n 30 bf4bot.log` (expect fresh startup banner)
  * Check widget is on desktop
  * If missing, `schtasks //Query //TN BF4Bot-Main //V //FO LIST` to
    debug the trigger.

## Pending operational TODOs

* **Known issues backlog** -- see [KNOWN_ISSUES.md](KNOWN_ISSUES.md)
  for 14 items ranked by severity (cooldown preservation, log parse
  hardening, duplicate alarm names, log rotation, dead code cleanup).
* **9am daily report** -- verify it still fires after today's changes.
  Now uses >= hour check so missed reports catch up after downtime.
* **Root-level YAML cleanup** -- old `keys.yaml`, `config.yaml`,
  `alarms.yaml` at project root were deleted during repo cleanup.
  All config now lives in `config/`.

---

# BF4 Bot — Session 1 Summary (2026-04-14)

## What this project is

A READ-ONLY Slack bot that monitors the BF4 Bluefors dilution refrigerator. Sends scheduled status reports with plots, responds to interactive commands via Slack, and fires configurable alarms when sensors cross thresholds.

**CRITICAL SAFETY CONSTRAINT:** The bot MUST NEVER write to the fridge API, log files, or control software. Only HTTP GET requests and `open(file, "r")` are used. The Bluefors API key is read-only (verified in the Bluefors Control Software UI).

---

## Project structure

```
C:/Users/slab/Code/BF4_BOT/
├── bf4bot/                          # Main package
│   ├── __init__.py
│   ├── reader.py                    # READ-ONLY fridge data access (API + logs)
│   ├── plotting.py                  # matplotlib plot generation
│   ├── slack_client.py              # Slack API wrapper
│   ├── alarms.py                    # Alarm definitions & cooldown tracking
│   ├── bot.py                       # BF4Bot orchestrator class
│   └── config.py                    # YAML config loader
├── run_bot.py                       # Entry point (CLI + Socket Mode daemon)
├── watchdog.py                      # Standalone offline-detection script
├── config.yaml                      # User-editable settings (committed)
├── alarms.yaml                      # Alarm thresholds (committed)
├── keys.yaml                        # Tokens, API keys, IDs (GITIGNORED)
├── docs/
│   ├── Bluefors Temperature Controller API.pdf
│   └── Bluefors_Control_Software_User_Manual.pdf
├── README.md                        # API permission reference
├── log_data_structure.md            # BF4 log file format reference
├── study_notes.md                   # HTTP, SSL, git, venv primer
├── study_notes_slack.md             # Slack communication methods primer
├── bash_learning.md                 # Bash command reference with real examples
├── .venv/                           # Python venv (gitignored)
├── .gitignore
├── test_step1_api_read.py           # Test: single API read
├── test_step1b_more_reads.py        # Test: flow, P3, P1 (sensor off handling)
├── test_step2_log_read.py           # Test: log file parsing
├── test_step2b_plot.py              # Test: 24h fridge plot
├── test_step2c_compressor_plot.py   # Test: compressor plot
├── test_step4_slack_dm.py           # Test: Slack DM
├── test_step5_slack_image.py        # Test: Slack image upload
├── test_step8_alarm.py              # Test: alarm trigger + notification
├── test_step10_full_bot.py          # Test: full bot (polling version)
├── test_socket_mode.py              # Test: Socket Mode connection
└── BF Log Plot.py, Slackbot/        # Original reference code (inherited)
```

---

## How to run

### CLI commands
```bash
source .venv/Scripts/activate
python run_bot.py --status           # Print live fridge values
python run_bot.py --report           # Send 24h fridge plot to operator DM
python run_bot.py --report --hours 168   # 7-day plot
python run_bot.py --compressor       # Compressor plot
python run_bot.py --turbo            # Turbo pump plot
python run_bot.py                    # Start full daemon (Socket Mode + alarms)
python watchdog.py                   # Check if main bot is alive
```

### Slack DM commands (operator only)
- `status`, `report`, `report 7d`, `compressor`, `turbo`, `alarms`, `help`

### Slack @mention commands (in channels)
- `@BF4-bot status|report|compressor|turbo|alarms|help`
- Replies in thread if mentioned in a thread

---

## Configuration files

### `keys.yaml` (GITIGNORED — never commit)
All tokens, API keys, user/channel IDs. Loaded by `bf4bot/config.py::load_keys()`.

```yaml
bluefors_api_key: "..."              # Bluefors API read-only key
slack_bot_token: "xoxb-..."          # Slack bot OAuth token
slack_app_token: "xapp-..."          # Slack app-level token (Socket Mode)
bot_operator_user_id: "U..."         # Chuyao's Slack user ID
fluxonium_kids_channel: "C..."       # Target channel for reports
```

### `config.yaml` (committed, references keys by name)
User-editable settings:
- `reports:` list — each entry has `channel` (key name), `hour`, `days`, `hours`, `type`
- `poll_interval: 30` — seconds between alarm checks
- `alarm_channel: fluxonium_kids_channel`
- `api_host`, `api_port`, `log_prefix` (don't change unless hardware changes)

### `alarms.yaml` (committed)
List of alarm definitions. Each has `name`, `variable`, `condition`, `threshold`, `interval_min`, `enabled`. Variables: `T_MXC`, `T_Still`, `T_4K`, `T_50K` (temperatures), `P1`–`P6` (pressures), `Flow`.

---

## Architecture notes

### Data sources (both READ-ONLY)
1. **Bluefors REST API** (`https://localhost:49098/values/mapper/...`) for real-time values — used for alarm polling and `status` command
2. **Log files** (`S:/_BlueFors4/Logs/YY-MM-DD/`) for historical data — used for plots. CSV files with 20s intervals

### Log file channels (BF4 specific)
| File | Fridge stage |
|------|-------------|
| CH1 | 50K flange |
| CH2 | 4K flange |
| CH5 | Still |
| CH6 | MXC (mixing chamber) |
| maxigauge | All pressures P1-P6 |
| Flowmeter | He-3 flow |
| Status_ | Compressor, turbo pumps, water temps |
| Channels | Valve/pump on-off states |

### Slack integration
- **Socket Mode** (`slack_bolt` + `xapp-` token) for instant DM/mention responses
- Background thread polls Bluefors API every 30s for alarms
- Reports sent to channels via `files_upload_v2`
- **Operator-only DMs**: only the user in `bot_operator_user_id` gets responses
- **@mentions**: work in any channel the bot is invited to; replies in-thread if mentioned in a thread

### Offline detection (heartbeat + watchdog)
- Main bot writes `bot_heartbeat.txt` timestamp every 30s
- `watchdog.py` runs periodically (via Windows Task Scheduler, every 5 min)
- If heartbeat is > 5 min stale → sends "BF4-bot OFFLINE" DM (once)
- When heartbeat returns → sends "BF4-bot back online" DM
- Uses `bot_outage_notified.flag` to avoid spam

---

## Git repository state

- **Initialized** in this directory, 1 branch (master)
- **8 commits** — all cleaned of hardcoded user/channel IDs and tokens
- **History rewritten once** with `git filter-branch` to remove IDs that were accidentally committed early on
- `keys.yaml`, `*.txt` tokens, `.venv/`, `*.png`, heartbeat/flag files all gitignored
- User config: `chuyaotong <cytong@stanford.edu>` (repo-local only, not global)

---

## What's working

- Real-time API reads (temperatures, pressures, flow, compressor, pulse tube)
- Historical log file parsing (multi-day stitching, all file types)
- 8-panel fridge status plot (Bluefors native colors)
- Compressor plot (water/oil/helium temps, pressures, current)
- Turbo pump plot (1 row × 3 columns, On/Off labels)
- Alarm plot (single-panel with threshold line)
- Slack DMs with instant Socket Mode responses (operator-only)
- @mention handler for channels (replies in-thread if in thread)
- Alarm system with per-alarm cooldown timers
- Config-driven scheduled reports (daily/weekly/weekdays)
- Heartbeat + watchdog for offline detection
- Full bot daemon stays running, survives crashes in alarm thread

---

## What's not done yet / open items

1. **Windows Task Scheduler setup** for:
   - Auto-start main bot on PC reboot/login
   - Run `watchdog.py` every 5 minutes
2. **Tune alarm thresholds** — `alarms.yaml` has default values that may need adjustment
3. **Test alarm channel vs DM channel** separation — currently both go to fluxonium-kids, may want alarms to DM operator only
4. **Daily 9am report** — configured in `config.yaml` but hasn't triggered yet (will fire at 9am tomorrow)
5. **Longer-term stability test** — bot has been tested for minutes, not days
6. **Error handling** — API timeouts, log file read failures could be more graceful
7. **README overhaul** — current README is API reference; a proper "how to run / how to install" section would help new contributors

---

## Key files to read first next session

1. `SESSION_SUMMARY.md` (this file)
2. `config.yaml` — current settings
3. `bf4bot/bot.py` — main orchestrator
4. `run_bot.py` — entry point with Socket Mode and @mention handlers
5. `README.md` — API permission reference (still useful)

---

## Commands to resume quickly

```bash
cd C:/Users/slab/Code/BF4_BOT
source .venv/Scripts/activate
python run_bot.py --status           # Verify bot can read fridge
python run_bot.py                    # Start the bot
```

---

## Lessons learned this session

1. **keys.yaml from the start** — should have set up gitignored keys file BEFORE first commit. Hardcoded IDs led to git history rewrite later. Saved to memory as `feedback_secrets_from_start.md`.
2. **Unicode chars crash on Windows** — never use `→` or `—` in `print()` on Windows cp1252 terminal. Use `->` and `--`. Saved as `feedback_unicode.md`.
3. **Python env isolation** — always verify we're in venv before pip install. Saved as `feedback_pip_safety.md`.
4. **Write test scripts not inline** — `.py` files over Bash inline Python. Saved as `feedback_test_scripts.md`.
5. **Slack DM blocking** — requires Event Subscriptions + `message.im` subscribed AND Socket Mode (or public URL) before DM input unblocks. Documented in `study_notes_slack.md`.

---

# BF4 Bot -- Session 3 Summary (2026-04-16)

Polish + stability session.  Eight commits, seven issues closed.

## Bugs fixed

* **Heartbeat mid-write race** (commit `ad7d210`).  `Bot.write_heartbeat`
  used `open(path, "w")` which truncated the file to 0 bytes before
  writing, causing the watchdog to occasionally read an empty file and
  falsely DM "OFFLINE".  Fix: write to a .tmp file and `os.replace()`
  onto the real name (atomic on Windows).  Smoke test verified 0 empty
  reads in 35k reads across 10s of contention.

* **`#17` Ambiguous DM commands** (commit `79b38bb`).  Renamed `alarms`
  -> `list-alarms` and `reports` -> `list-reports` because the short
  names collided with `report` (generate a fresh report) and `alarm`
  concepts generally.  Old names kept as silent aliases for habit.

* **`#18` STOP/START widget UX cluster** (commit `a20c323`).
  - STOP used to flip the button to green "START BOT" immediately
    while the bot was still cooperatively winding down (~30s), so a
    fast re-click would delete the shutdown flag before the bot noticed
    -> old bot stayed alive.  Fix: new STOPPING state with gray "please
    wait" disabled button until heartbeat confirms bot has exited
    (max 45s, 90s timeout for stuck bots).
  - START had a symmetric 5-second gap where the button said "START
    BOT" green before the new bot wrote its first heartbeat.  Fix:
    symmetric STARTING state.
  - Status label was reverting to RUNNING during STOPPING because
    `_tick` ran normal state rendering over the one-shot label I had
    set in the helper.  Fix: render pending states in `_tick` itself
    using COLOR_WARN amber for both dot and "STOPPING..."/"STARTING..."
    label.
  - Widget restart after a STOP was misreading the stale-but-recent
    heartbeat as "bot alive" and showing RUNNING.  Fix: `shutdown.flag`
    is now the authoritative source of truth, checked unconditionally
    every tick (not gated on `_bot_stopped_by_user`).
  - Button text `"STOPPING...\nplease wait"` didn't fit the button
    width.  Simplified to `"please wait"` (one line, smaller non-bold
    font).

* **`#2` + `#19` Duplicate scheduled fires** (commits `fa0f0c1` and
  `e095569`).  Two code paths were losing the "already fired" state.
  - Reload path: editing `reports.yaml` / `alarms.yaml` reconstructed
    objects with fresh state -> any past-threshold report or active
    alarm would re-fire immediately.  Fix: in `reload_if_changed`,
    transfer `_last_sent_date` / `last_triggered` + `is_active` from
    old objects to new by matching on name.
  - Restart path: the previous process died, taking in-memory state
    with it.  Fix: persist to disk in two new JSON files at project
    root, `report_state.json` and `alarm_state.json`, atomic-write
    pattern.  Managers read them in `__init__` and write them on
    every state mutation.
  Verified live against an intentionally-tripped `T_MXC < 100mK`
  alarm.

* **`#15` / `#22` Alarm + Report editor polish** (commit `c5d6040`).
  - Dialog text simplified from an 18-line wall to bare essentials;
    the "Tip: close + reopen" line was wrong (had to close the whole
    editor window, not just the dialog) -- fixed in body as a proper
    instruction.  Single function serves both alarm and report
    editors.
  - Alarm rows with multiple destinations were pushing the per-row
    edit/delete buttons off-screen; now wrap to multi-line.

* **`#20` Alarm message interval** (commit `c5d6040`).  Alarms now
  announce their re-alert interval so the user knows how often the
  bot will keep pinging while the condition persists.

## Infrastructure / cleanup

* **Migrated `KNOWN_ISSUES.md` to GitHub Issues** (commit `557eef0`)
  -- 14 tickets on the repo, `KNOWN_ISSUES.md` now points to them.
  Installed `gh` (GitHub CLI) via `winget` for scripted issue
  management.

* **Stopped tracking `study_notes/`** in git (commit `557eef0`) --
  personal learning notes kept locally only.  `git rm --cached -r`
  preserved files on disk.

* **Added weekly fridge report** (Friday -> Monday per user edit
  later) and `bf4_channel` to alarm + report destination whitelists
  (commits `557eef0`, `20a2492`).

* **MXC_warm interval** relaxed from 1m -> 10m; **Compressor_is_off**
  tightened from 5m -> 1m and added `bf4_channel` destination (commit
  `20a2492`).

## New memory files (project)

* `project_cooperative_shutdown.md` -- never `taskkill` the bot;
  `shutdown.flag` + exit-code-0 is the only correct STOP mechanism;
  the flag file is the authoritative source of truth for widget
  rendering.
* `project_no_wmic_tasklist_polling.md` -- wmic / tasklist flash a
  CMD window every call even with CREATE_NO_WINDOW; never poll them
  from a Tk widget.
* `project_windows_venv_double_pythonw.md` -- two pythonw.exe per
  venv-launched script is a Python-on-Windows implementation detail,
  not a bug; 4 total for BF4_BOT is healthy.

## New global Claude rules (CLAUDE.md)

Baked during this session, roughly in order of "times I got corrected":
1. Verify a tool exists (`which`/`where`/`ls`) before claiming I'll
   use it.
2. Gather project memory (grep) before touching a feature.
3. Debugged knowledge goes into project memory, not only code
   comments.
4. Plan before edit, self-review before end-to-end testing.
5. Issues describe user symptoms, not code paths -- one user bug =
   one issue even if the fix touches multiple paths.
6. Be concise in memory and notes.

## Still open after this session (11 issues)

- Original backlog minus the 4 closed: `#1`, `#3`-`#14`.
- `#16` Widget invisible on Windows taskbar (Tk `overrideredirect`
  removes the taskbar entry; needs investigation or a system-tray
  library).

Run `gh issue list --state open` for the live list.

## Commands to resume quickly (unchanged since Session 2)

```bash
cd C:/Users/slab/Code/BF4_BOT
source .venv/Scripts/activate
python run_bot.py --status
python run_bot.py
```

---

# BF4 Bot -- Session 4 Summary (2026-04-17)

Five GitHub issues closed, one new issue filed for follow-up work.
Focus was the "silently breaks and user wouldn't notice" bucket.

## Issues closed

| # | Title | Commit |
|---|---|---|
| #16 | Watchdog widget invisible on taskbar | `06f1dc1` |
| #12 | bf4bot.log never rotated | `652d0f7` |
| #3  | Log file parse errors crash report generation | `6a5b359` |
| #5  | Compressor/PulseTube state truthiness is fragile | `82b4b05` |
| #1  | Alarm loop tight-spins on persistent exceptions | `1f55043` |

## Issues opened

* `#23` Migrate from raw `print()` to Python `logging` module. Tracks
  Stages 2-5 of the migration started by #12. 97 `print()` call sites
  across 6 files, enumerated in the issue body.

## Key changes

* **`bf4bot/logging_setup.py`** (new, 160 lines) -- configures the
  `bf4bot` logger with a `RotatingFileHandler` (5 MB x 5 backups) and
  installs a stdout/stderr shim so every existing `print()` benefits
  from rotation without touching call sites. Stage 1 of the logger
  migration.
* **`bf4bot/reader.py`** -- every log-parse function wraps per-line
  parsing in `try/except (ValueError, IndexError)`, broadens
  file-open `FileNotFoundError` to `OSError` (catches drive
  disconnect / permission). Bonus fix: timestamps and values were
  being appended out of sync, so a bad float() left the two lists
  misaligned. Now built atomically.
* **`bf4bot/reader.py`** -- `get_compressor_state` and
  `get_pulse_tube` use explicit `value in (None, "")` instead of
  truthiness (would treat int 0 as "unknown") and honour
  `status == "INVALID"` (was silently trusting stale cached values).
* **`run_bot.py`** -- alarm loop body wrapped in
  `try/except/finally`. Shutdown-flag check moved inside `try` so a
  transient `os.path.exists` failure can't silently kill the thread.
  `time.sleep` moved to `finally` so a delay always happens, even if
  exception handling itself raises.
* **`watchdog.py`** -- native Windows title bar restored (drops
  `overrideredirect(True)`) so the widget appears in the taskbar and
  Alt-Tab. Tree icon from `shell32.dll` (icon #41) replaces the Tk
  feather. `assets/tree.ico` checked in; extracted via
  `scripts/extract_shell32_icons.ps1`.

## Tests added

First test suite in the project. 21 tests across:

* `tests/test_log_rotation.py` -- 6 tests: rotation at maxBytes,
  backup cascade, multi-line split, propagate=False, dev-terminal
  no-op, idempotent install.
* `tests/test_reader_robustness.py` -- 15 tests: corrupt lines, bad
  floats, missing files, PermissionError, truncated pressure lines,
  compressor/pulse-tube edge values, INVALID status.

Pytest installed into `.venv` (not miniconda base -- lab-safety rule).

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
```

## Memory baked this session

Global (`~/.claude/CLAUDE.md`):

* **Present options with detailed pros AND cons** -- never pre-filter
  to one recommended + others dismissed in a line.
* **Prefer native logger over print** in any language -- raw stdout
  prints are fine for one-off scripts, not for long-running processes.
* **Python installs: project venv only** (rewrite of earlier rule for
  clarity -- installing IS fine, just into `.venv`, never
  miniconda/system).
* **Study notes vs SESSION_SUMMARY are different** -- study notes =
  jargon/concepts, SESSION_SUMMARY = work accomplished.

Project:

* `project_logging_migration.md` -- notes the mid-flight state:
  Stage 1 (shim + rotation) shipped, Stages 2-5 tracked in #23.
  Future sessions seeing `print()` calls should know they flow
  through a shim and not propose yet another redirect.
* `project_reader_list_alignment.md` -- when parsing into parallel
  lists, build ALL fields first, append atomically.

## Still open after this session (9 issues)

Original backlog minus the 5 closed: `#4`, `#6`-`#11`, `#13`, `#14`,
and the new `#23`.

Remaining bugs are lower-severity: too-broad exception catches,
`suppress_alarm` holding a lock across Slack API call, no-`utf-8`
in `alarm_history.log`, etc. None are "silently breaks fridge
monitoring" grade.

Run `gh issue list --state open` for the live list.

## Pending verification still open

Task Scheduler logon-trigger auto-start (from Session 3) is still
unverified -- confirmed at start of this session that no logout/login
cycle has happened since last session.

# BF4 Bot -- Session 5 Summary (2026-04-17)

Short follow-up session: one UI bug fix in the Report editor, commit
pushed to main.

## What landed

* `47d26da` -- Report editor: wrap multi-destinations and keep
  edit/del buttons visible. Same pattern as alarm_editor fix (#22):
  split destinations onto their own lines, pack buttons BEFORE the
  expanding label so they reserve right-edge width on narrow windows.
  File: `bf4bot/report_editor.py`.

## Symptom the user saw

Weekly fridge report row (destinations = `bf4_channel, operator_dm`)
rendered as a single CSV line that pushed the `edit`/`del` buttons
off the right edge of the 760px Toplevel. Daily report with two
destinations hid its buttons entirely. The alarm editor already had
the fix; only the report editor was unpatched.

## Notable things for future work

* Tk pack order matters when mixing `expand=True` with `side="right"`
  siblings. If the expanding label is packed first, right-side buttons
  can still get clipped on narrow windows. Pack the fixed-width
  right-side widgets FIRST, then the expanding left-side label.
* The wrap-dest pattern is duplicated between alarm_editor.py:381
  and report_editor.py now. If a third editor shows up, pull this
  into a shared helper rather than copy-paste a third time.

## Second fix this session -- editor window icons

User noticed the alarm + report editors (and their sub-dialogs) still
showed Tk's default feather icon instead of the tree icon the
watchdog widget uses. Root cause: `iconbitmap` on the root `Tk()`
does NOT propagate to child `Toplevel` windows -- each Toplevel needs
its own call.

Fix: added `_ICON_PATH` and `_apply_icon(top)` helper in
`bf4bot/alarm_editor.py`, called on all 5 Toplevels across the two
editors (alarm list, alarm add/edit, destinations help dialog, report
list, report add/edit). Helper wraps `iconbitmap` in try/except so a
missing .ico falls back to the feather instead of crashing.

`_apply_icon` is imported from `alarm_editor` into `report_editor`
rather than duplicated. Underscore-prefixed export is intentional --
internal to the editor pair, not a public API.

## Not touched

`config/alarms.yaml` had pre-existing uncommitted edits from before
this session -- intentionally NOT staged or committed.

## Still open after this session

Same 9 issues as end of Session 4. No new issues filed.

## Pending verification still open

Task Scheduler logon-trigger auto-start (from Session 3) still
unverified -- no logout/login cycle yet.


# BF4 Bot -- Session 6 Summary (2026-07-14)

Investigated and fixed issue #24 (compressor report blank panels), plus two
latent plotting bugs surfaced during the investigation. Committed on branch
`fix/issue-24-compressor-mention-panels`.

## What landed

* `c842631` -- Fix #24: compressor pressure/current panels blank in @mention
  reports. Root cause: the @mention "compressor" handler
  (`run_bot._dispatch_mention`) calls `read_history()` + `plot_compressor()`
  directly, but the pressure/current series (`cpalp`/`cpahp`/`cpacurrent`)
  were added ONLY by a separate `extra_keys` loop inside
  `bot.send_compressor_report` (the DM/scheduled path). So compressor reports
  requested via @mention in a channel always rendered "Compressor Pressures"
  and "Motor Current" blank. Fix: added those keys to `reader.read_history`
  `comp_keys` (single source of compressor data) and deleted the redundant
  loop. Files: `bf4bot/reader.py`, `bf4bot/bot.py`, `bf4bot/plotting.py`.

## Symptom the user saw

A 2-day compressor report sent to a channel had blank bottom-row panels
(pressures + motor current); the same report to a DM was fine. The issue's
own guesses ("data consumed/cached on first render", "destination change")
were disproven. The real trigger was the code path: DM command vs @mention.

## How it was found

* Fanned out a multi-agent investigation; empirically proved the data layer
  and render layer were both correct/deterministic (two data builds
  identical; 144 concurrent renders, 0 blanks) -- which correctly said the
  bug was NOT where the issue guessed.
* Decisive evidence: the user's screenshot (bottom panels showed
  matplotlib's empty-axis default 00:00->00:00) + the live `bf4bot.log`,
  which showed the blank render came from an @mention (not a DM) and logged
  `plotting.py:233: No artists with labels` -- proof `cpalp`/`cpahp` were
  empty on that path.

## Latent bugs also fixed (not the #24 cause)

* Thread-safety: all `plot_*` fns now use the OO `Figure`/`FigureCanvasAgg`
  API under a module `_RENDER_LOCK` instead of pyplot global state (bare
  `plt.close()`/`plt.tight_layout()`), which is not thread-safe and runs
  from two threads (alarm thread + Slack command thread).
* Filename collision: `_window_filename` now appends `HHMMSS_microseconds`
  so two same-window reports don't overwrite the same PNG mid-upload.

## Notable things for future work

* DRY: report/turbo/fridge plots are still built in TWO places
  (`bot.send_*_report` and `run_bot._dispatch_mention`). They diverged once
  (this bug). If a fourth caller appears, extract a shared builder.
* Filenames are now unique -> `plots/` will accumulate PNGs; consider a
  retention/cleanup step later.
* The running bot must be restarted (normal STOP/START) to pick up this fix
  -- it was NOT restarted this session.

## Tests

21/21 passing (pytest).

## Not touched

`config/alarms.yaml` and `config/reports.yaml` had pre-existing uncommitted
edits from before this session -- intentionally NOT staged or committed.

## Still open after this session

Issue #24 fix committed on a branch (not merged to main yet). Issue #23
(print -> logging migration) still open.

## Pending verification still open

Task Scheduler logon-trigger auto-start (from Session 3) still unverified --
user confirmed no logout/login cycle has happened yet.
