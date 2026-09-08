"""The four archive routes serve the SPA shell, and shadow nothing.

WHAT THIS FILE IS DEFENDING AGAINST. ``src/main.py`` has no catch-all by
design, so before this change a reload while reading transcript 5767
returned a bare 404 and lost the app. The fix is two explicit routes,
``/archive`` and ``/archive/{rest:path}``. The second one matches across
slashes, which is what ``/archive/t/5767/l/7111`` needs - and is also
exactly the shape that, written one prefix wider as
``@app.get("/{rest:path}")``, would swallow ``/api/v1/*`` and
``/static/*`` and turn every 404 in the application into a 200 serving
HTML. That regression would be invisible to a browser (the app still
works) and catastrophic to every API client.

So this suite asserts THREE things, not one:

1. all four archive routes return the SPA shell;
2. ``/api/v1/archive/hosts`` still reaches the API, not the shell;
3. a path that merely STARTS with the letters of the prefix
   (``/archivexyz``) is still a 404 - proof the route is anchored at a
   path segment boundary rather than at a string prefix.

The requests are made without running the app's lifespan (no ``with``
block), so no background scheduler starts. These routes touch no
application state, which is what makes that safe.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_archroutes_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archroutes_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import pytest
from fastapi.testclient import TestClient

from src.main import app

# One representative of each of the four route shapes in section H.1.
ARCHIVE_ROUTES = [
    "/archive",
    "/archive/p/12",
    "/archive/t/5767",
    "/archive/t/5767/l/7111",
]

# A marker that is present in the SPA shell and in nothing else the
# server can return. The screen div is the right anchor: it is what this
# whole change added, so a shell served WITHOUT it is a half-deploy and
# should fail here rather than pass on a generic "<html>" check.
SHELL_MARKER = 'id="archive-screen"'


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A TestClient over the real app, with no lifespan run.

    Inputs: None.
    Outputs: TestClient.
    """
    return TestClient(app)


def test_root_still_serves_the_shell(client: TestClient) -> None:
    """POSITIVE CONTROL.

    If ``/`` does not serve a shell containing the marker, every
    assertion below would be testing the marker rather than the routes,
    and a failure there would send the next person at FastAPI routing
    instead of at index.html.
    """
    r = client.get("/")
    assert r.status_code == 200, f"GET / returned {r.status_code}"
    assert SHELL_MARKER in r.text, (
        f"the SPA shell served at / does not contain {SHELL_MARKER!r}. "
        "This suite cannot distinguish a served shell from anything else "
        "until that is fixed - CANNOT DETERMINE, not a pass."
    )


@pytest.mark.parametrize("path", ARCHIVE_ROUTES)
def test_archive_route_serves_the_spa_shell(client: TestClient, path: str) -> None:
    """A hard reload on any archive URL keeps the app.

    ``follow_redirects=False`` is deliberate and is what makes this test
    able to fail. MEASURED: with the explicit ``@app.get("/archive")``
    route removed, ``/archive`` does not 404 - Starlette's
    ``redirect_slashes`` answers 307 to ``/archive/`` and a
    redirect-following client still ends up at a 200 shell. So a test
    that follows redirects passes with the route deleted, which is a
    verification step that cannot fail. Asserting the direct 200 is what
    proves the route exists.
    """
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, (
        f"GET {path} returned {r.status_code}, so a reload on that URL "
        "loses the app instead of restoring the view."
    )
    assert SHELL_MARKER in r.text, f"GET {path} did not return the SPA shell"


def test_trailing_slash_root_also_works(client: TestClient) -> None:
    """``/archive/`` and ``/archive`` must both resolve.

    ``{rest:path}`` does not match the empty string, which is why the
    root has its own route; the trailing-slash form goes through the
    path route with an empty remainder.
    """
    r = client.get("/archive/")
    assert r.status_code == 200, f"GET /archive/ returned {r.status_code}"
    assert SHELL_MARKER in r.text


def test_the_archive_api_is_not_shadowed(client: TestClient) -> None:
    """``/api/v1/archive/hosts`` must reach the API, never the shell.

    Unauthenticated, so the expected answer is a 401/403 from the auth
    dependency. What matters is that it is JSON from the API layer and
    NOT 200 HTML - a 200 shell here would mean the SPA route is
    swallowing API traffic and every archive fetch would parse HTML as
    an envelope.
    """
    r = client.get("/api/v1/archive/hosts")
    assert SHELL_MARKER not in r.text, (
        "GET /api/v1/archive/hosts returned the SPA shell. An archive route "
        "is shadowing the API; the client would parse HTML as an envelope."
    )
    assert r.status_code in (401, 403), (
        f"expected an auth refusal from the API, got {r.status_code}"
    )


def test_static_is_not_shadowed(client: TestClient) -> None:
    """``/static/js/app.js`` must still be the real file."""
    r = client.get("/static/js/app.js")
    assert r.status_code == 200
    assert SHELL_MARKER not in r.text, "an archive route is shadowing /static"


@pytest.mark.parametrize("path", ["/archivexyz", "/archiv", "/notarchive/t/1"])
def test_paths_that_are_not_archive_routes_are_still_404(
    client: TestClient, path: str
) -> None:
    """The prefix is a path SEGMENT, not a string prefix.

    ``/archivexyz`` sharing the first eight characters with ``/archive``
    must not be enough to serve it the shell. If it were, the route is
    matching a substring and the blast radius is every URL in the app
    that happens to start the same way.
    """
    r = client.get(path)
    assert r.status_code == 404, (
        f"GET {path} returned {r.status_code}; expected 404. The archive "
        "route is matching more than it should."
    )
