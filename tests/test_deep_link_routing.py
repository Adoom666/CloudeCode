"""Tests for Item 9 — deep-link routing + CSP middleware.

Covers:
- GET ``/session/<name>`` serves the SPA shell (HTML 200).
- CSP + hardening headers present on every response we expose.
- `build_deep_link` composes ``{public_base_url}/session/<encoded-slug>``
  and defensively re-applies slugify so `.` in a raw slug becomes `_`.
- `build_deep_link` returns None on empty / missing public_base_url.
- `_slugify` behavior used by the deep-link pipeline: `/` → `_`,
  `..evil` → `evil`, etc.

Run with:
    python3 -m pytest tests/test_deep_link_routing.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- env bootstrap so pydantic Settings doesn't sys.exit(1) -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dl_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dl_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---- build_deep_link -----------------------------------------------------

def test_build_deep_link_basic():
    from src.core.notifications.events import (
        EventType,
        NotificationEvent,
        build_deep_link,
    )

    ev = NotificationEvent(
        kind=EventType.TASK_COMPLETE,
        session_slug="myproject",
        timestamp=0.0,
    )
    url = build_deep_link(ev, "http://mac.lan:8000")
    assert url == "http://mac.lan:8000/session/myproject"


def test_build_deep_link_strips_trailing_slash():
    from src.core.notifications.events import (
        EventType,
        NotificationEvent,
        build_deep_link,
    )

    ev = NotificationEvent(
        kind=EventType.PERMISSION_PROMPT,
        session_slug="foo",
        timestamp=0.0,
    )
    url = build_deep_link(ev, "http://mac.lan:8000///")
    assert url == "http://mac.lan:8000/session/foo"


def test_build_deep_link_slugifies_dots():
    """A raw slug containing `.` must be reduced to `_` before URL-encoding.

    This is the defense-in-depth re-slugify step: the upstream call site
    already runs `_slugify`, but `build_deep_link` re-applies it so a
    test / future caller constructing `NotificationEvent` directly can
    never ship a dotted path into a push notification.
    """
    from src.core.notifications.events import (
        EventType,
        NotificationEvent,
        build_deep_link,
    )

    ev = NotificationEvent(
        kind=EventType.TASK_COMPLETE,
        session_slug="my.project.v1",
        timestamp=0.0,
    )
    url = build_deep_link(ev, "http://mac.lan:8000")
    # `.` must NOT appear raw in the path segment.
    assert url == "http://mac.lan:8000/session/my_project_v1"
    assert "my.project.v1" not in url


def test_build_deep_link_returns_none_without_base_url():
    from src.core.notifications.events import (
        EventType,
        NotificationEvent,
        build_deep_link,
    )

    ev = NotificationEvent(
        kind=EventType.ERROR,
        session_slug="anything",
        timestamp=0.0,
    )
    assert build_deep_link(ev, "") is None
    assert build_deep_link(ev, None) is None


def test_build_deep_link_url_encodes_defensively():
    """Spaces and other non-alphanumerics must be percent-encoded.

    `_slugify` converts space → `_` already, so a hostile caller that
    somehow smuggles a space would hit `_` in the output. But quote()
    with safe="" runs regardless: encode FIRST, ask questions later.
    """
    from src.core.notifications.events import (
        EventType,
        NotificationEvent,
        build_deep_link,
    )

    ev = NotificationEvent(
        kind=EventType.TASK_COMPLETE,
        session_slug="has space",
        timestamp=0.0,
    )
    url = build_deep_link(ev, "http://mac.lan:8000")
    # Space → underscore via slugify. No literal space makes it through.
    assert " " not in url
    assert url == "http://mac.lan:8000/session/has_space"


# ---- slugify behavior (regression sanity) --------------------------------

def test_slugify_slash_becomes_underscore():
    from src.core.tmux_backend import _slugify

    assert _slugify("foo/bar") == "foo_bar"


def test_slugify_leading_dots_stripped_or_converted():
    """`..evil` exercises both the dot-replacement and leading-underscore
    strip-trailing_underscores path.

    Expected result: the two dots become `_`, then the leading strip
    removes them, leaving `evil`. Documenting this explicitly so a
    future refactor of `_slugify` can be tested against the plan spec.
    """
    from src.core.tmux_backend import _slugify

    assert _slugify("..evil") == "evil"


def test_slugify_handles_path_traversal_attempt():
    from src.core.tmux_backend import _slugify

    # `..` appearing in the middle should also be neutralized.
    out = _slugify("safe..name")
    assert ".." not in out
    assert out == "safe__name"


def test_slugify_empty_input_returns_default():
    from src.core.tmux_backend import _slugify

    assert _slugify("") == "default"
    assert _slugify("...") == "default"


# ---- FastAPI route + middleware ------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Build a TestClient that drives the real FastAPI app.

    We import `src.main` lazily so the env-var bootstrap at module top
    has already fired. The lifespan runs tunnel / notification setup
    which require intact settings.
    """
    from fastapi.testclient import TestClient
    from src.main import app

    # TestClient as a context manager runs the lifespan so app.state is
    # populated. For CSP tests we don't need any of that, but the root /
    # health routes read session_manager off app.state — keep lifespan on.
    with TestClient(app) as tc:
        yield tc


def _assert_csp_present(resp):
    """Every response we ship must carry the Item 9 headers."""
    csp = resp.headers.get("content-security-policy")
    assert csp is not None, "Content-Security-Policy header missing"
    # Minimum directives the plan requires.
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self' ws: wss:" in csp
    assert "frame-ancestors 'none'" in csp
    # xterm.js + addons load from jsdelivr; CSP must allowlist that host
    # in script-src, style-src, and font-src.
    assert "script-src 'self' https://cdn.jsdelivr.net" in csp
    assert "https://cdn.jsdelivr.net" in csp.split("style-src")[1].split(";")[0]
    assert "https://cdn.jsdelivr.net" in csp.split("font-src")[1].split(";")[0]
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_deep_link_route_serves_spa(client):
    """GET /session/<name> must return the SPA shell (HTML 200)."""
    resp = client.get("/session/myproject")
    assert resp.status_code == 200
    # Body should be the SPA shell — look for the doctype and the app
    # title. We use `.lower()` because servers may normalize case.
    body = resp.text.lower()
    assert "<!doctype html" in body
    assert "cloude code" in body


def test_deep_link_route_accepts_dotted_name(client):
    """FastAPI path parameter accepts `.` in the segment.

    Client-side JS enforces the stricter `[A-Za-z0-9_\\- ]+` regex —
    the server's job is just to serve the shell. So a URL like
    `/session/valid_project-1.2.3` returns 200; the browser-side
    router will then reject it and show the error banner.
    """
    resp = client.get("/session/valid_project-1.2.3")
    assert resp.status_code == 200


def test_csp_header_on_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    _assert_csp_present(resp)


def test_csp_header_on_deep_link(client):
    resp = client.get("/session/foo")
    assert resp.status_code == 200
    _assert_csp_present(resp)


def test_csp_header_on_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    _assert_csp_present(resp)


def test_responses_lack_dangerous_headers(client):
    """Paranoid sanity: no `X-Powered-By`, no stray `Set-Cookie` on GETs.

    FastAPI/Starlette don't emit `X-Powered-By` by default, but a future
    middleware could; this is a belt-and-braces guard to catch that
    regression early.
    """
    resp = client.get("/session/paranoid")
    assert resp.status_code == 200
    assert "x-powered-by" not in {k.lower() for k in resp.headers.keys()}
    # No auth flow runs on this path, so no cookies should land.
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}
