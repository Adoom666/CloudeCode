"""Phase 10 — tests for ``GET /api/v1/themes`` theme manifest discovery.

Covers:
- Manifest discovery (valid / missing-required-field / malformed JSON)
- Bundled vs user source labeling
- Bundled-wins on id collision
- Sort order: bundled (alphabetical by name) then user (alphabetical by name)
- Auth required (401 without bearer token)
- No 500 on pathological theme dirs (empty / no .json / binary garbage)

The endpoint reads the two roots at REQUEST time (via ``_bundled_themes_root``
and ``_user_themes_root`` module-level helpers), which makes monkeypatching
trivial — no app-build-time scanning to work around.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# The pydantic Settings loader sys.exit(1)s if DEFAULT_WORKING_DIR or
# LOG_DIRECTORY are missing. Set safe defaults BEFORE any ``src.*`` import.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_themes_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_themes_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_manifest(parent: Path, theme_id: str, **overrides) -> Path:
    """Drop a valid theme.json into ``<parent>/<theme_id>/theme.json``.

    ``overrides`` lets a test corrupt specific fields (e.g. drop ``name``,
    set ``id`` to a mismatch, etc.). Returns the manifest path.
    """
    theme_dir = parent / theme_id
    theme_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": theme_id,
        "name": f"Theme {theme_id}",
        "description": f"description for {theme_id}",
        "cssVars": {"--bg": "#000"},
        "xterm": {"background": "#000"},
    }
    manifest.update(overrides)
    path = theme_dir / "theme.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _write_raw(parent: Path, theme_id: str, content: bytes) -> Path:
    """Drop ``content`` verbatim into ``<parent>/<theme_id>/theme.json``."""
    theme_dir = parent / theme_id
    theme_dir.mkdir(parents=True, exist_ok=True)
    path = theme_dir / "theme.json"
    path.write_bytes(content)
    return path


@pytest.fixture
def themes_app():
    """Minimal FastAPI app exposing only the themes route.

    ``require_auth`` is overridden to return True so test bodies focus on
    the discovery logic. The dedicated 401 test removes the override.
    """
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app


@pytest.fixture
def themes_app_no_auth_override():
    """Same as ``themes_app`` but WITHOUT the auth override.

    Used by the 401 test to verify the auth gate is still wired.
    """
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1")
    return app


@pytest.fixture
def spy_logger(monkeypatch):
    """Replace ``routes_mod.logger`` with a MagicMock so warning calls
    can be inspected by event-name without going through structlog/stdlib
    logging plumbing (which is not configured in the test process)."""
    spy = MagicMock()
    monkeypatch.setattr(routes_mod, "logger", spy)
    return spy


def _warning_event_names(spy: MagicMock) -> list[str]:
    """Extract the first positional arg (event name) from each
    ``logger.warning(...)`` call."""
    return [
        call.args[0] if call.args else ""
        for call in spy.warning.call_args_list
    ]


@pytest.fixture
def patched_roots(monkeypatch, tmp_path):
    """Point both theme roots at fresh tmp dirs.

    Returns ``(bundled_dir, user_dir)`` so individual tests can drop
    manifests into either.
    """
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(routes_mod, "_bundled_themes_root", lambda: bundled)
    monkeypatch.setattr(routes_mod, "_user_themes_root", lambda: user)
    return bundled, user


# --------------------------------------------------------------------------- #
# Manifest discovery — valid / malformed / missing fields
# --------------------------------------------------------------------------- #


def test_returns_valid_manifest(themes_app, patched_roots):
    bundled, _ = patched_roots
    _write_manifest(bundled, "alpha")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "alpha"
    assert data[0]["source"] == "builtin"


def test_skips_manifest_with_missing_required_field(
    themes_app, patched_roots, spy_logger
):
    bundled, _ = patched_roots
    _write_manifest(bundled, "good")
    # ``description`` is required (no default on ThemeManifest).
    bad_dir = bundled / "broken"
    bad_dir.mkdir()
    (bad_dir / "theme.json").write_text(
        json.dumps({"id": "broken", "name": "Broken"}), encoding="utf-8"
    )

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["good"]
    assert "theme_manifest_validation_failed" in _warning_event_names(spy_logger)


def test_skips_malformed_json(themes_app, patched_roots, spy_logger):
    bundled, _ = patched_roots
    _write_raw(bundled, "junk", b"{not valid json")
    _write_manifest(bundled, "good")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["good"]
    assert "theme_manifest_parse_failed" in _warning_event_names(spy_logger)


def test_skips_id_dir_mismatch(themes_app, patched_roots, spy_logger):
    bundled, _ = patched_roots
    # Manifest ``id`` says "wrongid" but lives in dir "actualname"
    _write_manifest(bundled, "actualname", id="wrongid")
    _write_manifest(bundled, "good")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["good"]
    assert "theme_manifest_id_dir_mismatch" in _warning_event_names(spy_logger)


# --------------------------------------------------------------------------- #
# Bundled vs user source labeling
# --------------------------------------------------------------------------- #


def test_source_labels_bundled_and_user(themes_app, patched_roots):
    bundled, user = patched_roots
    _write_manifest(bundled, "bun-only")
    _write_manifest(user, "usr-only")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()}
    assert by_id["bun-only"]["source"] == "builtin"
    assert by_id["usr-only"]["source"] == "user"


# --------------------------------------------------------------------------- #
# Bundled wins on collision
# --------------------------------------------------------------------------- #


def test_bundled_wins_on_id_collision(themes_app, patched_roots, spy_logger):
    bundled, user = patched_roots
    _write_manifest(bundled, "shared", name="Bundled Shared")
    _write_manifest(user, "shared", name="User Shared")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    shared = [t for t in data if t["id"] == "shared"]
    # Exactly one entry, and it must be the bundled one (source=builtin).
    assert len(shared) == 1
    assert shared[0]["source"] == "builtin"
    assert shared[0]["name"] == "Bundled Shared"


# --------------------------------------------------------------------------- #
# Sort order
# --------------------------------------------------------------------------- #


def test_sort_order_bundled_first_then_user_alphabetical(
    themes_app, patched_roots
):
    """Per the route docstring: bundled section sorted alphabetical by name,
    user section sorted alphabetical by id."""
    bundled, user = patched_roots
    # Bundled: out-of-order names; expect alphabetical-by-name in response
    _write_manifest(bundled, "bz", name="Z-bundled")
    _write_manifest(bundled, "ba", name="A-bundled")
    _write_manifest(bundled, "bm", name="M-bundled")
    # User: out-of-order ids; expect alphabetical-by-id in response
    _write_manifest(user, "uz", name="user-z-name")
    _write_manifest(user, "ua", name="user-a-name")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    # All bundled themes come first
    sources = [t["source"] for t in data]
    assert sources == ["builtin", "builtin", "builtin", "user", "user"]
    # Bundled section: alphabetical by name (case-insensitive)
    bundled_names = [t["name"] for t in data if t["source"] == "builtin"]
    assert bundled_names == ["A-bundled", "M-bundled", "Z-bundled"]
    # User section: alphabetical by id (case-insensitive)
    user_ids = [t["id"] for t in data if t["source"] == "user"]
    assert user_ids == ["ua", "uz"]


# --------------------------------------------------------------------------- #
# Auth required
# --------------------------------------------------------------------------- #


def test_auth_required_returns_401(themes_app_no_auth_override, patched_roots):
    bundled, _ = patched_roots
    _write_manifest(bundled, "alpha")

    client = TestClient(themes_app_no_auth_override)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Pathological theme dirs — must never 500
# --------------------------------------------------------------------------- #


def test_empty_dirs_return_empty_list(themes_app, patched_roots):
    # patched_roots fixture creates empty bundled + user dirs
    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    assert resp.json() == []


def test_dir_with_no_theme_json_returns_empty(themes_app, patched_roots):
    bundled, _ = patched_roots
    # A directory exists, but it has no theme.json (and a stray file).
    sub = bundled / "no-manifest"
    sub.mkdir()
    (sub / "README.md").write_text("not a theme", encoding="utf-8")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    assert resp.json() == []


def test_binary_garbage_theme_json_does_not_500(themes_app, patched_roots):
    bundled, _ = patched_roots
    # 256 bytes of random-looking binary masquerading as a theme.json.
    _write_raw(
        bundled,
        "binary",
        bytes(range(256)),
    )
    _write_manifest(bundled, "ok")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["ok"]


def test_user_root_none_does_not_500(themes_app, monkeypatch, tmp_path):
    """When no user themes dir exists, _user_themes_root returns None."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    _write_manifest(bundled, "only-bundled")
    monkeypatch.setattr(routes_mod, "_bundled_themes_root", lambda: bundled)
    monkeypatch.setattr(routes_mod, "_user_themes_root", lambda: None)

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "only-bundled"
    assert data[0]["source"] == "builtin"


