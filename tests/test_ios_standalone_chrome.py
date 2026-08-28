"""Tests for the iOS home-screen / standalone chrome.

These assert what is actually SERVED, not what is on disk: the SPA shell
is rendered through ``_render_index_html()`` and could in principle strip
or rewrite head content, and the manifest/icon are served from routes
rather than the static mount.

Deliberately NOT asserted: that a service worker exists. There is none,
because this app is served over plain http on a Tailscale hostname and
``navigator.serviceWorker.register()`` requires a secure context. The
last test in this file pins that absence so nobody "helpfully" adds one
without also adding TLS. See docs/ios-standalone.md.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- env bootstrap so pydantic Settings doesn't sys.exit(1) -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ios_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ios_logs_"))
os.environ.setdefault("CLOUDE_STATE_DIR", tempfile.mkdtemp(prefix="cc_ios_state_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    """TestClient over the real app, lifespan on (root route needs state)."""
    from fastapi.testclient import TestClient
    from src.main import app

    with TestClient(app) as tc:
        yield tc


@pytest.fixture(scope="module")
def shell(client):
    """The served SPA shell HTML."""
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


class TestServedMetaTags:
    def test_viewport_opts_into_cover(self, shell):
        # Without viewport-fit=cover the page letterboxes on a notched
        # iPhone and none of the safe-area CSS does anything.
        assert "viewport-fit=cover" in shell

    def test_web_app_capable(self, shell):
        assert 'name="apple-mobile-web-app-capable" content="yes"' in shell
        assert 'name="mobile-web-app-capable" content="yes"' in shell

    def test_status_bar_style(self, shell):
        assert 'name="apple-mobile-web-app-status-bar-style"' in shell
        assert 'content="black-translucent"' in shell

    def test_home_screen_title(self, shell):
        assert 'name="apple-mobile-web-app-title"' in shell

    def test_theme_color(self, shell):
        assert 'name="theme-color"' in shell

    def test_manifest_link_points_at_the_origin_root(self, shell):
        # Scope/start_url resolve relative to the manifest URL; a
        # /static/ manifest would scope the app to /static/ and a
        # standalone launch of "/" would bounce out to a browser tab.
        assert 'rel="manifest" href="/manifest.webmanifest"' in shell

    def test_apple_touch_icon_is_a_png(self, shell):
        # iOS does not read SVG for apple-touch-icon.
        assert 'rel="apple-touch-icon"' in shell
        line = [ln for ln in shell.splitlines() if 'rel="apple-touch-icon"' in ln][0]
        assert ".png" in line


class TestManifestRoute:
    def test_served_from_root(self, client):
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200

    def test_media_type_is_the_one_chrome_accepts(self, client):
        resp = client.get("/manifest.webmanifest")
        assert resp.headers["content-type"].startswith("application/manifest+json")

    def test_is_valid_json_with_the_required_keys(self, client):
        data = json.loads(client.get("/manifest.webmanifest").text)
        for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
            assert key in data, f"manifest missing {key}"

    def test_display_is_standalone(self, client):
        data = json.loads(client.get("/manifest.webmanifest").text)
        assert data["display"] == "standalone"

    def test_scope_is_the_origin_root(self, client):
        data = json.loads(client.get("/manifest.webmanifest").text)
        assert data["scope"] == "/"
        assert data["start_url"] == "/"

    def test_declares_a_maskable_icon(self, client):
        data = json.loads(client.get("/manifest.webmanifest").text)
        purposes = {i.get("purpose") for i in data["icons"]}
        assert "maskable" in purposes

    def test_every_declared_icon_actually_resolves(self, client):
        data = json.loads(client.get("/manifest.webmanifest").text)
        for icon in data["icons"]:
            resp = client.get(icon["src"])
            assert resp.status_code == 200, f"{icon['src']} -> {resp.status_code}"
            assert resp.headers["content-type"] == "image/png"


class TestAppleTouchIconRoute:
    def test_root_probe_answers(self, client):
        # iOS probes the origin root when the <link> is absent or fails.
        resp = client.get("/apple-touch-icon.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"

    def test_linked_icon_resolves(self, client):
        resp = client.get("/static/assets/icons/icon-180.png")
        assert resp.status_code == 200

    def test_icon_is_the_size_it_claims(self):
        from PIL import Image
        path = ROOT / "client" / "assets" / "icons" / "icon-180.png"
        with Image.open(path) as img:
            assert img.size == (180, 180)


class TestSafeAreaCss:
    """viewport-fit=cover without safe-area padding is a regression, not a
    feature: it puts the header under the notch. Pin the pairing."""

    @pytest.fixture(scope="class")
    def css(self, client):
        resp = client.get("/static/css/ios-chrome.css")
        assert resp.status_code == 200
        return resp.text

    def test_header_pads_the_top_inset(self, css):
        assert "env(safe-area-inset-top)" in css

    def test_all_four_insets_are_handled(self, css):
        for side in ("top", "bottom", "left", "right"):
            assert f"env(safe-area-inset-{side})" in css

    def test_floating_controls_clear_the_home_indicator(self, css):
        assert "env(safe-area-inset-bottom)" in css
        assert ".slash-commands-btn" in css


class TestNoServiceWorker:
    """Plain http is not a secure context, so a service worker cannot
    register. Absence is the correct state, and it is load-bearing: a
    registration that silently never takes looks like offline support."""

    def test_shell_does_not_register_a_service_worker(self, shell):
        assert "serviceWorker" not in shell

    def test_no_service_worker_file_shipped(self):
        client_dir = ROOT / "client"
        found = [
            p.name for p in client_dir.rglob("*.js")
            if p.name in ("sw.js", "service-worker.js", "serviceworker.js")
        ]
        assert found == [], f"unexpected service worker: {found}"
