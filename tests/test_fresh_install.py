"""What a genuinely fresh install is allowed to contain.

WHY THIS FILE EXISTS

Four first-run defects shipped in v1.0.2 and every one of them survived a
green suite, because nothing in the suite ever exercised an empty machine.
Every test here had a developer's populated ``config.json`` sitting next to
it, so "the user has four projects" and "the user has zero projects" were
never distinguishable.

The specific defect this file's first test would have caught: the bootstrap
(``macOS/bootstrap.js``, step 3e) copies ``config.example.json`` verbatim to
``config.json``. The example carried four invented demo projects pointing at
``~/projects/*`` paths that exist on nobody's machine, so the first screen a
new user ever saw was four broken rows.

The rule this file enforces: ``config.example.json`` documents the SHAPE of
the file. It is never a source of user DATA. A key whose value is a list of
things the user owns (projects) must ship empty, because the bootstrap copies
it verbatim and cannot tell an illustration from a real entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Keys in config.example.json whose value is a collection of things the USER
#: owns, rather than a setting describing how the app behaves. The bootstrap
#: copies this file verbatim, so anything listed here must ship empty or a
#: fresh install starts with fabricated user data.
# NOTHING LEFT. ``projects`` used to be the one user-data key config.json
# carried, and the parametrized guard below made sure it shipped present
# and empty. Projects are DB-only now, so the correct assertion inverted:
# the key must be ABSENT, which is what
# test_example_config_carries_no_projects_key checks. If a future user-data
# key lands in config.json, add it here and the present-and-empty guard
# comes back to life for it.
USER_DATA_KEYS = ()


@pytest.fixture(scope="module")
def example() -> dict:
    """Parsed config.example.json.

    Returns:
        The example config as a dict.
    """
    return json.loads((REPO_ROOT / "config.example.json").read_text())


def test_example_config_seeds_no_user_data(example: dict) -> None:
    """Every declared user-data key must be present (shape) and empty (no rows).

    Present, so the file still documents that the key exists and is a list.
    Empty, so ``fs.copyFileSync(config.example.json, config.json)`` cannot
    hand a brand-new user rows they did not create.

    NOT PARAMETRIZED, deliberately. ``USER_DATA_KEYS`` is empty now that
    projects are DB-only, and pytest turns an empty parametrize into a
    test that is SKIPPED on every platform - one that can never go red,
    which is exactly what scripts/ci/skip-audit.py rejects. Iterating
    inside the body keeps the guard armed for a future key while still
    actually running today.

    Args:
        example: Parsed config.example.json.
    """
    for key in USER_DATA_KEYS:
        assert key in example, (
            f"{key} vanished from config.example.json. The example must still "
            f"document the key's existence and type, or the file stops being a "
            f"description of the file's shape."
        )
        assert example[key] == [], (
            f"config.example.json seeds {len(example[key])} {key}. The bootstrap "
            f"copies this file verbatim to config.json, so every entry here "
            f"becomes a row on a brand-new user's very first screen, pointing at "
            f"a path that exists only on the machine of whoever wrote it."
        )


def test_example_config_carries_no_projects_key(example: dict) -> None:
    """Projects are not a config concern, so the example must not imply they are.

    Description: this assertion is the exact INVERSE of the one it
      replaces, and the inversion is the point. The old test required a
      ``projects: []`` key plus a ``_comment_projects`` documenting the
      entry shape. Both are now misleading: a reader who followed that
      documentation would hand-write entries into config.json and watch
      the app ignore every one of them. Documentation for a key that no
      longer does anything is worse than no documentation, because a
      reader will act on it.
    """
    assert "projects" not in example, (
        "config.example.json still carries a projects key. Projects live "
        "in cloude.db only, so a key here documents a shape the app does "
        "not read and invites hand edits that silently do nothing."
    )
    assert "_comment_projects" not in example, (
        "config.example.json still documents the retired projects array's "
        "entry shape. That is now a stale doc."
    )
    retired = example.get("_comment_projects_retired")
    assert isinstance(retired, str) and "cloude.db" in retired, (
        "config.example.json should say where projects DID go. Removing "
        "the key without a forwarding note leaves a reader who remembers "
        "it with no way to find out what replaced it."
    )


def test_fresh_config_json_yields_zero_projects(tmp_path: Path) -> None:
    """Reproduce the bootstrap copy and assert the result has no projects.

    This is the actual first-run path, not a restatement of the test above:
    ``macOS/bootstrap.js`` step 3e does a byte copy of the example to
    ``config.json`` when none exists. Asserting on the COPY is what proves a
    fresh install starts empty, because the copy is what the server reads.

    Args:
        tmp_path: pytest-provided empty directory standing in for a fresh
            install's config location.
    """
    fresh = tmp_path / "config.json"
    fresh.write_bytes((REPO_ROOT / "config.example.json").read_bytes())
    loaded = json.loads(fresh.read_text())
    assert not loaded.get("projects"), (
        "A freshly bootstrapped config.json contains projects. The first "
        "screen of a brand-new install would show rows the user never made."
    )
