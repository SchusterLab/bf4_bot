# BF4-bot -- Slack App Description

Paste the text below into the Slack app's "About" section (api.slack.com
-> Your Apps -> BF4-bot -> Basic Information -> Display Information ->
Short Description / Long Description).

---

## Short description (for Slack sidebar)

BF4 fridge monitor -- DM me for live status, reports, and alarm info.

---

## Long description (for the app's About page)

BF4-bot monitors the BF4 Bluefors dilution refrigerator 24/7. It is
strictly read-only -- it never controls the fridge.

**Anyone in the workspace can DM the bot:**

  status             -- live temperatures, pressures, pump states
  report             -- 24h fridge plot (PNG)
  report 30min       -- custom-window fridge plot
  report 7d          -- week-long plot
  compressor         -- compressor status plot
  compressor 6h      -- custom window
  turbo              -- turbo pump plot
  alarms             -- list active alarm definitions
  help               -- show commands

You can also @mention the bot in any channel it's invited to; it replies
in-thread.

**Automated features (managed on the fridge PC, not from Slack):**

  * Scheduled daily reports to configured channels.
  * Configurable alarms with per-alarm thresholds, cooldowns, and routing.
  * Watchdog offline/online DMs to the operator if the bot goes down.

Alarm thresholds, report schedules, and routing destinations are
configured via a GUI widget on the fridge PC or by editing YAML files
in the BF4_BOT project folder. These cannot be changed from Slack.

For setup details, technical documentation, or to contribute, see the
project README on GitHub.
