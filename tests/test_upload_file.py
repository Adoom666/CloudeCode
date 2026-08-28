"""Tests for the ARBITRARY-FILE half of the upload endpoint.

Covers POST /api/v1/sessions/upload-file, added 2026-08-16 when the
image-only paste feature was widened to any file so Claude can be handed
a path to something it reads for itself.

The image contract (magic-byte cross-check, tighter cap) is pinned in
tests/test_upload_image.py. What THIS file pins is the part that is new
attack surface: a client-supplied filename now influences the name of a
file written to disk, so sanitisation, path containment and the
no-silent-overwrite guarantee each get direct coverage, at the pure-helper
level AND through the route.

Threat model worth stating: this app runs on the user's own machine and
is reachable from the LAN, behind TOTP auth. The guards below are what
stand between an authenticated request and an arbitrary write outside the
session's own upload bucket.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# ---- env bootstrap so ``src.config`` import succeeds -------------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_uf_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_uf_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.api.routes import router as api_router  # noqa: E402
from src.api.uploads import (  # noqa: E402
    MAX_BASENAME_LENGTH,
    extension_of,
    sanitize_filename,
    save_upload_to_session_dir,
    validate_upload,
)
from src.core.upload_sweeper import UPLOAD_DIR_NAME  # noqa: E402
from src.models import Session, SessionStatus  # noqa: E402


JWT_SECRET = "test-secret-for-upload-file"
MAX_SIZE_MB = 10
MAX_FILE_SIZE_MB = 2  # small so the oversize test does not allocate 50 MB


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _make_fake_auth_config():
    """AuthConfig stand-in that satisfies upload-file and require_auth."""
    uploads = SimpleNamespace(
        enabled=True,
        ttl_seconds=86400,
        sweep_interval_seconds=3600,
        max_size_mb=MAX_SIZE_MB,
        max_file_size_mb=MAX_FILE_SIZE_MB,
    )
    return SimpleNamespace(
        jwt_secret=JWT_SECRET,
        jwt_expiry_minutes=30,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=604800,
        refresh_grace_seconds=10,
        totp_secret="X" * 32,
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


class _StubSessionManager:
    """Minimal SessionManager stand-in for route-level testing."""

    def __init__(self, working_dir: Path):
        self.session = Session(
            id="ses_test01",
            working_dir=str(working_dir),
            status=SessionStatus.RUNNING,
        )
        backend = MagicMock()
        backend.is_alive.return_value = True
        self.backend = backend

    def has_active_session(self) -> bool:
        return True


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.session_manager = _StubSessionManager(tmp_path)
    app.include_router(api_router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_mint_access_token()}"}


# --------------------------------------------------------------------------- #
# Unit — sanitize_filename()
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "declared,expected",
    [
        # Traversal, POSIX and Windows flavours. Both must lose every
        # directory component; the result carries no separator at all.
        ("../../etc/passwd", "passwd"),
        ("../../../../../../etc/shadow", "shadow"),
        ("..\\..\\Windows\\System32\\evil.dll", "evil.dll"),
        ("/absolute/path/to/thing.txt", "thing.txt"),
        ("....//....//x.txt", "x.txt"),
        # Shell metacharacters collapse rather than survive into a prompt.
        ('my "quoted" report.pdf', "my_quoted_report.pdf"),
        ("rm -rf $(whoami).txt", "rm_-rf_whoami_.txt"),
        ("a;b|c&d.log", "a_b_c_d.log"),
        ("with spaces here.txt", "with_spaces_here.txt"),
        # Unicode folds to its ASCII skeleton rather than to underscores.
        ("résumé final.pdf", "resume_final.pdf"),
        # Leading dot/dash are stripped: no hidden files, no option flags.
        (".bashrc", "bashrc"),
        ("-rf.txt", "rf.txt"),
        # Degenerate input still yields a usable name.
        ("", "upload"),
        ("...", "upload"),
        ("☃☃☃", "upload"),
    ],
)
def test_sanitize_filename_cases(declared: str, expected: str):
    """Hostile and awkward filenames reduce to a safe, separator-free name."""
    assert sanitize_filename(declared) == expected


def test_sanitize_filename_never_contains_separators():
    """No sanitised name can carry a path separator, whatever goes in."""
    for hostile in ("../../a", "a/b/c", "a\\b\\c", "/", "\\", "..", "./."):
        out = sanitize_filename(hostile)
        assert "/" not in out and "\\" not in out
        assert out not in ("", ".", "..")


def test_sanitize_filename_truncates_but_keeps_extension():
    """An absurdly long name is trimmed in the STEM, never the extension."""
    out = sanitize_filename("z" * 900 + ".tar.gz")
    assert len(out) <= MAX_BASENAME_LENGTH
    assert out.endswith(".gz")


def test_sanitize_filename_strips_control_characters():
    """A newline in a filename can never reach the terminal injection layer."""
    out = sanitize_filename("evil\nname\r\ttest.txt")
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert out.endswith(".txt")


def test_extension_of():
    """Extension helper lowercases, drops the dot, and tolerates no-extension."""
    assert extension_of("photo.PNG") == "png"
    assert extension_of("archive.tar.gz") == "gz"
    assert extension_of("README") == ""
    assert extension_of("bashrc") == ""


# --------------------------------------------------------------------------- #
# Unit — validate_upload()
# --------------------------------------------------------------------------- #
def test_validate_upload_passes_plain_bytes_through():
    """A non-image is size-capped only; its bytes are never parsed."""
    data = b"\x00\x01\x02 not remotely a document"
    out, name = validate_upload(data, "notes.txt", MAX_SIZE_MB, MAX_FILE_SIZE_MB)
    assert out == data
    assert name == "notes.txt"


def test_validate_upload_rejects_oversize_non_image():
    """Past the non-image cap → 400 naming the limit."""
    data = b"A" * (MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
    with pytest.raises(HTTPException) as exc:
        validate_upload(data, "big.bin", MAX_SIZE_MB, MAX_FILE_SIZE_MB)
    assert exc.value.status_code == 400
    assert "maximum size" in exc.value.detail.lower()


def test_validate_upload_rejects_empty():
    """An empty payload is a client bug, not a zero-byte file to keep."""
    with pytest.raises(HTTPException) as exc:
        validate_upload(b"", "empty.txt", MAX_SIZE_MB, MAX_FILE_SIZE_MB)
    assert exc.value.status_code == 400


def test_validate_upload_applies_image_contract_to_image_extension():
    """An image extension still gets the magic-byte check, not a free pass."""
    with pytest.raises(HTTPException) as exc:
        validate_upload(b"nope", "fake.png", MAX_SIZE_MB, MAX_FILE_SIZE_MB)
    assert exc.value.status_code == 400


def test_validate_upload_sanitizes_before_dispatch():
    """Extension dispatch reads the SANITISED name, not the raw one."""
    _, name = validate_upload(b"data", "../../notes.txt", MAX_SIZE_MB, MAX_FILE_SIZE_MB)
    assert name == "notes.txt"


# --------------------------------------------------------------------------- #
# Unit — save_upload_to_session_dir()
# --------------------------------------------------------------------------- #
def test_save_stays_inside_bucket_for_traversal_name(tmp_path):
    """A traversal attempt lands INSIDE the bucket, never above it."""
    target = save_upload_to_session_dir(
        b"payload", sanitize_filename("../../../../etc/passwd"), str(tmp_path)
    )
    bucket = (tmp_path / UPLOAD_DIR_NAME).resolve()
    assert target.parent == bucket
    # The definitive check: nothing was written outside the bucket.
    assert target.resolve().is_relative_to(bucket)
    assert not (tmp_path.parent / "passwd").exists()
    assert target.read_bytes() == b"payload"


def test_save_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    """With the uuid forced to collide, the write FAILS rather than clobbers.

    The uuid prefix makes a real collision practically impossible, so it
    is pinned by forcing one: O_EXCL must turn it into a loud error, not a
    silently destroyed file.
    """
    import src.api.uploads as uploads_mod

    class _FixedUUID:
        hex = "deadbeef" + "0" * 24

    monkeypatch.setattr(uploads_mod.uuid, "uuid4", lambda: _FixedUUID())

    first = save_upload_to_session_dir(b"original", "notes.txt", str(tmp_path))
    assert first.read_bytes() == b"original"

    with pytest.raises(HTTPException) as exc:
        save_upload_to_session_dir(b"replacement", "notes.txt", str(tmp_path))
    assert exc.value.status_code == 409
    # The original survived untouched — that is the whole point.
    assert first.read_bytes() == b"original"


def test_save_sets_restrictive_modes(tmp_path):
    """Bucket 0o700, file 0o600 — another local user cannot read either."""
    target = save_upload_to_session_dir(b"secret", "notes.txt", str(tmp_path))
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (target.parent.stat().st_mode & 0o777) == 0o700


def test_save_preserves_name_and_extension(tmp_path):
    """The saved basename is <uuid8>-<name>, so the type stays recognisable."""
    target = save_upload_to_session_dir(b"data", "quarterly.pdf", str(tmp_path))
    assert target.name.endswith("-quarterly.pdf")
    assert target.suffix == ".pdf"


def test_save_distinct_names_for_same_original(tmp_path):
    """Two uploads of the same filename coexist rather than one winning."""
    a = save_upload_to_session_dir(b"one", "report.txt", str(tmp_path))
    b = save_upload_to_session_dir(b"two", "report.txt", str(tmp_path))
    assert a != b
    assert a.read_bytes() == b"one"
    assert b.read_bytes() == b"two"


# --------------------------------------------------------------------------- #
# Route — end-to-end wiring
# --------------------------------------------------------------------------- #
def test_route_requires_auth(client):
    """POST without Authorization header → 401."""
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("notes.txt", b"data", "text/plain")},
    )
    assert r.status_code == 401, r.text


def test_route_accepts_text_file(client, auth_headers, tmp_path):
    """A .txt round-trips and the returned path is the real file on disk."""
    data = b"hello from a plain text file\n"
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("notes.txt", data, "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    target = Path(body["path"])
    assert target.is_absolute()
    assert target.exists()
    assert target.read_bytes() == data
    assert target.parent == (tmp_path / UPLOAD_DIR_NAME).resolve()
    assert body["size"] == len(data)
    assert body["filename"] == target.name


def test_route_accepts_binary_file(client, auth_headers):
    """A binary payload is stored byte-for-byte, never parsed or transcoded."""
    data = b"%PDF-1.4\n\x00\x01\x02\xff\xfe binary tail"
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("report.pdf", data, "application/pdf")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    target = Path(r.json()["path"])
    assert target.read_bytes() == data
    assert target.suffix == ".pdf"


def test_route_sanitizes_traversal_filename(client, auth_headers, tmp_path):
    """A traversal filename cannot write outside the session bucket."""
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("../../../../tmp/pwned.txt", b"x", "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    target = Path(r.json()["path"])
    bucket = (tmp_path / UPLOAD_DIR_NAME).resolve()
    assert target.parent == bucket
    assert target.name.endswith("-pwned.txt")
    assert ".." not in str(target)


def test_route_sanitizes_spaces_and_quotes(client, auth_headers):
    """Spaces and quotes never reach disk, so the injected path stays bare.

    Asserted as PROPERTIES, not as one exact string: TestClient
    percent-encodes a quote inside the multipart Content-Disposition
    header, so the server legitimately sees a different name than a
    browser would send. The exact transform is pinned at the unit level
    in test_sanitize_filename_cases instead.
    """
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("my 'big' report v2.txt", b"x", "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    name = Path(r.json()["path"]).name
    assert " " not in name and '"' not in name and "'" not in name
    assert name.endswith(".txt")
    assert "report" in name and "v2" in name


def test_route_sanitizes_unicode_filename(client, auth_headers):
    """A unicode name folds to ASCII and keeps its extension."""
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("résumé ☃.txt", b"x", "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    name = Path(r.json()["path"]).name
    assert name.isascii()
    assert name.endswith(".txt")


def test_route_rejects_oversized_file(client, auth_headers):
    """Past the non-image cap → 400 with a message that names the limit."""
    data = b"A" * (MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("huge.bin", data, "application/octet-stream")},
        headers=auth_headers,
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "maximum size" in detail
    assert str(MAX_FILE_SIZE_MB) in detail


def test_route_ignores_client_content_type(client, auth_headers):
    """A lying Content-Type changes nothing: type comes from the extension.

    Declared image/png on a .txt must NOT trigger the image contract, and
    must NOT be trusted to bypass it either.
    """
    r = client.post(
        "/api/v1/sessions/upload-file",
        files={"file": ("notes.txt", b"plain text", "image/png")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert Path(r.json()["path"]).suffix == ".txt"


def test_legacy_image_alias_still_routes(client, auth_headers):
    """The retained /upload-image path reaches the same handler.

    A PWA client holding a cached older api.js must not 404 on every paste
    until its service worker updates.
    """
    r = client.post(
        "/api/v1/sessions/upload-image",
        files={"file": ("notes.txt", b"data", "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
