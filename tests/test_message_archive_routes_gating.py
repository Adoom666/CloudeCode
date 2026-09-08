"""OFF MEANS ABSENT, part 2: the HTTP surface, measured by real request.

Split out of ``test_message_archive_gating.py`` for this repo's 500-line
cap; that file owns the schema and the scheduler, this one owns the routes
and the ``/api/v1/features`` report.

THE ROUTES ARE ASSERTED BY REAL REQUESTS AGAINST A REAL APP, never by
inspecting a router object. A route mounted somewhere unexpected - a
different prefix, a second include, a sub-application - looks identical to
an absent one in a router inspection and answers perfectly well over HTTP.
The discriminator is 404 versus 401: an unmounted path 404s, a mounted
path refuses an unauthenticated caller with 401. Both are measured, in
both flag states, so neither status can be produced by the wrong cause.

THE PAGE ROUTES ARE THE ONE ASYMMETRY. ``/archive`` and
``/archive/{rest:path}`` stay REGISTERED with the flag off and redirect to
the launchpad, because a bookmarked deep link has to land somewhere a
human can use and a bare 404 from a page route in a single-page app loses
the app. Serving the SPA shell would be worse than either: the archive
screen would boot and then 404 against every endpoint it needs, which
reads as a broken feature rather than an absent one.

THE APP IS REBUILT BY RELOADING ``src.main``, because the switch is read
once at import - which is the point of it. ``importlib.reload`` produces a
NEW FastAPI object; modules that already did ``from src.main import app``
keep the object they bound, so no other test file is affected. The module
is reloaded back to the suite-wide state after each test.
"""


from __future__ import annotations

import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_magr_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_magr_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from src.core import message_archive_flag as flag

#: One archive route, one corpus route and one overlay route. All three
#: are /api/v1/archive/* or /api/v1/corpus/* and all three must vanish
#: together - the overlay reads the archive, so leaving it mounted would
#: leave a live archive endpoint on an install that opted out.
GATED_API_PATHS = [
    "/api/v1/archive/hosts",
    "/api/v1/archive/projects",
    "/api/v1/corpus/status",
]

#: Page routes. These stay REGISTERED with the flag off and redirect,
#: rather than 404ing - a bookmarked deep link has to land somewhere a
#: human can use.
PAGE_PATHS = ["/archive", "/archive/t/5767/l/7111"]


def _mint_token() -> str:
    """Mint an access token the running app will accept.

    Description: the secret is read from the LIVE settings object rather
      than from a constant, so this cannot pass against a build whose
      secret came from somewhere else.
    Inputs: none.
    Output: str - an encoded JWT.
    Example: _mint_token()[:2] -> 'ey'
    """
    from src.config import settings

    payload = {
        "exp": datetime.utcnow() + timedelta(minutes=15),
        "iat": datetime.utcnow(),
        "sub": "claudetunnel_user",
        "typ": "access",
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")


@pytest.fixture()
def app_with_flag(monkeypatch: pytest.MonkeyPatch):
    """Build a real app with the master switch in a chosen state.

    Description: sets the env override, reloads ``src.main`` so the
      import-time gate runs again, and yields a factory. The module is
      reloaded back to the suite-wide state on teardown so no later test
      inherits an app built with the flag off.
    Inputs: monkeypatch (pytest.MonkeyPatch).
    Output: callable(bool) -> TestClient.
    Example: client = app_with_flag(False)
    """
    import src.main as main_module

    def build(enabled: bool) -> TestClient:
        monkeypatch.setenv(flag.ENABLE_ENV, "1" if enabled else "0")
        importlib.reload(main_module)
        assert main_module.MESSAGE_ARCHIVE.enabled is enabled, (
            "the reloaded module did not pick up the env override, so "
            "every assertion below would be measuring the wrong build"
        )
        return TestClient(main_module.app)

    yield build

    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    importlib.reload(main_module)


# ---------------------------------------------------------------------------
# 3. THE ROUTES, BY REAL REQUEST
# ---------------------------------------------------------------------------


def test_the_archive_api_is_absent_with_the_flag_off(app_with_flag) -> None:
    """404, measured over HTTP, on every gated path."""
    client = app_with_flag(False)
    for path in GATED_API_PATHS:
        response = client.get(path)
        assert response.status_code == 404, (
            f"{path} answered {response.status_code} with the archive "
            "switched off; the route surface leaks"
        )


def test_the_archive_api_is_present_with_the_flag_on(app_with_flag) -> None:
    """401, not 404 - the positive control that proves 404 meant absent.

    Without this, the test above would pass identically against a build
    whose paths were simply misspelled.
    """
    client = app_with_flag(True)
    for path in GATED_API_PATHS:
        response = client.get(path)
        assert response.status_code != 404, (
            f"{path} is missing with the archive switched ON"
        )
        assert response.status_code in (401, 403), (
            f"{path} answered {response.status_code} to an unauthenticated "
            "caller; it should refuse, not serve"
        )


def test_the_page_routes_redirect_to_the_launchpad_with_the_flag_off(
    app_with_flag,
) -> None:
    """A bookmarked deep link lands on a screen that exists."""
    client = app_with_flag(False)
    for path in PAGE_PATHS:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, (
            f"{path} answered {response.status_code}; with the archive off "
            "it must redirect rather than serve a shell that cannot work"
        )
        assert response.headers["location"] == "/"


def test_the_page_routes_serve_the_shell_with_the_flag_on(app_with_flag) -> None:
    """Positive control for the redirect above."""
    client = app_with_flag(True)
    for path in PAGE_PATHS:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_everything_else_still_works_with_the_flag_off(app_with_flag) -> None:
    """The app is a whole app with the feature off, not a crippled one."""
    client = app_with_flag(False)
    assert client.get("/health").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    # A representative ungated API route still refuses rather than 404s,
    # which proves the app's router is intact and not merely empty.
    assert client.get("/api/v1/sessions").status_code in (401, 403)


# ---------------------------------------------------------------------------
# 4. THE THIRD OUTCOME, PUBLISHED
# ---------------------------------------------------------------------------


def test_features_reports_disabled_and_says_why(app_with_flag) -> None:
    """"Off" is published as its own state, with a reason, and always exists."""
    client = app_with_flag(False)
    assert client.get("/api/v1/features").status_code in (401, 403), (
        "/api/v1/features is not auth-protected like every other API route"
    )
    response = client.get(
        "/api/v1/features",
        headers={"Authorization": f"Bearer {_mint_token()}"},
    )
    assert response.status_code == 200, (
        "/api/v1/features must exist in BOTH states; it is the only way a "
        "client can tell 'off' from 'on but broken'"
    )
    block = response.json()["message_archive"]
    assert block["state"] == flag.STATE_DISABLED
    assert block["routes_mounted"] is False
    assert block["restart_required"] is False
    assert block["reason"], "a refusal with no reason is a silent refusal"


def test_features_reports_enabled_when_it_is_on(app_with_flag) -> None:
    """Positive control, and proof routes_mounted is measured not derived."""
    client = app_with_flag(True)
    response = client.get(
        "/api/v1/features",
        headers={"Authorization": f"Bearer {_mint_token()}"},
    )
    assert response.status_code == 200
    block = response.json()["message_archive"]
    assert block["state"] == flag.STATE_ENABLED
    assert block["routes_mounted"] is True, (
        "the flag says enabled but no /api/v1/archive/* route is in the "
        "live route table; routes_mounted is derived, not measured"
    )
    assert block["restart_required"] is False
