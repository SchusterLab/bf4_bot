# Task Scheduler Setup

How to make the BF4 bot and its status widget auto-start at Windows login.

**One-time setup.** Once installed, both processes launch automatically every
time you log on to this PC. See [study_notes_windows_tasks.md](study_notes_windows_tasks.md)
for concepts (XML, SID, pythonw vs python) before following the steps below.

---

## Before you start

Confirm:

* You are logged in as **`slab`** on **`Bluefors4`** (the XMLs hard-code
  this user; see "Adapting to a different user" at the bottom if that
  changes).
* The venv at `C:/Users/slab/Code/BF4_BOT/.venv/` exists and contains
  `Scripts/pythonw.exe`.
* All bot dependencies are installed in that venv (run
  `python run_bot.py --status` once to confirm the bot can read the fridge).

---

## Install (one-time) -- pick ONE of the two methods

### Method A: GUI import (recommended if this is your first time)

1. Press the Windows key, type `Task Scheduler`, press Enter.
2. In the left pane, click **Task Scheduler Library** to select it.
3. In the right pane (Actions), click **Import Task...**.
4. Navigate to `C:\Users\slab\Code\BF4_BOT\scheduler\` and open
   `BF4Bot-Main.xml`.
5. The "Create Task" dialog appears with everything pre-filled. Review,
   then click **OK**. If it prompts for credentials, enter your Windows
   password.
6. Repeat steps 3-5 for `BF4Bot-Watchdog.xml`.

You should now see both `BF4Bot-Main` and `BF4Bot-Watchdog` in the task
library list.

### Method B: Command line (faster once you are used to it)

Open a Git Bash terminal in the project directory, then:

```bash
schtasks /Create /XML scheduler/BF4Bot-Main.xml     /TN BF4Bot-Main     /F
schtasks /Create /XML scheduler/BF4Bot-Watchdog.xml /TN BF4Bot-Watchdog /F
```

Flag reference:
* `/Create` -- register a new task.
* `/XML path` -- read the task definition from this XML.
* `/TN name` -- the name the task will appear under in Task Scheduler.
* `/F` -- force; overwrite if a task with that name already exists.

If it asks for your Windows password, that is expected -- Task Scheduler
needs it to store the task as a scheduled-for-this-user action.

---

## Test that it works (BEFORE trusting it to auto-start)

### 1. Start both tasks manually

```bash
schtasks /Run /TN BF4Bot-Main
schtasks /Run /TN BF4Bot-Watchdog
```

Within a few seconds you should see:
* The **watchdog widget** appear in the bottom-right corner of your primary
  monitor. It may briefly show `OFFLINE` if the bot is still starting up,
  then flip to `RUNNING` once the first heartbeat is written (within 30s
  of the main bot starting).
* A new file `bf4bot.log` in the project directory that is growing over
  time -- this is the bot's stdout/stderr.
* A new file `bot_heartbeat.txt` that updates every 30s.

### 2. Confirm the processes are alive

```bash
tasklist | grep -i pythonw
```

You should see **two** `pythonw.exe` processes -- one for the bot, one for
the watchdog. If you only see one, something is wrong; check
`bf4bot.log` (for the bot) or `watchdog.log` (for the widget).

### 3. Send yourself a test report from Slack

DM the bot: `report 30min`. If you get a plot back, everything is wired
correctly.

### 4. Simulate a bot crash

Use Task Manager (Ctrl+Shift+Esc) or the command line to kill the MAIN bot:

```bash
# Find its PID
tasklist | grep pythonw
# Kill just the bot (not the widget!)
taskkill /PID <pid-of-run_bot> /F
```

Within about a minute you should see:
* The widget dot turns **red** and the label changes to `OFFLINE`.
* Task Scheduler relaunches the bot (because of the `RestartOnFailure`
  block in the XML).
* The widget dot turns **green** again.
* Your Slack DM gets `:warning: BF4-bot appears to be OFFLINE` followed
  shortly after by `:white_check_mark: BF4-bot is back online.`

If any of that does not happen, see Troubleshooting below.

### 5. The real test: log off and back on

Log out of Windows, then log back in. Both tasks should launch
automatically. Within ~30 seconds the widget should appear and show
`RUNNING`.

---

## Managing the tasks day-to-day

| What you want | Command |
|---------------|---------|
| Check status | `schtasks /Query /TN BF4Bot-Main /V /FO LIST` |
| Start manually | `schtasks /Run /TN BF4Bot-Main` |
| Stop | `schtasks /End /TN BF4Bot-Main` |
| Temporarily disable | `schtasks /Change /TN BF4Bot-Main /DISABLE` |
| Re-enable | `schtasks /Change /TN BF4Bot-Main /ENABLE` |
| Delete entirely | `schtasks /Delete /TN BF4Bot-Main /F` |

Same commands work for `BF4Bot-Watchdog`. The bot and widget are fully
independent -- stopping one does not affect the other.

**Day-to-day you should not need any of these commands.** The widget on
your desktop tells you everything:
* Visible and green = all healthy.
* Visible and red = bot crashed, Task Scheduler is restarting it, you get
  a Slack DM.
* Not visible = the widget itself is dead -- go investigate.

---

## Uninstall

```bash
schtasks /Delete /TN BF4Bot-Main     /F
schtasks /Delete /TN BF4Bot-Watchdog /F
```

That removes the tasks. Running processes are left alone; kill them
manually with Task Manager if needed. No files are deleted.

---

## Troubleshooting

**"Task Scheduler cannot register the task" when importing**
: The `<UserId>` in the XML does not match your Windows account. See
  "Adapting to a different user" below.

**Tasks imported but nothing starts**
: Check that Task Scheduler actually enabled the task --
  `schtasks /Query /TN BF4Bot-Main /V /FO LIST` and look for
  `Status: Ready`. If it says `Disabled`, run
  `schtasks /Change /TN BF4Bot-Main /ENABLE`.

**Widget never appears**
: Run it by hand in a terminal first: `.venv/Scripts/python.exe watchdog.py`.
  This uses the CONSOLE python (not pythonw) so you see any error traceback.
  Fix the error, then re-try via Task Scheduler.

**Bot starts but dies immediately**
: Read `bf4bot.log` -- that is what stdout/stderr of the bot get redirected
  to when launched by pythonw. Most common failure: missing
  `keys.yaml` or stale Slack tokens.

**Both processes running, widget shows OFFLINE forever**
: Delete `bot_heartbeat.txt` and restart the main bot:
  `schtasks /End /TN BF4Bot-Main && schtasks /Run /TN BF4Bot-Main`.
  This forces the bot to write a fresh heartbeat.

---

## Adapting to a different user or PC

The XMLs contain `Bluefors4\slab` in three places each. If you ever need
to install on a different account or PC, edit the XMLs before importing:

```bash
# Replace with your own hostname\username:
sed -i 's/Bluefors4\\\\slab/NEWPC\\\\newuser/g' scheduler/*.xml
```

Notes:
* The `\\\\` in the sed pattern is bash escaping `\\` which is XML escaping
  `\`. The actual XML contains a single backslash.
* You may also need to update the hard-coded path
  `C:\Users\slab\Code\BF4_BOT` in `<Command>` and `<WorkingDirectory>` if
  the project lives elsewhere.

After editing, re-import with the methods above.
