"""Every script the app INVOKES by path must be DELIVERED to the tree it invokes it from.

Why this file exists
--------------------
``nuke.sh`` shipped in the .app bundle but was deliberately left out of
``RESYNC_ALLOWLIST`` in ``macOS/bootstrap.js``, on the theory that a user
might have customized their copy. The consequence was that a first-run copy
landed once and then never moved again, while ``macOS/main.js`` kept executing
``path.join(serverManager.getProjectRoot(), 'nuke.sh')`` - the DERIVED tree.
So a fresh install got the new uninstaller and every UPGRADE got the new
typed-NUKE confirmation window wired to the OLD destructive script: the one
that missed the state directory and left ``cloude.db`` and
``refresh_tokens.db`` on disk while reporting a full reset.

That is worse than either half alone, because it looks fixed.

What this file asserts, and why it is DERIVED
---------------------------------------------
A hardcoded list of filenames rots the moment somebody adds a script. So the
three inputs here are all READ OUT OF THE SOURCE:

  1. INVOKED  - every script name the running app builds a path to against
                its own root and hands to a spawn/exec, scraped from
                ``macOS/*.js`` and ``src/**/*.py``.
  2. BUNDLED  - ``build.extraResources`` in ``macOS/package.json``: what
                physically ships inside the .app.
  3. RESYNCED - ``RESYNC_ALLOWLIST`` in ``macOS/bootstrap.js``: what is
                refreshed into the derived tree on every packaged launch.

A script added tomorrow is caught without anyone remembering to update this
test, because it enters INVOKED by being invoked.

THREE OUTCOMES, not two
-----------------------
A script that is invoked but not resynced is not automatically a pass or a
fail. There is a third state - "invoked, deliberately not delivered, and the
caller handles its absence LOUDLY" - and it is recorded in
``ACCEPTED_UNDELIVERED`` with a reason. That register is not a mute button:
an entry is only legal if the script is ALSO absent from ``extraResources``,
i.e. the app never pretends to ship it. The nuke.sh failure mode - shipped,
stale, executed anyway, silently wrong - can never be parked there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_JS = REPO_ROOT / "macOS" / "bootstrap.js"
PACKAGE_JSON = REPO_ROOT / "macOS" / "package.json"

# Identifiers that denote "the tree the app runs out of". A path joined
# against one of these and then executed is a DELIVERY claim: the app is
# asserting that file will be there, in a current version, at run time.
_ROOT_IDENT = r"[A-Za-z_.]*(?:root|Root|baseDir|serverDir|projectRoot|project_root)"

# path.join(<root-ish>, 'name.sh')  /  os.path.join(root, "name.py")
_JS_JOIN = re.compile(
    r"path\.join\(\s*" + _ROOT_IDENT + r"[^,)]*,\s*['\"]([^'\"/]+\.(?:sh|py))['\"]"
)
_PY_JOIN = re.compile(
    r"os\.path\.join\(\s*" + _ROOT_IDENT + r"[^,)]*,\s*['\"]([^'\"/]+\.(?:sh|py))['\"]"
)

# Scripts the app builds a root-relative path to for a reason OTHER than
# executing it (documentation strings, existence probes with no spawn).
# Kept empty deliberately: a root-relative path to a script is treated as a
# delivery claim regardless of what the app then does with it, because the
# file being current is the whole point either way.
_NON_EXEC_USES: set[str] = set()

# The third outcome. Each entry: name -> reason. An entry is only legal if
# the script is ALSO absent from extraResources (asserted below).
ACCEPTED_UNDELIVERED: dict[str, str] = {
    # EMPTY, and that is the finished state, not a gap.
    #
    # reset.sh was the only entry. It was invoked by
    # POST /api/v1/server/reset, never shipped in extraResources, and so
    # 500'd on every packaged install. That endpoint and its UI control were
    # REMOVED (see the note where the route used to be in src/api/routes.py),
    # which takes reset.sh out of the invoked set entirely - so the register
    # entry became permanently true and this file's own `unused` assertion
    # required deleting it rather than rewording it.
    #
    # An entry here is legal only when the script is invoked, is genuinely
    # undeliverable, is ALSO absent from extraResources, and its caller fails
    # loudly. It is not a place to park a control that does not work.
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _strip_js_comments(text: str) -> str:
    """Drop // and /* */ comments so PROSE about a script is not an invocation.

    Inputs: text (str) - JavaScript source.
    Outputs: str - the same source with comments blanked.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _strip_py_comments(text: str) -> str:
    """Drop # comments and triple-quoted docstrings from Python source.

    Inputs: text (str) - Python source.
    Outputs: str - the same source with comments and docstrings blanked.
    """
    text = re.sub(r'"""[\s\S]*?"""', '""', text)
    text = re.sub(r"'''[\s\S]*?'''", "''", text)
    return re.sub(r"(?m)#.*$", "", text)


