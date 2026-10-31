# BF4 Bot -- Setup Walkthrough (new PC / fridge / Slack)

Step-by-step guide for someone who has cloned this repo and wants to run
the bot on a DIFFERENT PC, a different Bluefors fridge, or a different
Slack workspace.  If you are the original operator on the original
fridge, you probably don't need this file -- you already have everything
running.

---

## Prerequisites

* **Windows 10/11** -- the Task Scheduler auto-start and `pythonw.exe`
  headless mode are Windows-specific.
* **Python 3.10+** installed.  We recommend miniconda but any CPython
  works.
* **Git for Windows** (provides Git Bash).
* **Bluefors Control Software** running on this PC, with its REST API
  enabled.
* **Slack workspace admin access** -- you need to create a Slack app.

---

## Step 1: Clone the repo

```bash
git clone <repo-url> C:/Users/<you>/Code/BF4_BOT
cd C:/Users/<you>/Code/BF4_BOT
```

---

## Step 2: Create and activate the Python virtual environment

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install slack_bolt slack_sdk pyyaml requests matplotlib
```

Verify: `python -c "import slack_bolt; print('ok')"`.

---

## Step 3: Create a Slack app

1. Go to https://api.slack.com/apps and click "Create New App" ->
   "From scratch".
2. Name it (e.g. "BF4-bot"), pick your workspace.
3. Under **OAuth & Permissions**, add these Bot Token Scopes:
   * `chat:write`
   * `files:write`
   * `channels:read`
   * `groups:read`
   * `im:read`
   * `im:write`
   * `im:history`
   * `mpim:read`
   * `users:read`
   * `app_mentions:read`
4. Install the app to the workspace.  Copy the **Bot User OAuth Token**
   (`xoxb-...`).
5. Under **Socket Mode**, enable it.  Generate an **App-Level Token**
   with the `connections:write` scope.  Copy it (`xapp-...`).
6. Under **Event Subscriptions**, enable events.  Subscribe to bot events:
   * `message.im` (for DM handling)
   * `app_mention` (for @mention handling in channels)
7. Invite the bot to any channel you want it to post reports / alarms
   into (e.g. `/invite @BF4-bot` in the channel).

---

## Step 4: Get the Bluefors API key

1. Open the Bluefors Control Software on the fridge PC.
2. Settings -> API -> Generate a READ-ONLY key.
3. Copy the key string.

---

## Step 5: Fill in the config files

All config lives in `config/`.  You need to create or edit these:

### config/keys.yaml (gitignored -- never committed)

```yaml
bluefors_api_key: "<paste your Bluefors read-only API key>"
slack_bot_token: "xoxb-<paste from step 3>"
slack_app_token: "xapp-<paste from step 3>"
```

### config/channels.yaml (gitignored)

```yaml
# Who is the operator?
operator: your_name

# Slack channel IDs (get from channel details -> About -> bottom)
my_lab_channel: C0XXXXXXXXX

# DM-able users (get user ID from Slack profile -> three-dot -> Copy member ID)
dm_users:
  your_name: U0XXXXXXXXX
```

### config/alarm_config.yaml

Edit `alarm_destinations:` to list the names you defined in
`channels.yaml` above.  Add `operator_dm`, any `dm_<nickname>`, or any
channel key name.

### config/report_config.yaml

Same as above but for scheduled reports.

### config/hardware.yaml

Change `api_host`, `api_port`, `log_prefix` to match your fridge:

```yaml
api_host: "https://localhost"     # usually localhost, unless remote
api_port: "49098"                 # Bluefors default
log_prefix: "S:/_BlueFors4/Logs/" # your fridge's log file path
```

The channel mappings in `bf4bot/reader.py` (`CH1`=50K, `CH2`=4K,
`CH5`=Still, `CH6`=MXC) are BF4-specific.  If your fridge uses
different channel assignments, edit the mapping in `reader.py::read_history`
and `reader.py::get_temperature`.

### config/alarms.yaml and config/reports.yaml

Edit via the watchdog GUI (recommended), or hand-edit the YAML.  Start
with the defaults and tune thresholds for your fridge.

---

## Step 6: Smoke test

```bash
source .venv/Scripts/activate
python run_bot.py --status
```

If this prints live temperatures, pressures, and pump states, the
Bluefors API connection works.

```bash
python run_bot.py --report
```

If this sends a 24h fridge plot to Slack, everything is wired.

---

## Step 7: Install Task Scheduler auto-start

Edit `scheduler/BF4Bot-Main.xml` and `scheduler/BF4Bot-Watchdog.xml`:
* Change `<UserId>` to your Windows domain and username.
* Change `<Command>` to the path of your venv's `pythonw.exe`.
* Change `<WorkingDirectory>` to your project folder.

Then re-encode and register:

```bash
source .venv/Scripts/activate
python scheduler/convert_xml_utf16.py
schtasks /Create /XML scheduler/BF4Bot-Main.xml     /TN BF4Bot-Main     /F
schtasks /Create /XML scheduler/BF4Bot-Watchdog.xml /TN BF4Bot-Watchdog /F
```

Test manually:

```bash
schtasks /Run /TN BF4Bot-Main
schtasks /Run /TN BF4Bot-Watchdog
```

The widget should appear.  Log out and back in to confirm auto-start.

---

## Step 8: Copy the desktop shortcut

```bash
cp scheduler/BF4_Watchdog.bat "$USERPROFILE/Desktop/"
```

Edit the `.bat` if your project path is different from what's inside.

---

## Things you'll likely want to customize

| Item | Where | Notes |
|------|-------|-------|
| Alarm thresholds | config/alarms.yaml (or GUI) | Fridge-specific |
| Temperature channel mapping | bf4bot/reader.py | CH1=50K etc. is BF4-specific |
| Turbo pump API paths | bf4bot/reader.py | Pfeiffer TC400 paths; other makes differ |
| Report schedule | config/reports.yaml (or GUI) | Hour, days, type |
| Plot colors | bf4bot/plotting.py | Bluefors-native colors hardcoded |
| Widget size/colors | watchdog.py top constants | Aesthetic preference |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `--status` prints nothing | API key or host wrong | Check hardware.yaml + keys.yaml |
| Bot starts but DMs don't work | Socket Mode not enabled | Check Slack app settings |
| Reports have no data | Log path wrong or S: not mounted | Check hardware.yaml log_prefix |
| Task Scheduler won't import XML | Wrong encoding | Run convert_xml_utf16.py |
| Widget never appears | pythonw path wrong in XML | Check `<Command>` in XML |
| Alarms never fire | Thresholds too far from real values | DM `status` to see current values |
