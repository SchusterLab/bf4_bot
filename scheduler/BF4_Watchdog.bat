@echo off
REM BF4_Watchdog.bat -- Start the watchdog widget via Task Scheduler.
REM
REM Uses schtasks /Run instead of launching pythonw directly.  Task
REM Scheduler's IgnoreNew policy prevents duplicate widgets.

schtasks /Change /TN BF4Bot-Watchdog /ENABLE >NUL 2>&1
schtasks /Run /TN BF4Bot-Watchdog >NUL 2>&1

echo BF4 Watchdog starting...
timeout /t 2 >NUL
