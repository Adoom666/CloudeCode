"""The v4 -> v5 migration step: repairing a wrapper that never forwards args.

Why this step exists: ``render_wrapper_invocation`` launches a wrapper as
``source <file> "$@"``, so the caller's extra arguments arrive as the
sourced script's positional parameters. The v0->v1 seed wrote the default
wrapper's script WITHOUT a ``"$@"``, so those parameters were set and never
read. ``--resume``, ``--fork-session`` and ``--name`` were all discarded in
silence: the restart button opened an empty conversation and a fork was
recorded as a fork while actually being a fresh session. Both look like
success from the outside, which is why it survived to v1.0.31.

Covers: the repair itself, every shape the step must REFUSE to touch
(``entry`` present, args already referenced in each accepted form, a last
line that does not launch claude, a commented last line, non-dict and
non-string junk), idempotency at both step and end-to-end level, and
non-mutation of the caller's dict.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.config_migration import CURRENT_CONFIG_VERSION, migrate_config_dict
from src.core.config_migration_steps import _step_v4_to_v5


def _cfg(wrappers):
    return {"config_version": 4, "agents": {"wrappers": wrappers}}


def _scripts(data):
    return [w.get("script") for w in data["agents"]["wrappers"]]


# --- the repair -----------------------------------------------------------

def test_appends_args_to_the_default_seed():
    out = _step_v4_to_v5(_cfg([
        {"id": "claude-skip-permissions",
         "script": "command claude --dangerously-skip-permissions",
         "entry": None},
    ]))
    assert _scripts(out) == ['command claude --dangerously-skip-permissions "$@"']


def test_appends_to_the_last_line_only():
    script = "# preamble\nexport FOO=1\ncommand claude --model x"
    out = _step_v4_to_v5(_cfg([{"id": "w", "script": script, "entry": None}]))
    assert _scripts(out) == ['# preamble\nexport FOO=1\ncommand claude --model x "$@"']


# --- everything it must refuse to touch -----------------------------------

def test_leaves_a_wrapper_with_an_entry_alone():
    # An entry forwards through its own function call already.
    script = "cld() (\n  command claude\n)"
    out = _step_v4_to_v5(_cfg([{"id": "cld", "script": script, "entry": "cld"}]))
    assert _scripts(out) == [script]


def test_leaves_every_already_forwarding_form_alone():
    for token in ('"$@"', "$@", "$1", "$*"):
        script = f"command claude {token}"
        out = _step_v4_to_v5(_cfg([{"id": "w", "script": script, "entry": None}]))
        assert _scripts(out) == [script], token


def test_leaves_a_last_line_that_does_not_launch_claude_alone():
    script = "command claude\necho done"
    out = _step_v4_to_v5(_cfg([{"id": "w", "script": script, "entry": None}]))
    assert _scripts(out) == [script]


def test_leaves_a_commented_last_line_alone():
    script = "export FOO=1\n# command claude"
    out = _step_v4_to_v5(_cfg([{"id": "w", "script": script, "entry": None}]))
    assert _scripts(out) == [script]


def test_tolerates_junk_shapes():
    for wrappers in ([{"id": "w"}], [{"id": "w", "script": ""}],
                     [{"id": "w", "script": 42}], ["not-a-dict"], "not-a-list"):
        out = _step_v4_to_v5({"agents": {"wrappers": wrappers}})
        assert out["agents"]["wrappers"] == wrappers
    assert _step_v4_to_v5({}) == {}
    assert _step_v4_to_v5({"agents": "junk"}) == {"agents": "junk"}


# --- invariants -----------------------------------------------------------

def test_does_not_mutate_the_caller_dict():
    src = _cfg([{"id": "w", "script": "command claude", "entry": None}])
    before = copy.deepcopy(src)
    _step_v4_to_v5(src)
    assert src == before


def test_step_is_idempotent():
    once = _step_v4_to_v5(_cfg([
        {"id": "w", "script": "command claude", "entry": None}]))
    twice = _step_v4_to_v5(once)
    assert _scripts(twice) == _scripts(once) == ['command claude "$@"']


def test_end_to_end_reaches_current_version_and_repairs():
    out, changed = migrate_config_dict(_cfg([
        {"id": "claude-skip-permissions",
         "script": "command claude --dangerously-skip-permissions",
         "entry": None}]), True, True)
    assert changed is True
    assert out["config_version"] == CURRENT_CONFIG_VERSION
    assert _scripts(out) == ['command claude --dangerously-skip-permissions "$@"']
    # Running the whole chain again changes nothing.
    assert migrate_config_dict(out, True, True)[0] == out
