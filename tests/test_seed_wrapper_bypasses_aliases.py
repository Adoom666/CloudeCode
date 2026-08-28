"""The seeded plain-claude wrapper must not be hijackable by a shell alias.

THE MECHANISM. Every wrapper runs inside ``zsh -c 'source ~/.zshrc; ...'``
because sourcing the rc is how the pane inherits the user's PATH and their
own shell FUNCTIONS (``cld``, ``cldor``). Sourcing it also brings in their
ALIASES, and a bare ``claude`` then resolves to whatever alias they have.

MEASURED, NOT IMAGINED. A real install carried::

    alias claude="security unlock-keychain ~/Library/Keychains/login.keychain-db && claude"

so every session the seeded wrapper launched blocked forever on an
interactive keychain password prompt, in a pane with nobody to type it.
The chain from there is the expensive part: Claude never started, so its
SessionStart hook never fired, so ``claude_session_uuid`` was never bound,
so resume and fork could not work at all. One alias, and the whole session
identity feature was dead.

WHERE ``command`` IS RIGHT AND WHERE IT IS WRONG. It bypasses aliases AND
functions. That is exactly right for the row that means "the plain CLI, no
wrapper", and exactly WRONG for a wrapper whose entire purpose is to call
a function the rc defines - ``cld`` and ``cldor`` are functions, and
``command cld`` would fail to find them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.agent_wrappers import EXAMPLE_WRAPPERS
from src.core.config_migration_steps import build_seed_wrappers


def _seed(has_cld=False, has_cldor=False):
    """The seeded wrapper list, tolerating either signature shape."""
    try:
        return build_seed_wrappers(has_cld=has_cld, has_cldor=has_cldor)
    except TypeError:
        return build_seed_wrappers(has_cld)


def test_the_plain_claude_seed_bypasses_aliases():
    """THE FIX. A bare `claude` here is hijackable by the user's own rc."""
    plain = [w for w in _seed() if w["id"] == "claude"]
    assert plain, "the seed no longer contains a plain claude wrapper"
    script = plain[0]["script"]
    assert script.startswith("command claude"), (
        f"seeded plain-claude script is {script!r}; without `command` a "
        "user's `alias claude=...` hijacks every session this launches"
    )


def test_the_seeded_script_still_skips_permissions():
    """The behaviour that was there before must be unchanged."""
    plain = [w for w in _seed() if w["id"] == "claude"][0]
    assert "--dangerously-skip-permissions" in plain["script"]


def test_function_wrappers_do_NOT_use_command():
    """``command`` bypasses FUNCTIONS too, which would break cld/cldor.

    Those wrappers exist precisely to call a function the user's rc
    defines. Adding `command` to them would make them unfindable - the
    opposite failure, and just as total.
    """
    for w in _seed(has_cld=True, has_cldor=True):
        if w["id"] in ("cld", "cldor"):
            entry = (w.get("entry") or "")
            assert not entry.startswith("command "), (
                f"{w['id']} calls a shell FUNCTION; `command` cannot find it"
            )


def test_the_offered_examples_already_bypass_aliases():
    """The example wrappers got this right first; keep it that way."""
    for raw in EXAMPLE_WRAPPERS:
        script = raw.get("script") or ""
        if "claude" in script and raw.get("family") in ("claude", "local"):
            # Every invocation of the BINARY inside an example is guarded.
            for line in script.splitlines():
                stripped = line.strip()
                if stripped.startswith("claude ") or stripped == "claude":
                    raise AssertionError(
                        f"{raw['id']} invokes a bare `claude`: {stripped!r}"
                    )
