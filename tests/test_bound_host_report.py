"""What the server reports as its EFFECTIVE bind, and why it cannot be derived.

WHY THIS FILE EXISTS

A fresh install hit this on its very first run. The Electron bootstrap was
still writing .env and config.json when the server started, so the server
evaluated setup as INCOMPLETE, the lockdown pinned uvicorn to 127.0.0.1, and
uvicorn bound its socket - once, permanently, because uvicorn has no in-place
rebind.

Moments later the bootstrap finished. Setup now evaluated COMPLETE. And
``current_exposure()`` RE-DERIVES the exposure from the filesystem on every
call, so GET /health began reporting ``effective_host: "0.0.0.0"`` while the
only listening socket on the machine was on loopback. The menu bar faithfully
displayed 0.0.0.0, the owner read it as "I am reachable on the LAN", and he
was not.

Nothing errored. Every signal was green. The endpoint's own docstring claimed
it reported "what it really bound", and it was reporting a fresh guess.

THE FIX IS THE DISTINCTION THIS FILE PINS. An effective bind is a RECORD of
what was passed to uvicorn at startup, or it is UNKNOWN. It is never
re-derived, because a re-derivation is an aspiration wearing a measurement's
name - the exact false-green class this project keeps finding. And unknown is
reported as unknown: null, never a plausible address, never silently the
configured one.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_bh_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_bh_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

from src.core.setup_state import (  # noqa: E402
    BOUND_HOST_ENV,
    LOOPBACK_HOST,
    bind_report,
    record_bound_host,
    recorded_bound_host,
)


@pytest.fixture(autouse=True)
def clean_record():
    """Remove the startup record around every test.

    The record lives in the process environment because uvicorn imports the
    app into the SAME process that chose the bind, but ``python -m src.main``
    means the chooser is ``__main__`` and the app is ``src.main`` - two
    distinct module objects, so a module-level global would not survive the
    trip. The environment does.
    """
    previous = os.environ.pop(BOUND_HOST_ENV, None)
    yield
    os.environ.pop(BOUND_HOST_ENV, None)
    if previous is not None:
        os.environ[BOUND_HOST_ENV] = previous


def test_an_unrecorded_bind_is_unknown_not_the_configured_value() -> None:
    """No record means UNKNOWN. It must never resolve to configuration.

    This is the whole rule in one assertion. A server this process did not
    start, or one started before this record existed, has an effective bind
    nobody measured, and the honest answer is that we do not know.
    """
    assert recorded_bound_host() is None

    report = bind_report(configured_host="0.0.0.0")
    assert report["effective_host"] is None, (
        "an unmeasured bind resolved to an address; showing a plausible "
        "wrong address is the worst of the three collapses"
    )
    assert report["effective_host_known"] is False
    assert report["configured_host"] == "0.0.0.0"


def test_unknown_stays_unknown_in_every_derived_field() -> None:
    """The third state must not leak back into pass or fail downstream.

    ``restart_required`` and ``locked_down`` are both computed FROM the
    effective host. When it is unknown they are unknown too - a JSON false
    here would tell the menu "nothing to do", which is a verdict nobody
    measured.
    """
    report = bind_report(configured_host="0.0.0.0")
    assert report["restart_required"] is None
    assert report["locked_down"] is None


def test_the_recorded_bind_wins_over_a_completed_setup() -> None:
    """The exact shape of the owner's first-run bug.

    Setup is complete NOW, configuration says 0.0.0.0, and a re-derivation
    would happily report 0.0.0.0. The socket is on loopback and always will
    be until a restart. The record is what the report must reflect.
    """
    record_bound_host(LOOPBACK_HOST)

    report = bind_report(configured_host="0.0.0.0")
    assert report["effective_host"] == LOOPBACK_HOST
    assert report["effective_host_known"] is True
    assert report["restart_required"] is True, (
        "the configured address differs from the bound one and the report "
        "does not say a restart is needed to apply it"
    )
    assert report["locked_down"] is True


def test_a_matching_record_needs_no_restart() -> None:
    """The healthy case, so the test above cannot pass by always saying yes."""
    record_bound_host("0.0.0.0")

    report = bind_report(configured_host="0.0.0.0")
    assert report["effective_host"] == "0.0.0.0"
    assert report["restart_required"] is False
    assert report["locked_down"] is False


def test_an_empty_record_is_unknown_not_an_empty_address() -> None:
    """An empty string is absence, not an address.

    Recorded via the environment, which cannot distinguish "unset" from "set
    to nothing" unless somebody says which one it means.
    """
    os.environ[BOUND_HOST_ENV] = ""
    assert recorded_bound_host() is None
    assert bind_report(configured_host="0.0.0.0")["effective_host"] is None


def test_the_reason_names_what_could_not_be_measured() -> None:
    """A blank cell is not actionable; the report says which state it is in."""
    unknown = bind_report(configured_host="0.0.0.0")["reason"]
    assert "not" in unknown.lower() or "unknown" in unknown.lower(), unknown

    record_bound_host(LOOPBACK_HOST)
    pending = bind_report(configured_host="0.0.0.0")["reason"]
    assert "restart" in pending.lower(), pending

