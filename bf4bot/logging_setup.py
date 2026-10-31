"""
Log setup for BF4 bot and watchdog.

Replaces the old raw ``open(path, "a")`` stdout/stderr redirect with a
``RotatingFileHandler`` from ``logging.handlers``.  Every ``print()`` in
the codebase keeps working unchanged -- we install a tiny shim that
routes stdout/stderr writes through a named logger, whose handler
rotates the log file when it grows past a size threshold.

Why a shim instead of migrating every ``print()`` to ``logger.info()``:
this is Stage 1 of the migration (tracking issue: #23).  The full
migration happens later.  Keeping the shim means we can close the
rotation bug (#12) in one small PR without touching 97 call sites.

Public API:
    install_log_shim(log_path) -> None

After the call, ``sys.stdout`` and ``sys.stderr`` route to a
RotatingFileHandler that:
  * rotates when the file exceeds ``MAX_BYTES`` (5 MB)
  * keeps ``BACKUP_COUNT`` historical rotations (``.log.1`` .. ``.log.5``)
  * is thread-safe (uses the handler's built-in lock)
  * uses UTF-8 encoding so Slack emoji in log messages do not crash writes

The function is idempotent: calling it twice does not install two
handlers.
"""

import logging
import logging.handlers
import os
import sys
import threading


# Tuning knobs.  5 MB per file x 5 backups = at most 30 MB of logs on
# disk (1 active + 5 rotated).  Notepad opens 5 MB files instantly.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

# Name of the logger we install and route prints through.  Same name
# used in both run_bot.py and watchdog.py so tests can grab it via
# logging.getLogger("bf4bot").
_LOGGER_NAME = "bf4bot"


class _LoggerStream:
    """
    File-like object that forwards ``write()`` calls to a ``logging.Logger``.

    Used to replace ``sys.stdout`` / ``sys.stderr`` so existing
    ``print()`` calls flow into the rotating file handler without
    changing any call sites.

    Quirks handled:
      * ``print("foo")`` calls ``.write("foo")`` and ``.write("\\n")``
        separately.  We skip the bare newline.
      * ``print("foo\\nbar")`` sends ``"foo\\nbar\\n"`` as one chunk --
        we split on newlines so each logical line is its own log record.
      * Re-entry guard: if the logger's handler itself raises and Python's
        logging module writes the error to stderr (which is us), we must
        not recurse forever.  ``_in_write`` blocks nested calls on the
        same thread.
    """

    def __init__(self, logger):
        self._logger = logger
        # threading.local so each thread gets its own re-entry flag.
        # Slack_bolt dispatches handlers on background threads, so per-
        # thread state matters here.
        self._local = threading.local()

    def write(self, msg):
        # Re-entry guard.  If we are already inside a write on this
        # thread, drop the message silently rather than recurse.
        if getattr(self._local, "in_write", False):
            return
        self._local.in_write = True
        try:
            # Split on newlines so each logical line becomes one log
            # record.  rstrip('\n') after split handles trailing newline
            # from print().  Skip empty strings so blank prints do not
            # clutter the log.
            for line in msg.splitlines():
                line = line.rstrip()
                if line:
                    self._logger.info(line)
        except Exception:
            # Swallow any error inside the shim.  A logging failure must
            # never crash the bot.
            pass
        finally:
            self._local.in_write = False

    def flush(self):
        # logging.Handler manages its own flushing; this is a no-op so
        # code that calls ``sys.stdout.flush()`` does not raise.
        pass

    def isatty(self):
        # Some libraries probe ``sys.stdout.isatty()`` to decide whether
        # to emit ANSI colour.  We are definitely not a TTY.
        return False


def _should_install_shim():
    """
    Return True if we are running headless (pythonw or missing std streams).

    Mirrors the condition from the old raw-redirect code in run_bot.py:
    under ``python run_bot.py`` in a terminal, stdout is a real TTY and
    we want prints to go there, not to the log file.  Only divert to the
    log file when there is no usable console.
    """
    exec_name = (
        os.path.basename(sys.executable).lower() if sys.executable else ""
    )
    return (
        sys.stdout is None
        or sys.stderr is None
        or exec_name.startswith("pythonw")
    )


def install_log_shim(log_path):
    """
    Configure the ``bf4bot`` logger and route stdout/stderr through it.

    Parameters
    ----------
    log_path : str
        Absolute path to the log file (e.g. ``/.../bf4bot.log``).

    Behaviour
    ---------
    * If the current process has a real console (normal
      ``python run_bot.py`` dev run), do nothing -- prints still go to
      the terminal.
    * Otherwise, install a ``RotatingFileHandler`` on the ``bf4bot``
      logger and redirect ``sys.stdout`` and ``sys.stderr`` to a shim
      that forwards writes to that logger.

    Idempotent: if the logger already has our handler attached, the
    function returns without doing anything.  Safe to call from both
    run_bot.py and watchdog.py in the same process (not our current
    deployment, but harmless).
    """
    if not _should_install_shim():
        return

    logger = logging.getLogger(_LOGGER_NAME)

    # Guard against double-install.  Re-running this function (e.g. in
    # tests, or on reload) should not stack handlers.
    for h in logger.handlers:
        if getattr(h, "_bf4bot_handler", False):
            return

    logger.setLevel(logging.INFO)
    # Do NOT propagate to the root logger.  Root's default handler would
    # write to real stderr, which after our swap is our shim -- infinite
    # recursion.
    logger.propagate = False

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    # Tag so we can detect re-install attempts (see loop above).
    handler._bf4bot_handler = True

    # Empty formatter during Stage 1 migration: existing prints already
    # embed their own timestamps like ``[2026-04-17 12:34:56] foo``.
    # Adding a formatter timestamp here would double-stamp.  Issue #23
    # Stage 5 turns on a real formatter once all prints are migrated.
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(handler)

    # Disable logging's default "print error to stderr" behaviour -- our
    # stderr is the shim that feeds back into the logger.  Errors inside
    # the handler are silenced; acceptable because the alternative is
    # infinite recursion on disk-full / permission errors.
    logging.raiseExceptions = False

    stream = _LoggerStream(logger)
    sys.stdout = stream
    sys.stderr = stream
