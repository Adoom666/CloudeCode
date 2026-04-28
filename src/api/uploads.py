"""Image upload validation and session-scoped persistence helpers.

Pure helpers consumed by the ``POST /sessions/upload-image`` route. Kept
separate from ``routes.py`` so the validation contract (extension allowlist,
magic-byte cross-check, size cap) is unit-testable without spinning up the
full FastAPI app.

Validation strategy: declared-extension allowlist screens out obviously
non-image filenames before any byte parsing, then ``PIL.Image.verify()``
performs a structural pass over the buffer and surfaces ``Image.format``
which is cross-referenced against the declared extension. ``verify()`` is
deliberately structural-only (it does NOT decode pixels) — that's the
fast-path we want; treat any exception out of PIL as a 400.

Storage layout: ``<working_dir>/.cloude_uploads/<uuid>.<ext>``. The
directory is created with mode 0o700 and files written with mode 0o600
so other local users can't enumerate or read what's inside another
session's upload bucket. The ``.cloude_uploads`` dotfile prefix keeps
the bucket out of casual ``ls`` output and out of the project's working
tree when the session shells happen to inhabit a git repo.
"""

import uuid
from io import BytesIO
from pathlib import Path
from typing import Tuple

import structlog
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

logger = structlog.get_logger()

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# PIL.Image.format values keyed by the declared extension. ``jpg`` and
# ``jpeg`` both decode as ``JPEG`` in Pillow, so the comparison key is the
# normalized format string, not the user-facing extension.
_EXT_TO_PIL_FORMAT: dict[str, str] = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "gif": "GIF",
    "webp": "WEBP",
}


def validate_image(
    data: bytes, declared_filename: str, max_size_mb: int
) -> Tuple[bytes, str]:
    """Validate an uploaded image buffer against the allowlist + size cap.

    Args:
        data: Raw image bytes from the multipart upload.
        declared_filename: Filename as declared by the client. Only the
            extension is consulted — the basename is discarded by the caller.
        max_size_mb: Per-upload size cap in megabytes.

    Returns:
        Tuple of ``(validated_bytes, normalized_ext)`` where
        ``normalized_ext`` is lowercased and stripped of the leading dot.
        ``jpg`` is preserved as ``jpg`` (not coerced to ``jpeg``) so the
        on-disk filename matches what the client asked for; format-level
        normalization happens internally for the magic-byte comparison only.

    Raises:
        HTTPException: 400 on missing/disallowed extension, oversize buffer,
            or magic-byte mismatch.
    """
    if not declared_filename or "." not in declared_filename:
        raise HTTPException(status_code=400, detail="Filename must include an extension")

    ext = declared_filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image extension '.{ext}'. Allowed: png, jpg, jpeg, gif, webp",
        )

    max_bytes = max_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"Image exceeds maximum size of {max_size_mb} MB",
        )

    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty image payload")

    expected_format = _EXT_TO_PIL_FORMAT[ext]
    try:
        with Image.open(BytesIO(data)) as img:
            actual_format = img.format
            img.verify()
    except (UnidentifiedImageError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid or corrupted image: {exc}",
        ) from exc

    if actual_format != expected_format:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Image content does not match declared extension '.{ext}' "
                f"(detected {actual_format})"
            ),
        )

    return data, ext


def save_to_session_dir(data: bytes, ext: str, working_dir: str) -> Path:
    """Persist validated image bytes into the session's upload bucket.

    Creates ``<working_dir>/.cloude_uploads/`` on demand with mode 0o700,
    writes ``<uuid>.<ext>`` inside it, then chmods the file to 0o600. The
    UUID hex filename is alphanumerics + a single dot, which sidesteps any
    tmux ``send-keys -l`` quoting concerns at the injection layer.

    Args:
        data: Validated image bytes.
        ext: Lowercase extension without leading dot.
        working_dir: Session working directory; user-facing path that may
            include ``~`` or relative segments.

    Returns:
        Absolute ``Path`` to the saved file.

    Raises:
        HTTPException: 500 if the directory cannot be created or the file
            cannot be written.
    """
    try:
        base = Path(working_dir).expanduser().resolve()
        upload_dir = base / ".cloude_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.error(
            "upload_dir_create_failed",
            working_dir=working_dir,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to prepare upload directory",
        ) from exc

    target = upload_dir / f"{uuid.uuid4().hex}.{ext}"
    try:
        target.write_bytes(data)
        target.chmod(0o600)
    except OSError as exc:
        logger.error(
            "upload_write_failed",
            target=str(target),
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to write uploaded image",
        ) from exc

    return target
