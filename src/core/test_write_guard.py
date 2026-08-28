"""Refuse writes outside a temp directory while a test run is in progress.

WHY THIS LIVES IN ``src/`` AND NOT IN ``tests/``
------------------------------------------------
The defect this exists to kill was a production code path
(``src/main.py``'s lifespan calling ``ensure_hook_settings()`` with no
path) that fell back to the developer's real ``~/.claude/settings.json``
and merged into it during a plain ``pytest`` run. It succeeded, returned
``True`` and logged an ``info`` line, so every signal was green while the
machine's live Claude Code configuration was being edited.

A ``conftest.py`` fixture can redirect the path, and this repo does that
too, but a fixture is not a control: a test that constructs its own app,
imports the writer directly, or shells out to a subprocess never sees it.
The only place that can refuse EVERY route is the writer itself, so the
check has to ship next to the writer.

It is inert in production. ``running_under_test()`` is false unless a
pytest marker is present in the environment, so a real server boot takes
the same path it always did.

THE THREE-OUTCOME RULE
----------------------
``assert_test_write_allowed`` resolves to exactly one of three verdicts:

* allowed      - not under test, or the path is provably under a temp root.
* refused      - under test and the path is provably outside every temp root.
* undetermined - under test and the temp root could not be resolved at all.

``undetermined`` is a REFUSAL. "I could not work out where this write
would land" is the precise state the original defect hid in, so it is
treated as at least as dangerous as a known-bad path. Collapsing it into
"allowed" would rebuild the bug inside the guard meant to prevent it.

DETECTING A TEST RUN
--------------------
Two markers, deliberately, because each covers a hole in the other:

* ``PYTEST_CURRENT_TEST`` is set by pytest for the duration of each test.
  It is authoritative but it is CLEARED between tests and during
  collection, so code running at import time or in a fixture teardown is
  unprotected by it alone.
* ``CLOUDE_TEST_MODE`` is set once by ``tests/conftest.py`` at import
  time, before any test module is collected, and lives in ``os.environ``
  so a child process inherits it. That is what keeps the guard alive
  across a ``subprocess.run`` fork, where ``PYTEST_CURRENT_TEST`` may
  have been stripped.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = [
    "OutsideTempWriteError",
    "TEST_MODE_ENV_VAR",
    "running_under_test",
    "temp_roots",
    "assert_test_write_allowed",
]

#: Sticky marker set by ``tests/conftest.py`` at import time. Survives
#: between tests and is inherited by subprocesses, unlike
#: ``PYTEST_CURRENT_TEST``.
TEST_MODE_ENV_VAR: str = "CLOUDE_TEST_MODE"


class OutsideTempWriteError(RuntimeError):
    """A test run tried to write to a path outside every temp root.

    Deliberately derived from ``RuntimeError`` rather than from any
    application exception hierarchy: this is a harness violation, not an
    application error, and no ``except`` clause in ``src/`` should ever
    swallow it.
    """


def running_under_test() -> bool:
    """Report whether this process is executing as part of a test run.

    Inputs:
        None. Reads ``PYTEST_CURRENT_TEST`` and ``CLOUDE_TEST_MODE`` from
        the process environment.
    Outputs:
        bool - True when either marker is present and non-empty.
    Example:
        >>> running_under_test()  # inside pytest
        True
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(os.environ.get(TEST_MODE_ENV_VAR))


def temp_roots() -> list[Path]:
    """Return every directory a test run is permitted to write beneath.

    Inputs:
        None. Consults ``tempfile.gettempdir()`` and ``TMPDIR``.
    Outputs:
        list[Path] - fully resolved temp roots. EMPTY when none could be
        determined, which callers must treat as "undetermined", never as
        "no restrictions".
    """
    roots: list[Path] = []
    candidates: list[str] = []

    try:
        candidates.append(tempfile.gettempdir())
    except OSError:
        # No usable temp dir. Leave it out; an empty result is the
        # undetermined signal the caller checks for.
        pass

    env_tmp = os.environ.get("TMPDIR")
    if env_tmp:
        candidates.append(env_tmp)

    for raw in candidates:
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _is_under(path: Path, root: Path) -> bool:
    """Report whether ``path`` sits at or beneath ``root``.

    Inputs:
        path: Candidate path, already resolved.
        root: Temp root, already resolved.
    Outputs:
        bool - True when path == root or path is inside it.
    """
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def assert_test_write_allowed(path: Path) -> None:
    """Raise unless ``path`` is a legitimate destination for a test write.

    A no-op outside a test run, so production behaviour is unchanged.

    Inputs:
        path: The file the caller is about to write.
    Outputs:
        None. Raises :class:`OutsideTempWriteError` on refusal.
    Raises:
        OutsideTempWriteError: the run is under test AND the path is
            outside every temp root, or the temp roots could not be
            resolved at all.
    Example:
        >>> assert_test_write_allowed(tmp_path / "settings.json")  # allowed
    """
    if not running_under_test():
        return

    roots = temp_roots()
    if not roots:
        raise OutsideTempWriteError(
            "Refusing a write during a test run: could not determine any "
            f"temp root, so the destination {path} cannot be shown safe. "
            "An undetermined destination is refused, never allowed."
        )

    # ``resolve()`` on a path whose parents do not exist still normalises
    # it on every supported Python, so this works for a file about to be
    # created as well as one already there.
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise OutsideTempWriteError(
            f"Refusing a write during a test run: {path} could not be "
            f"resolved ({exc}), so it cannot be shown safe."
        ) from exc

    for root in roots:
        if _is_under(resolved, root):
            return

    raise OutsideTempWriteError(
        f"Refusing a write during a test run to {resolved}, which is "
        "outside every temp root "
        f"({', '.join(str(r) for r in roots)}). A test must never modify "
        "real machine state. Pass an explicit path under pytest's "
        "tmp_path, or set the relevant redirect env var in "
        "tests/conftest.py."
    )