def test_hidden_dirs_are_ignored(themes_app, patched_roots):
    """Dirs whose names start with '.' (e.g. .DS_Store, .git) are skipped."""
    bundled, _ = patched_roots
    _write_manifest(bundled, "visible")
    # Drop a ".hidden" dir that LOOKS like a valid manifest — it should be
    # filtered out by the dotfile-skip logic in _scan_themes_root.
    hidden = bundled / ".hidden"
    hidden.mkdir()
    (hidden / "theme.json").write_text(
        json.dumps(
            {
                "id": ".hidden",
                "name": "Hidden",
                "description": "should never appear",
                "cssVars": {},
                "xterm": {},
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["visible"]


def test_client_supplied_source_field_is_overwritten(themes_app, patched_roots):
    """A theme.json claiming ``source: "user"`` in the bundled dir must
    still be labeled ``builtin`` — the server stamps source one-way."""
    bundled, _ = patched_roots
    _write_manifest(bundled, "spoof", source="user")  # liar

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source"] == "builtin"  # server stamp wins


# --------------------------------------------------------------------------- #
# The `audio` block — the regression that made the whole app silent.
#
# GET /api/v1/themes declares response_model=List[ThemeManifest]. That model
# had no `audio` field, so Pydantic dropped the block at parse time and
# FastAPI serialised it away again. Every theme.json on disk carried a
# correct block; the client received none of them, built no playback node,
# and honestly reported that the theme had no track. Four separate audio
# fixes landed downstream of this while it was the actual cause.
#
# The test that would have caught it asserts on the RESPONSE, not on the
# file: reading theme.json back and finding `audio` there proves nothing,
# because that was true throughout the outage.
# --------------------------------------------------------------------------- #

_AUDIO_BLOCK = {
    "src": "/static/assets/audio/dead-ship.m4a",
    "srcFallback": "/static/assets/audio/dead-ship.ogg",
    "volume": 0.5,
    "fadeMs": 1500,
}


def test_audio_block_survives_the_response_model(themes_app, patched_roots):
    """The `audio` block must reach the client, field for field."""
    bundled, _ = patched_roots
    _write_manifest(bundled, "withaudio", audio=dict(_AUDIO_BLOCK))

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["audio"] == _AUDIO_BLOCK


def test_theme_without_audio_reports_null_not_missing(themes_app, patched_roots):
    """A silent theme says so explicitly rather than omitting the key.

    The client branches on `m.audio`, and an absent key and a null are the
    same to it, but an explicit null is the difference between "this theme
    has no track" and "something ate the field" when reading a payload by
    hand during the next outage.
    """
    bundled, _ = patched_roots
    _write_manifest(bundled, "silent")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert "audio" in data[0]
    assert data[0]["audio"] is None


def test_every_bundled_theme_serves_its_audio_block():
    """All shipped themes must serve audio through the REAL bundled root.

    Deliberately not monkeypatched: this walks `client/css/themes/` exactly
    as the endpoint does in production. A manifest that parses but loses its
    audio on the way out is silence for that theme and nothing else.
    """
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    resp = TestClient(app).get("/api/v1/themes")
    assert resp.status_code == 200
    bundled = [t for t in resp.json() if t["source"] == "builtin"]
    assert bundled, "no bundled themes discovered"

    silent = [t["id"] for t in bundled if not (t.get("audio") or {}).get("src")]
    assert silent == [], f"themes serving no audio src: {silent}"


def test_out_of_range_audio_values_are_clamped_not_rejected(
    themes_app, patched_roots
):
    """A bad number must not take the whole theme out of the selector.

    `_load_manifest` drops any manifest that fails validation, so a strict
    validator here would turn one typo into a theme that silently vanishes
    from the picker. Clamp instead.
    """
    bundled, _ = patched_roots
    _write_manifest(
        bundled,
        "loud",
        audio={**_AUDIO_BLOCK, "volume": 9.0, "fadeMs": -200},
    )

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1, "a clampable value must not drop the theme"
    assert data[0]["audio"]["volume"] == 1.0
    assert data[0]["audio"]["fadeMs"] == 0


def test_audio_block_without_src_drops_the_theme(themes_app, patched_roots):
    """`src` is required: an audio block with no source is a real error.

    Distinct from clamping. A missing src cannot be repaired into anything
    meaningful, so the manifest is skipped and logged like any other
    malformed one rather than served as a half-usable track.
    """
    bundled, _ = patched_roots
    _write_manifest(bundled, "broken", audio={"volume": 0.5})

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# themeCss: dead-file removal (2026-08-19)
#
# client/css/themes/*/theme.css was never wired to any <link href> - the
# client applies themes entirely through cssVars and effects.js. All
# theme.css files and the themeCss key that pointed at them were removed.
# The themeCss field stays on ThemeManifest so a manifest that declares one
# without shipping the file fails loudly (skip + log) instead of the
# silent-drop that a missing pydantic field used to cause.
# --------------------------------------------------------------------------- #


def test_declared_themecss_missing_file_drops_theme_and_logs_loudly(
    themes_app, patched_roots, spy_logger
):
    """A manifest that declares `themeCss` but does not ship the file must
    be skipped and logged, never silently served with a dangling reference.
    """
    bundled, _ = patched_roots
    _write_manifest(bundled, "good")
    _write_manifest(bundled, "dangling", themeCss="theme.css")
    # Deliberately do NOT create dangling/theme.css on disk.

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["good"]
    assert "theme_manifest_themecss_missing" in _warning_event_names(spy_logger)


def test_declared_themecss_with_file_present_still_validates(
    themes_app, patched_roots
):
    """The inverse of the drop case: a manifest that declares `themeCss`
    and actually ships the file must validate and pass the field through.
    """
    bundled, _ = patched_roots
    theme_dir = _write_manifest(bundled, "has-css", themeCss="theme.css").parent
    (theme_dir / "theme.css").write_text("/* real */", encoding="utf-8")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["themeCss"] == "theme.css"


def test_theme_without_themecss_validates_with_null_field(
    themes_app, patched_roots
):
    """The common case post-removal: no `themeCss` key at all must still
    validate cleanly and report the field as null, not error or vanish.
    """
    bundled, _ = patched_roots
    _write_manifest(bundled, "plain")

    client = TestClient(themes_app)
    resp = client.get("/api/v1/themes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["themeCss"] is None


def test_no_bundled_theme_css_files_remain_on_disk():
    """Filesystem assertion, not just endpoint behavior: no `theme.css`
    exists anywhere under the real bundled themes root any more.
    """
    root = routes_mod._bundled_themes_root()
    leftovers = sorted(str(p) for p in root.glob("*/theme.css"))
    assert leftovers == [], f"theme.css files still present: {leftovers}"


def test_no_bundled_theme_declares_a_dangling_themecss():
    """Walks the REAL bundled root exactly as the endpoint does. If any
    shipped theme.json still declares `themeCss`, its file must exist -
    otherwise `_load_manifest` would (correctly) drop that theme, and this
    test names the dangling reference instead of the theme just vanishing.
    """
    root = routes_mod._bundled_themes_root()
    dangling = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / "theme.json"
        if not manifest_path.is_file():
            continue
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        theme_css = raw.get("themeCss")
        if theme_css and not (child / theme_css).is_file():
            dangling.append(child.name)
    assert dangling == [], f"themes declaring a missing themeCss: {dangling}"


def test_every_bundled_theme_declares_and_ships_effects_js():
    """Bidirectional check against the REAL bundled root: every theme must
    both declare an `effects` filename in its manifest AND have that file
    present on disk - and nothing may ship an effects.js that no manifest
    declares. effects.js is the ONLY per-theme JS/CSS asset that actually
    loads in the running app (see themeCss above for the one that never did).
    """
    root = routes_mod._bundled_themes_root()
    theme_dirs = sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    assert theme_dirs, "no bundled theme directories found"

    not_declared = []
    declared_but_missing = []
    for theme_dir in theme_dirs:
        manifest_path = theme_dir / "theme.json"
        effects_on_disk = (theme_dir / "effects.js").is_file()
        effects_declared = None
        if manifest_path.is_file():
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            effects_declared = raw.get("effects")
        if effects_on_disk and effects_declared != "effects.js":
            not_declared.append(theme_dir.name)
        if effects_declared and not (theme_dir / effects_declared).is_file():
            declared_but_missing.append(theme_dir.name)

    assert not_declared == [], f"effects.js present but undeclared: {not_declared}"
    assert declared_but_missing == [], (
        f"effects declared but missing on disk: {declared_but_missing}"
    )
