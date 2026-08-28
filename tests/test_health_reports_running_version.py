"""The running server must be able to say which version's CODE it is running.

WHY THIS EXISTS
---------------

On 2026-08-25 a v1.0.2 Cloude Code orphaned its Python server on quit. The
server was reparented to launchd and kept serving port 8000. A v1.0.3 bundle
started four hours later, found a healthy listener there, and ADOPTED it. The
user then ran v1.0.2 server code under a v1.0.3 app for four hours, with a
four-hour-stale in-memory config cache that manufactured a false divergence
report.

Adoption is the right behaviour when Electron crashed and left its own healthy
server behind. It is the wrong behaviour across an upgrade. The only thing that
tells those two cases apart is the VERSION the running server reports, so the
running server has to be askable, without credentials, by an app that has not
authenticated yet.

WHY NOT ``GET /api/v1/version``
-------------------------------

It is auth-gated: ``@router.get("/version", dependencies=[Depends(require_auth)])``.
The menu-bar app decides whether to adopt before any user has logged in, so a
401 would be the normal answer and "cannot determine" would be the normal
outcome, which makes the gate useless. ``GET /api/v1/health`` is the endpoint
the tray already polls and is deliberately unauthenticated for exactly this
reason ("to allow menu bar app to poll before user logs in"), so the version
goes there. test_version_route_still_requires_auth below pins that this was a
real constraint and not an assumption.

WHY THE VALUE IS FROZEN AT STARTUP
----------------------------------

``resolve_version()`` re-resolves on every call. Its first source is the
``CLOUDE_APP_VERSION`` env var, which is stable for the life of a process - but
when that is absent it falls back to the ``VERSION`` file on disk, and
``macOS/bootstrap.js`` REWRITES that file on every packaged launch. So the
upgraded bundle stamps VERSION=1.0.3 while the orphaned 1.0.2 process is still
running, and a request-time resolve would have that old process report 1.0.3.
The adoption check would then see a match, adopt, and reproduce the exact bug
it exists to prevent - a false green built out of a correct-looking read.

Freezing at import pins what the process resolved when IT started, which is the
question actually being asked.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_hver_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_hver_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import pytest

from src.core import version as version_mod
from src.core.version import (
    freeze_startup_version,
    reset_startup_version,
    startup_version,
    write_version_file,
)


@pytest.fixture(autouse=True)
def _clean_freeze():
    """Each test gets an unfrozen resolver and restores the env afterwards."""
    reset_startup_version()
    saved = os.environ.get("CLOUDE_APP_VERSION")
    yield
    if saved is None:
        os.environ.pop("CLOUDE_APP_VERSION", None)
    else:
        os.environ["CLOUDE_APP_VERSION"] = saved
    reset_startup_version()


# --- the freeze -----------------------------------------------------------


def test_startup_version_reports_what_the_process_started_with() -> None:
    os.environ["CLOUDE_APP_VERSION"] = "1.0.2"
    assert freeze_startup_version() == "1.0.2"
    assert startup_version() == "1.0.2"


def test_the_frozen_version_survives_the_env_changing_underneath_it() -> None:
    os.environ["CLOUDE_APP_VERSION"] = "1.0.2"
    freeze_startup_version()
    os.environ["CLOUDE_APP_VERSION"] = "1.0.3"
    assert startup_version() == "1.0.2", (
        "the running process re-resolved its version and reported the NEW "
        "bundle's number. An upgrade would then look like a match and the "
        "old server would be adopted - the exact bug this guards."
    )


def test_the_frozen_version_survives_the_VERSION_file_being_rewritten(
    tmp_path: Path,
) -> None:
    """bootstrap.js rewrites VERSION on every packaged launch."""
    os.environ.pop("CLOUDE_APP_VERSION", None)
    write_version_file("1.0.2", tmp_path)
    assert freeze_startup_version(tmp_path) == "1.0.2"

    # The upgraded bundle lands and stamps its own number over the top.
    write_version_file("1.0.3", tmp_path)
    assert startup_version() == "1.0.2", (
        "the still-running old server read the NEW bundle's VERSION file and "
        "reported 1.0.3, which is a false match"
    )


def test_an_unresolvable_version_is_empty_not_invented(tmp_path: Path) -> None:
    os.environ.pop("CLOUDE_APP_VERSION", None)
    # tmp_path has no VERSION file, no git, no macOS/package.json.
    assert freeze_startup_version(tmp_path) == "", (
        "a version was invented for an install where nothing resolved"
    )


def test_freezing_is_idempotent() -> None:
    os.environ["CLOUDE_APP_VERSION"] = "1.0.2"
    first = freeze_startup_version()
    os.environ["CLOUDE_APP_VERSION"] = "9.9.9"
    assert freeze_startup_version() == first


# --- the endpoint ---------------------------------------------------------


def _health_body(client) -> dict:
    response = client.get("/api/v1/health")
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_the_running_version_without_credentials() -> None:
    """The tray must be able to ask BEFORE anyone has logged in."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import router

    os.environ["CLOUDE_APP_VERSION"] = "1.0.2"
    reset_startup_version()
    freeze_startup_version()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.session_manager = None
    app.state.local_servers = None

    with TestClient(app) as client:
        body = _health_body(client)

    assert "version" in body, (
        "GET /api/v1/health does not report a version, so the menu-bar app has "
        "no unauthenticated way to find out whose code is on the port and can "
        "only guess. Guessing is what adopted a v1.0.2 server into a v1.0.3 app."
    )
    assert body["version"] == "1.0.2"


def test_health_version_is_the_frozen_one_not_a_fresh_resolve() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import router

    os.environ["CLOUDE_APP_VERSION"] = "1.0.2"
    reset_startup_version()
    freeze_startup_version()
    # The newer bundle lands while this process keeps running.
    os.environ["CLOUDE_APP_VERSION"] = "1.0.3"

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.session_manager = None
    app.state.local_servers = None

    with TestClient(app) as client:
        body = _health_body(client)

    assert body["version"] == "1.0.2", (
        "the health endpoint re-resolved and reported the new bundle's version, "
        "so an upgrade reads as a match and adopts the old server"
    )


def test_version_route_still_requires_auth() -> None:
    """Pins the reason /api/v1/version could not be used for this.

    If this ever stops being true the adoption check could move there, but
    until then a 401 is the normal answer for an app that has not logged in.
    """
    import inspect

    from src.api import version_routes

    source = inspect.getsource(version_routes)
    assert 'dependencies=[Depends(require_auth)]' in source
    assert '@router.get("/version"' in source


def test_the_health_model_declares_version() -> None:
    """A FastAPI response_model DELETES any field it does not declare.

    This project has already been bitten twice by that: a field present on the
    server, correct on disk, and silently stripped at serialization because the
    model did not enumerate it. A missing field in a response is not evidence
    of a missing value.
    """
    from src.models import HealthResponse

    assert "version" in HealthResponse.model_fields, (
        "HealthResponse does not declare `version`, so FastAPI will strip it "
        "from every response no matter what the endpoint returns"
    )
