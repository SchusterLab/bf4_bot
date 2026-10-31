"""
Tests for bf4bot.logging_setup -- Stage 1 of the print() -> logger
migration (issue #12 + #23).

Covers the edge cases flagged during plan review:

  1. Rotation actually happens when file exceeds maxBytes.
  2. Backup cascade writes .1, .2, ... up to BACKUP_COUNT.
  3. Multi-line print("foo\\nbar") splits into two log records.
  4. logger.propagate is False (no recursion via root logger).
  5. pythonw-guard: shim only installs under pythonw / missing streams.
  6. Idempotent install -- calling twice does not stack handlers.

Each test uses tmp_path so no real bf4bot.log is touched.  Tests that
swap sys.stdout carefully save/restore it; pytest's capsys is not used
because we ARE the thing being tested.

Run:
    python -m pytest tests/test_log_rotation.py -v
"""

import logging
import os
import sys

import pytest


# Make bf4bot importable when tests are run from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bf4bot import logging_setup  # noqa: E402


@pytest.fixture
def clean_logger():
    """Reset the bf4bot logger between tests.

    The logger is a module-level singleton so handlers installed by one
    test persist into the next unless we scrub them.  Also saves/restores
    sys.stdout and sys.stderr because install_log_shim swaps them.
    """
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    logger = logging.getLogger("bf4bot")
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    saved_level = logger.level
    # Start each test with a clean logger.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    yield logger
    # Restore.
    sys.stdout = saved_stdout
    sys.stderr = saved_stderr
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        logger.addHandler(h)
    logger.propagate = saved_propagate
    logger.setLevel(saved_level)


@pytest.fixture
def force_headless(monkeypatch):
    """Force install_log_shim to treat the test as headless.

    Normally the shim is a no-op when a real TTY is present.  We need it
    to actually install so we can test it.  Replace sys.stdout with a
    dummy that claims to not be a TTY, and spoof sys.executable to look
    like pythonw.
    """
    monkeypatch.setattr(sys, "executable", "C:/fake/venv/pythonw.exe")


def test_rotation_triggers_at_maxbytes(tmp_path, clean_logger, force_headless,
                                       monkeypatch):
    """When the active log exceeds maxBytes, a rotation creates .log.1."""
    # Shrink maxBytes to something tiny so we can overflow it fast.
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 200)
    log_path = tmp_path / "test.log"
    logging_setup.install_log_shim(str(log_path))

    # Each print writes ~50 chars + newline.  20 prints = ~1 KB -- well
    # past 200 bytes, so at least one rotation should happen.
    for i in range(20):
        print(f"rotation test line number {i:03d} padding padding")

    # Close handlers so Windows releases the file locks before asserts.
    for h in list(logging.getLogger("bf4bot").handlers):
        h.close()

    assert log_path.exists(), "active log file should exist"
    rotated = tmp_path / "test.log.1"
    assert rotated.exists(), "at least one rotation should have occurred"


def test_backup_cascade(tmp_path, clean_logger, force_headless, monkeypatch):
    """Enough writes create .log.1 through .log.N (up to BACKUP_COUNT)."""
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 100)
    monkeypatch.setattr(logging_setup, "BACKUP_COUNT", 3)
    log_path = tmp_path / "cascade.log"
    logging_setup.install_log_shim(str(log_path))

    # Write enough to force several rotations.  Each line ~60 bytes,
    # maxBytes=100 means ~2 lines per rotation, so 50 lines -> plenty.
    for i in range(50):
        print(f"cascade line {i:03d} with some padding content here")

    for h in list(logging.getLogger("bf4bot").handlers):
        h.close()

    # BACKUP_COUNT=3 means .log.1, .log.2, .log.3 exist; .log.4 should not.
    assert (tmp_path / "cascade.log").exists()
    assert (tmp_path / "cascade.log.1").exists()
    assert (tmp_path / "cascade.log.2").exists()
    assert (tmp_path / "cascade.log.3").exists()
    assert not (tmp_path / "cascade.log.4").exists(), \
        "backup count should cap at BACKUP_COUNT"


def test_multiline_print_splits(tmp_path, clean_logger, force_headless):
    """print('foo\\nbar') writes two distinct log lines, not one."""
    log_path = tmp_path / "multiline.log"
    logging_setup.install_log_shim(str(log_path))
    print("alpha\nbeta\ngamma")

    for h in list(logging.getLogger("bf4bot").handlers):
        h.flush()
        h.close()

    contents = log_path.read_text(encoding="utf-8").strip().splitlines()
    # Each of the 3 tokens should be on its own line.  There may be more
    # lines if earlier prints leaked in, but these three must be present
    # as separate entries and in order.
    assert "alpha" in contents
    assert "beta" in contents
    assert "gamma" in contents
    a, b, g = (contents.index("alpha"),
               contents.index("beta"),
               contents.index("gamma"))
    assert a < b < g, "multiline order should be preserved"


def test_propagate_is_false(tmp_path, clean_logger, force_headless):
    """bf4bot logger must NOT propagate to root, or stderr loops forever."""
    log_path = tmp_path / "propagate.log"
    logging_setup.install_log_shim(str(log_path))
    logger = logging.getLogger("bf4bot")
    assert logger.propagate is False


def test_no_install_with_real_stdout(tmp_path, clean_logger, monkeypatch):
    """Under python (not pythonw) with real stdout, shim does nothing."""
    # Spoof a non-pythonw executable.  sys.stdout is already real here
    # (pytest's capture or terminal).  The shim's _should_install_shim
    # check should return False.
    monkeypatch.setattr(sys, "executable", "C:/real/python.exe")
    saved_stdout = sys.stdout
    log_path = tmp_path / "nodev.log"

    logging_setup.install_log_shim(str(log_path))

    # sys.stdout should NOT have been swapped.
    assert sys.stdout is saved_stdout, \
        "shim should not touch stdout when a real terminal is present"
    # No log file should have been opened.
    assert not log_path.exists(), \
        "no log file should be created in dev terminal mode"


def test_idempotent_install(tmp_path, clean_logger, force_headless):
    """Calling install_log_shim twice does not stack handlers."""
    log_path = tmp_path / "idem.log"
    logging_setup.install_log_shim(str(log_path))
    logger = logging.getLogger("bf4bot")
    handler_count_first = len(logger.handlers)

    # Second call should be a no-op.
    logging_setup.install_log_shim(str(log_path))
    handler_count_second = len(logger.handlers)

    assert handler_count_first == 1
    assert handler_count_second == 1, \
        "re-installing the shim must not add a duplicate handler"
