"""Tests for the browser-paste image upload endpoint.

Covers POST /api/v1/sessions/upload-image and the destroy-session
upload-bucket cleanup. The validation core (validate_image /
save_to_session_dir) is exercised through the route so the wiring
contract is pinned end-to-end.

Pillow generates real bytes for png / jpeg / webp fixtures so the
magic-byte cross-check inside validate_image() runs against authentic
image headers — no mocking of PIL.Image.verify().
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

# ---- env bootstrap so ``src.config`` import succeeds -------------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_upload_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_upload_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.api.routes import router as api_router  # noqa: E402
from src.core.session_manager import SessionManager  # noqa: E402
from src.core.upload_sweeper import UPLOAD_DIR_NAME  # noqa: E402
from src.models import Session, SessionStatus  # noqa: E402


JWT_SECRET = "test-secret-for-upload-image"
MAX_SIZE_MB = 10


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _make_fake_auth_config():
    """AuthConfig stand-in that satisfies upload-image and require_auth."""
    uploads = SimpleNamespace(
        enabled=True,
        ttl_seconds=86400,
        sweep_interval_seconds=3600,
        max_size_mb=MAX_SIZE_MB,
    )
    return SimpleNamespace(
        jwt_secret=JWT_SECRET,
        jwt_expiry_minutes=30,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=604800,
        refresh_grace_seconds=10,
        totp_secret="X" * 32,
        projects=[],
        uploads=uploads,
    )


@pytest.fixture(autouse=True)
def patched_auth(monkeypatch):
    """Inject a stable fake AuthConfig at every consumer site."""
    fake = _make_fake_auth_config()
    from src.config import settings as real_settings

    def fake_loader(self=None):
        return fake

    monkeypatch.setattr(type(real_settings), "load_auth_config", fake_loader)
    return fake


def _mint_access_token() -> str:
    payload = {
        "exp": datetime.utcnow() + timedelta(minutes=15),
        "iat": datetime.utcnow(),
        "sub": "claudetunnel_user",
        "typ": "access",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _png_bytes(size: tuple[int, int] = (10, 10), color: str = "red") -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), "green").save(buf, format="WEBP")
    return buf.getvalue()


class _StubSessionManager:
    """Minimal SessionManager stand-in for route-level testing."""

    def __init__(self, working_dir: Path | None):
        self._wd = working_dir
        if working_dir is not None:
            self.session = Session(
                id="ses_test01",
                working_dir=str(working_dir),
                status=SessionStatus.RUNNING,
            )
            backend = MagicMock()
            backend.is_alive.return_value = True
            self.backend = backend
        else:
            self.session = None
            self.backend = None

    def has_active_session(self) -> bool:
        return (
            self.session is not None
            and self.session.status == SessionStatus.RUNNING
            and self.backend is not None
            and self.backend.is_alive()
        )


@pytest.fixture
def app_with_session(tmp_path):
    """Build a FastAPI app whose state.session_manager points at tmp_path."""
    app = FastAPI()
    app.state.session_manager = _StubSessionManager(tmp_path)
    app.include_router(api_router, prefix="/api/v1")
    return app


@pytest.fixture
def app_without_session():
    app = FastAPI()
    app.state.session_manager = _StubSessionManager(None)
    app.include_router(api_router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app_with_session):
    return TestClient(app_with_session)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_mint_access_token()}"}


# --------------------------------------------------------------------------- #
# Tests — endpoint behavior
# --------------------------------------------------------------------------- #
def test_upload_requires_auth(client):
    """POST without Authorization header → 401."""
    files = {"file": ("test.png", _png_bytes(), "image/png")}
    r = client.post("/api/v1/sessions/upload-image", files=files)
    assert r.status_code == 401, r.text


def test_upload_valid_png_writes_to_session_dir(client, auth_headers, tmp_path):
    data = _png_bytes()
    files = {"file": ("test.png", data, "image/png")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 201, r.text
    body = r.json()

    target = Path(body["path"])
    assert target.exists(), f"upload not on disk: {target}"
    assert target.parent == (tmp_path / UPLOAD_DIR_NAME).resolve()
    assert target.suffix == ".png"
    assert body["filename"] == target.name
    assert body["size"] == len(data)
    assert target.read_bytes() == data


def test_upload_valid_jpeg_writes_to_session_dir(client, auth_headers, tmp_path):
    data = _jpeg_bytes()
    files = {"file": ("photo.jpg", data, "image/jpeg")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    target = Path(body["path"])
    assert target.exists()
    assert target.suffix == ".jpg"
    assert target.parent == (tmp_path / UPLOAD_DIR_NAME).resolve()
    assert body["size"] == len(data)


def test_upload_valid_webp_accepted(client, auth_headers, tmp_path):
    data = _webp_bytes()
    files = {"file": ("clip.webp", data, "image/webp")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 201, r.text
    target = Path(r.json()["path"])
    assert target.exists()
    assert target.suffix == ".webp"


def test_upload_bad_extension_rejected(client, auth_headers):
    files = {"file": ("notes.txt", b"not an image", "text/plain")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 400, r.text
    assert "extension" in r.json()["detail"].lower()


def test_upload_magic_byte_mismatch_rejected(client, auth_headers):
    """File named .png but containing JPEG bytes → 400."""
    jpeg_bytes = _jpeg_bytes()
    files = {"file": ("fake.png", jpeg_bytes, "image/png")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "match" in detail or "jpeg" in detail


def test_upload_oversized_rejected(client, auth_headers):
    """Bytes larger than max_size_mb cap → 400 before any validation."""
    oversized = b"\x89PNG\r\n\x1a\n" + b"A" * (MAX_SIZE_MB * 1024 * 1024 + 1)
    files = {"file": ("big.png", oversized, "image/png")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 400, r.text
    assert "size" in r.json()["detail"].lower() or "maximum" in r.json()["detail"].lower()


def test_upload_no_active_session_rejected(app_without_session, auth_headers):
    """No active session in session_manager → 409."""
    client = TestClient(app_without_session)
    files = {"file": ("test.png", _png_bytes(), "image/png")}
    r = client.post("/api/v1/sessions/upload-image", files=files, headers=auth_headers)
    assert r.status_code == 409, r.text
    assert "session" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Tests — destroy-session cleanup
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_destroy_session_cleans_uploads_dir(tmp_path, monkeypatch):
    """destroy_session() rmtrees ``.cloude_uploads/`` after backend stop."""
    import src.core.session_manager as sm_mod

    # Redirect metadata path so destroy_session's unlink stays inside tmp_path.
    # pydantic Settings rejects attribute mutation, so patch the bound method
    # via object.__setattr__ — same pattern as test_session_agent_type.py.
    metadata_path = tmp_path / "session_metadata.json"
    object.__setattr__(
        sm_mod.settings,
        "get_session_metadata_path",
        lambda: metadata_path,
    )

    uploads_dir = tmp_path / UPLOAD_DIR_NAME
    uploads_dir.mkdir(mode=0o700)
    (uploads_dir / "foo.png").write_bytes(b"fake-png-bytes")
    assert uploads_dir.exists()

    manager = SessionManager()
    manager.session = Session(
        id="ses_destroy01",
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
    )
    backend = MagicMock()

    async def _stop():
        return None

    backend.stop = _stop
    backend.tmux_session = "cloude_destroy01"
    manager.backend = backend

    await manager.destroy_session()

    assert not uploads_dir.exists(), "uploads dir must be removed on destroy"
    assert manager.session is None