def discover_invoked_scripts() -> dict[str, str]:
    """Every script the app resolves against its own root at run time.

    Derived from source, never hardcoded, so a script added tomorrow appears
    here the moment it is wired up.

    Inputs: none.
    Outputs: dict[str, str] - script filename -> "<relpath>:<line>" of the
        first site that resolves it.

    Example:
        >>> "nuke.sh" in discover_invoked_scripts()
        True
    """
    found: dict[str, str] = {}
    targets = sorted((REPO_ROOT / "macOS").glob("*.js")) + sorted(
        (REPO_ROOT / "src").rglob("*.py")
    )
    for path in targets:
        raw = _read(path)
        cleaned = _strip_js_comments(raw) if path.suffix == ".js" else _strip_py_comments(raw)
        pattern = _JS_JOIN if path.suffix == ".js" else _PY_JOIN
        for match in pattern.finditer(cleaned):
            name = match.group(1)
            if name in _NON_EXEC_USES or name in found:
                continue
            line = cleaned.count("\n", 0, match.start()) + 1
            found[name] = f"{path.relative_to(REPO_ROOT)}:{line}"
    return found


def parse_resync_allowlist() -> set[str]:
    """The names bootstrap.js refreshes into the derived tree every launch.

    Inputs: none.
    Outputs: set[str] - the ``name`` field of every RESYNC_ALLOWLIST entry.
    """
    text = _read(BOOTSTRAP_JS)
    block = re.search(r"const RESYNC_ALLOWLIST\s*=\s*\[(.*?)\];", text, re.DOTALL)
    assert block, "RESYNC_ALLOWLIST not found in macOS/bootstrap.js - has it been renamed?"
    names = set(re.findall(r"name:\s*['\"]([^'\"]+)['\"]", block.group(1)))
    assert names, "RESYNC_ALLOWLIST parsed as empty - the parser is broken, not the allowlist"
    return names


def parse_extra_resources() -> set[str]:
    """The names electron-builder physically ships inside the .app bundle.

    Inputs: none.
    Outputs: set[str] - the ``to`` field of every build.extraResources entry.
    """
    data = json.loads(_read(PACKAGE_JSON))
    entries = data.get("build", {}).get("extraResources", [])
    assert entries, "build.extraResources is empty or missing in macOS/package.json"
    return {e["to"] for e in entries if isinstance(e, dict) and "to" in e}


def test_discovery_is_actually_finding_things():
    """Positive control: a scraper that finds nothing looks exactly like a pass."""
    invoked = discover_invoked_scripts()
    assert invoked, (
        "discover_invoked_scripts() found NOTHING. That is a broken scraper, not "
        "a clean app - every assertion below would vacuously pass."
    )
    assert "nuke.sh" in invoked, (
        "nuke.sh is executed by macOS/main.js against getProjectRoot(); if the "
        "scraper cannot see it, the scraper is wrong."
    )


def test_every_runtime_invoked_script_is_resynced():
    """A script the app executes from the derived tree must be refreshed there.

    Otherwise an upgrade pairs new calling code with a first-run-old copy of
    the script - the nuke.sh release blocker.
    """
    invoked = discover_invoked_scripts()
    resynced = parse_resync_allowlist()

    stale = {
        name: site
        for name, site in invoked.items()
        if name not in resynced and name not in ACCEPTED_UNDELIVERED
    }
    assert not stale, (
        "These scripts are invoked at run time from the app's own root but are "
        "NOT in RESYNC_ALLOWLIST (macOS/bootstrap.js), so an upgraded install "
        "keeps executing its first-run copy forever:\n"
        + "\n".join(f"  {n}  <- {s}" for n, s in sorted(stale.items()))
        + "\n\nFix by adding the script to RESYNC_ALLOWLIST (and to "
        "build.extraResources so it ships at all), or - if it genuinely cannot "
        "be delivered - register it in ACCEPTED_UNDELIVERED with a reason."
    )


def test_resynced_scripts_are_actually_bundled():
    """Resyncing a file the bundle does not carry is a no-op that logs a warning."""
    invoked = discover_invoked_scripts()
    bundled = parse_extra_resources()
    resynced = parse_resync_allowlist()

    missing = sorted(n for n in invoked if n in resynced and n not in bundled)
    assert not missing, (
        "Named in RESYNC_ALLOWLIST but absent from build.extraResources, so "
        "there is nothing in the bundle to resync FROM: " + ", ".join(missing)
    )


