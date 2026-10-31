@echo off
REM BF4_Bot.bat -- Start the main bot via Task Scheduler.
REM
REM Uses schtasks /Run instead of launching pythonw directly.  This way
REM Task Scheduler's IgnoreNew policy prevents duplicates automatically
REM -- no fragile wmic process-checking needed.

cd /d "C:\Users\slab\Bluefors logs\bf4_bot"

REM Remove shutdown flag (written by widget's STOP BOT) so the bot
REM does not immediately exit on startup.
if exist bot_shutdown.flag del bot_shutdown.flag

REM Re-enable the scheduler task (STOP BOT disables it).
schtasks /Change /TN BF4Bot-Main /ENABLE >NUL 2>&1

REM Launch via scheduler.  If the bot is already running, scheduler
REM silently ignores this (MultipleInstancesPolicy: IgnoreNew).
schtasks /Run /TN BF4Bot-Main >NUL 2>&1

echo BF4 Bot starting...
timeout /t 2 >NUL
