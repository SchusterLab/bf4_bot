"""
One-shot migration: move root-level YAML files into a config/ subfolder
and split keys.yaml into a tokens/user-id file and a channels-IDs file.

Before:
    BF4_BOT/
    |-- keys.yaml            # api key + tokens + bot_operator + channel ids
    |-- config.yaml
    |-- alarms.yaml

After:
    BF4_BOT/
    |-- config/
    |   |-- keys.yaml        # api key + tokens + bot_operator  (sensitive)
    |   |-- channels.yaml    # channel IDs only  (user-editable)
    |   |-- config.yaml
    |   |-- alarms.yaml

This script is IDEMPOTENT and SAFE:
  * If the new files already exist, it does not overwrite them.
  * It does NOT delete any root-level files -- the code change makes the
    bot read from config/ only, and the user manually deletes the root
    copies once they have verified everything works.

Any time you want to re-do the migration from the current root files,
move or delete config/ first.

Run:
    python scripts/migrate_to_config_folder.py
"""

import os
import shutil
import sys
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG_DIR = os.path.join(ROOT, "config")

# Keys that belong in keys.yaml (sensitive: tokens + identity).
SECRET_KEYS = {
    "bluefors_api_key",
    "slack_bot_token",
    "slack_app_token",
    "bot_operator_user_id",
}

# Everything else from the old flat keys.yaml goes into channels.yaml --
# typically channel IDs (fluxonium_kids_channel, etc).


def move_if_missing(src, dst):
    """Copy src to dst only if dst does not already exist."""
    if not os.path.exists(src):
        print(f"  [skip]  source missing: {src}")
        return False
    if os.path.exists(dst):
        print(f"  [skip]  target exists (not overwriting): {dst}")
        return False
    shutil.copy2(src, dst)
    print(f"  [copy]  {src}  ->  {dst}")
    return True


def split_keys(src, keys_dst, channels_dst):
    """Split the old flat keys.yaml into two files."""
    if os.path.exists(keys_dst) and os.path.exists(channels_dst):
        print(f"  [skip]  both split files exist, not overwriting")
        return False
    if not os.path.exists(src):
        print(f"  [skip]  source missing: {src}")
        return False
    with open(src, "r") as f:
        data = yaml.safe_load(f) or {}
    keys_part = {k: v for k, v in data.items() if k in SECRET_KEYS}
    channels_part = {k: v for k, v in data.items() if k not in SECRET_KEYS}

    if not os.path.exists(keys_dst):
        with open(keys_dst, "w") as f:
            yaml.dump(keys_part, f, default_flow_style=False, sort_keys=False)
        print(f"  [write] {keys_dst}  ({len(keys_part)} entries)")
    if not os.path.exists(channels_dst):
        # Seed the dm_users map with the current operator so the multi-DM
        # machinery works out of the box.  The operator's user ID stays in
        # keys.yaml too (it is the watchdog's default DM target).
        operator_uid = data.get("bot_operator_user_id")
        dm_users = {}
        if operator_uid:
            # Default nickname "operator".  User can rename or add more.
            dm_users["operator"] = operator_uid

        # Build the full channels.yaml structure by hand so we control the
        # ordering (channels first, then dm_users block).
        header = (
            "# Slack channel / user IDs used for alarm destinations and\n"
            "# report routing.  SAFE TO EDIT -- these are not credentials.\n"
            "#\n"
            "# Channels (IDs start with C or G).  Add new channels here --\n"
            "# get the ID from Slack (channel details -> bottom of About).\n"
            "# After adding, list the new key name in config/config.yaml\n"
            "# under 'alarm_destinations:' so the alarm GUI picks it up.\n"
            "#\n"
            "# dm_users -- users the bot can DM.  The alarm destination\n"
            "# name is 'dm_<nickname>', e.g. 'dm_chuyao' resolves to DM\n"
            "# that user's Slack account.  Nicknames are arbitrary; the\n"
            "# user ID (starts with U) is what matters.\n"
            "\n"
        )
        channels_only = {k: v for k, v in channels_part.items() if k != "dm_users"}
        with open(channels_dst, "w") as f:
            f.write(header)
            if channels_only:
                yaml.dump(channels_only, f, default_flow_style=False, sort_keys=False)
                f.write("\n")
            f.write("dm_users:\n")
            if dm_users:
                for nick, uid in dm_users.items():
                    f.write(f"  {nick}: {uid}\n")
            else:
                f.write("  # operator: U065...\n")
        print(f"  [write] {channels_dst}  ({len(channels_only)} channel(s), "
              f"{len(dm_users)} DM user(s))")
    return True


def main():
    print(f"Ensuring {CONFIG_DIR}/ exists...")
    os.makedirs(CONFIG_DIR, exist_ok=True)

    print("Migrating plain-copy files:")
    move_if_missing(os.path.join(ROOT, "config.yaml"),
                    os.path.join(CONFIG_DIR, "config.yaml"))
    move_if_missing(os.path.join(ROOT, "alarms.yaml"),
                    os.path.join(CONFIG_DIR, "alarms.yaml"))

    print("Splitting keys.yaml into keys.yaml + channels.yaml:")
    split_keys(
        os.path.join(ROOT, "keys.yaml"),
        os.path.join(CONFIG_DIR, "keys.yaml"),
        os.path.join(CONFIG_DIR, "channels.yaml"),
    )

    print()
    print("Done.  Original root-level files are left in place.")
    print("After verifying the bot works with the new paths, delete:")
    print("  keys.yaml, config.yaml, alarms.yaml (at project root)")


if __name__ == "__main__":
    sys.exit(main())
