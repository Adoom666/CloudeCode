"""Filesystem and endpoint checks for theme background effects.

The JS behaviour (rAF teardown, reduced motion, the hidden-tab pause, the
three-outcome status) is covered by ``tests/test_theme_effects.node.mjs``,
which needs a DOM. What is checked here is the half that a DOM cannot see:
that a manifest which PROMISES an effects module is backed by a file that
actually exists, in a form the client loader will actually accept.

That gap is the interesting one. ``registry.js`` treats a missing or
unloadable ``effects.js`` as a warning and moves on, which is correct
behaviour for the app and useless as a signal: a theme that silently ships
no background looks exactly like a theme that was never given one. These
tests make the difference loud at build time instead.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds --------------
# ``src.config`` exits the process at import time when DEFAULT_WORKING_DIR or
# LOG_DIRECTORY are missing. Set safe defaults BEFORE any ``src.*`` import,
# matching tests/test_themes_endpoint.py.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_fx_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_fx_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.routes as routes_mod  # noqa: E402
from src.api.auth import require_auth  # noqa: E402

BUNDLED_THEMES_DIR = Path(__file__).resolve().parents[1] / "client" / "css" / "themes"

# Shared harness module every effect is expected to build on. Kept as a
# literal rather than derived, so moving the harness has to be a deliberate
# edit here too.
SHARED_HARNESS = BUNDLED_THEMES_DIR / "_shared" / "effects-base.js"
HARNESS_IMPORT = "../_shared/effects-base.js"


def _theme_dirs() -> list[Path]:
    """Every bundled theme directory, i.e. one holding a theme.json.

    Returns:
        list[Path]: Sorted theme directories.
    """
    return sorted(p for p in BUNDLED_THEMES_DIR.iterdir() if (p / "theme.json").is_file())


def _manifests() -> list[tuple[Path, dict]]:
    """Parse every bundled theme.json.

    Returns:
        list[tuple[Path, dict]]: (theme dir, parsed manifest) pairs.
    """
    out = []
    for d in _theme_dirs():
        out.append((d, json.loads((d / "theme.json").read_text(encoding="utf-8"))))
    return out


def _themes_declaring_effects() -> list[tuple[Path, dict]]:
    """Bundled themes whose manifest declares an ``effects`` module.

    Returns:
        list[tuple[Path, dict]]: (theme dir, parsed manifest) pairs.
    """
    return [(d, m) for d, m in _manifests() if m.get("effects")]


def test_theme_discovery_finds_a_plausible_number_of_themes() -> None:
    """Sanity floor, so a broken glob fails loudly rather than vacuously."""
    assert len(_theme_dirs()) >= 20


def test_at_least_one_theme_declares_effects() -> None:
    """Guard against a vacuous pass in every test below.

    Each parametrised test here iterates the declaring themes. If discovery
    broke, that list would be empty and every one of them would pass while
    checking nothing.
    """
    assert _themes_declaring_effects(), "no bundled theme declares an effects module"


@pytest.mark.parametrize("theme_dir", [d for d, _ in _themes_declaring_effects()],
                         ids=lambda p: p.name)
def test_declared_effects_file_exists_on_disk(theme_dir: Path) -> None:
    """A manifest promising an effects module must be backed by a real file."""
    manifest = json.loads((theme_dir / "theme.json").read_text(encoding="utf-8"))
    target = theme_dir / manifest["effects"]
    assert target.is_file(), f"{theme_dir.name}: declares {manifest['effects']}, not on disk"
    assert target.stat().st_size > 0, f"{theme_dir.name}: effects module is empty"


@pytest.mark.parametrize("theme_dir", [d for d, _ in _themes_declaring_effects()],
                         ids=lambda p: p.name)
def test_declared_effects_value_is_a_bare_filename(theme_dir: Path) -> None:
    """The client rejects a slash or a ``..`` in ``effects``; fail here first.

    ``effectsUrlFor()`` in client/js/themes/registry.js refuses any path
    component, and refuses it with a console warning, so a manifest that got
    this wrong would ship as a theme with no background and no other symptom.
    """
    manifest = json.loads((theme_dir / "theme.json").read_text(encoding="utf-8"))
    value = manifest["effects"]
    assert isinstance(value, str) and value
    assert "/" not in value, f"{theme_dir.name}: effects must be a bare filename"
    assert "\\" not in value, f"{theme_dir.name}: effects must be a bare filename"
    assert ".." not in value, f"{theme_dir.name}: effects must not traverse"


@pytest.mark.parametrize("theme_dir", [d for d, _ in _themes_declaring_effects()],
                         ids=lambda p: p.name)
def test_effects_module_builds_on_the_shared_harness(theme_dir: Path) -> None:
    """Every effect must go through the harness, not hand-roll a loop.

    The harness is what enforces reduced motion, the hidden-tab pause, the
    frame cap, full teardown and the unavailable status. An effect that
    imports it inherits all six; an effect that does not inherits none of
    them, and the omission is invisible until a user's laptop fan tells them.
    """
    src = (theme_dir / "effects.js").read_text(encoding="utf-8")
    assert HARNESS_IMPORT in src, (
        f"{theme_dir.name}: effects.js must import {HARNESS_IMPORT}"
    )
    assert "createEffect(" in src, f"{theme_dir.name}: effects.js must call createEffect()"


@pytest.mark.parametrize("theme_dir", [d for d, _ in _themes_declaring_effects()],
                         ids=lambda p: p.name)
def test_effects_module_exports_the_loader_contract(theme_dir: Path) -> None:
    """registry.js calls init() and destroy(); both must be exported."""
    src = (theme_dir / "effects.js").read_text(encoding="utf-8")
    assert "export const init" in src, f"{theme_dir.name}: missing init export"
    assert "export const destroy" in src, f"{theme_dir.name}: missing destroy export"
    assert "export default" in src, f"{theme_dir.name}: missing default export"


def test_shared_harness_exists() -> None:
    """The harness the effects import must be present and served."""
    assert SHARED_HARNESS.is_file()
    assert SHARED_HARNESS.stat().st_size > 0


def test_shared_dir_is_not_discovered_as_a_theme() -> None:
    """``_shared/`` lives under the themes root but must never be a theme.

    It has no theme.json, which is exactly why ``_scan_themes_root`` skips
    it. Asserting it here means a future refactor that adds one, or that
    changes the scanner's skip rule, cannot quietly put a harness in the
    theme picker.
    """
    assert not (SHARED_HARNESS.parent / "theme.json").exists()
    assert SHARED_HARNESS.parent not in _theme_dirs()


def test_no_file_exceeds_the_five_hundred_line_limit() -> None:
    """House rule: no source file over 500 lines."""
    targets = [SHARED_HARNESS] + [d / "effects.js" for d, _ in _themes_declaring_effects()]
    oversized = {
        str(p.relative_to(BUNDLED_THEMES_DIR)): len(p.read_text(encoding="utf-8").splitlines())
        for p in targets
        if len(p.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}, f"files over 500 lines: {oversized}"


def test_endpoint_serves_the_effects_field_for_every_declaring_theme() -> None:
    """The manifests must survive the response model with ``effects`` intact.

    Deliberately not monkeypatched: this walks the REAL bundled root exactly
    as the endpoint does in production. A manifest that parses but loses its
    effects field on the way out is a theme with no background and no error.
    """
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    resp = TestClient(app).get("/api/v1/themes")
    assert resp.status_code == 200
    served = {t["id"]: t for t in resp.json() if t["source"] == "builtin"}

    expected = {d.name: m["effects"] for d, m in _themes_declaring_effects()}
    assert expected, "no bundled theme declares effects"

    for theme_id, filename in expected.items():
        assert theme_id in served, f"{theme_id} was not discovered by the endpoint"
        assert served[theme_id].get("effects") == filename, (
            f"{theme_id}: endpoint served {served[theme_id].get('effects')!r}, "
            f"manifest declares {filename!r}"
        )