def test_accepted_undelivered_register_cannot_hide_a_stale_shipped_script():
    """The register is for scripts that are ABSENT, never for scripts that are STALE.

    A file that ships in the bundle and is executed from the derived tree but
    is not resynced is exactly the nuke.sh failure - shipped, silently old,
    and run anyway. That case must never be parkable here.
    """
    bundled = parse_extra_resources()
    invoked = discover_invoked_scripts()

    illegal = sorted(n for n in ACCEPTED_UNDELIVERED if n in bundled)
    assert not illegal, (
        "These are in ACCEPTED_UNDELIVERED but DO ship in build.extraResources. "
        "A bundled script that is invoked must be resynced, not excused: "
        + ", ".join(illegal)
    )

    unused = sorted(n for n in ACCEPTED_UNDELIVERED if n not in invoked)
    assert not unused, (
        "ACCEPTED_UNDELIVERED names scripts that are no longer invoked anywhere. "
        "A permanently-true exemption is furniture, not a monitor - delete it: "
        + ", ".join(unused)
    )


def test_accepted_undelivered_entries_carry_a_real_reason():
    """An exemption without a stated reason is a suppression.

    Deliberately NOT parametrized over ``ACCEPTED_UNDELIVERED``. The register
    is empty and that is its finished state, and a parametrize over an empty
    collection collects one placeholder case that pytest SKIPS on every
    platform - a test that cannot go red, which the CI skip audit correctly
    refuses. Iterating inside the test body keeps it running against an empty
    register and still fails the moment a thin reason is added.

    Inputs: none - reads the module-level ``ACCEPTED_UNDELIVERED`` register.
    Outputs: none. Raises AssertionError naming every entry whose reason is
    too short to audit.
    """
    thin = sorted(
        name for name, reason in ACCEPTED_UNDELIVERED.items() if len(reason) <= 80
    )
    assert not thin, (
        "These ACCEPTED_UNDELIVERED entries carry a reason too thin to audit. "
        "An exemption without a stated reason is a suppression: "
        + ", ".join(thin)
    )


# ---------------------------------------------------------------------------
# Build hygiene: what the BUILD MACHINE leaks into the bundle.
#
# The src/ and client/ extraResources entries used a bare ``**/*`` filter, so
# whatever happened to be sitting in the working tree at build time shipped.
# Running pytest before building put 110 ``.pyc`` files, compiled by the build
# machine's own interpreter, inside the 1.0.2 candidate DMG. Not a correctness
# fault - CPython invalidates a .pyc whose recorded source mtime and size do
# not match - but it is several MB of one machine's state distributed to
# users, and the same unbounded filter is what would ship a stray .DS_Store or
# anything else a future tool drops in those trees.
#
# These directories are gitignored, so nothing in git can catch this. The
# filter is the only place it can be caught, which is why it is asserted here.
# ---------------------------------------------------------------------------

# Directories copied wholesale into the bundle, whose contents are therefore
# whatever the build machine's working tree holds at build time.
_WHOLESALE_COPIED = ("src", "client")

# Patterns each wholesale-copied entry must exclude. Build-machine byproducts
# that are never part of a release.
_REQUIRED_EXCLUSIONS = ("!**/__pycache__/**", "!**/*.pyc", "!**/.DS_Store")


def parse_extra_resource_filters() -> dict[str, list[str]]:
    """The ``filter`` array of every build.extraResources entry, keyed by ``to``.

    Inputs: none.
    Outputs: dict[str, list[str]] - destination name -> its filter patterns.
      An entry with no ``filter`` key maps to an empty list, which is itself
      the unbounded case this module's hygiene test rejects.
    """
    data = json.loads(_read(PACKAGE_JSON))
    entries = data.get("build", {}).get("extraResources", [])
    return {
        e["to"]: list(e.get("filter", []))
        for e in entries
        if isinstance(e, dict) and "to" in e
    }


def test_wholesale_copied_trees_exclude_build_machine_byproducts():
    """src/ and client/ must not ship the build machine's caches.

    A bare ``**/*`` filter is not "everything we wrote", it is "everything
    that is there right now", and those are different sets on any machine
    that has run the test suite.
    """
    filters = parse_extra_resource_filters()
    missing: list[str] = []
    for name in _WHOLESALE_COPIED:
        assert name in filters, (
            f"build.extraResources no longer ships {name!r}. If that is "
            "deliberate, delete it from _WHOLESALE_COPIED in the same edit; "
            "a hygiene check pointed at a tree nobody ships is furniture."
        )
        for pattern in _REQUIRED_EXCLUSIONS:
            if pattern not in filters[name]:
                missing.append(f"{name}: {pattern}")

    assert not missing, (
        "These extraResources entries copy a whole tree without excluding "
        "build-machine byproducts, so the DMG carries whatever the working "
        "tree held at build time: " + ", ".join(missing)
    )
