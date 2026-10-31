"""
Regression guard for the channel @mention suppress/unsuppress NameError.

run_bot.run_daemon defines two sibling nested handlers:
  * handle_mention   -- has its own `raw_parts` local
  * _dispatch_mention -- referenced `raw_parts` WITHOUT binding it

A sibling function's locals are NOT in scope, so `@BF4-bot suppress <name>`
and `@BF4-bot unsuppress <name>` in a channel raised NameError (the same
commands worked in DMs, which use handle_dm's own raw_parts).

These are static scope checks -- no Slack stack needed.  They parse the
real run_bot.py with the stdlib `symtable` module (exact compiler scope
analysis) and assert _dispatch_mention binds every argument token it uses.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_mention_scope.py -v
"""

import os
import symtable

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_RUN_BOT = os.path.join(_REPO_ROOT, "run_bot.py")


def _child(table, name):
    for c in table.get_children():
        if c.get_name() == name:
            return c
    return None


def _dispatch_symtable():
    with open(_RUN_BOT, encoding="utf-8") as f:
        src = f.read()
    top = symtable.symtable(src, "run_bot.py", "exec")
    run_daemon = _child(top, "run_daemon")
    assert run_daemon is not None, "run_daemon function not found"
    dispatch = _child(run_daemon, "_dispatch_mention")
    assert dispatch is not None, "_dispatch_mention not found in run_daemon"
    return dispatch


def test_dispatch_mention_binds_raw_parts_locally():
    """raw_parts must be a local of _dispatch_mention, not a stray global.

    Before the fix the compiler classified `raw_parts` as an (implicit)
    global lookup, which blew up at runtime with NameError.
    """
    dispatch = _dispatch_symtable()
    sym = dispatch.lookup("raw_parts")
    assert sym.is_local(), (
        "raw_parts must be local to _dispatch_mention (derived from the "
        "case-preserving `stripped` param); a sibling handler's local is "
        "not in scope"
    )
    assert not sym.is_global(), "raw_parts should not resolve to a global"


def test_dispatch_mention_uses_case_preserving_source():
    """`stripped` (case-preserving) must be a parameter it can split.

    Guards against a 'fix' that reaches for the lowercased `parts`, which
    would mangle case-sensitive alarm names (e.g. P4_high) in the reply.
    """
    dispatch = _dispatch_symtable()
    assert "stripped" in dispatch.get_parameters()
