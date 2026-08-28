"""Upload validation and session-scoped persistence helpers.

Pure helpers consumed by the ``POST /sessions/upload-file`` route (and its
retained ``/sessions/upload-image`` alias). Kept separate from ``routes.py``
so the validation contract (filename sanitisation, size caps, image
magic-byte cross-check) is unit-testable without spinning up the full
FastAPI app.

TWO CLASSES OF UPLOAD, ONE ENDPOINT. An upload whose sanitised extension is
in :data:`ALLOWED_EXTENSIONS` is treated as an IMAGE and keeps the original,
stricter contract: ``PIL.Image.verify()`` performs a structural pass over the
buffer and surfaces ``Image.format``, which is cross-referenced against the
declared extension, so a ``.png`` that is not a PNG is a 400. ``verify()`` is
deliberately structural-only (it does NOT decode pixels) - that's the
fast-path we want; treat any exception out of PIL as a 400. Anything else is
an ARBITRARY FILE: the bytes are never parsed, only the size is capped and
the name sanitised, because the point of the feature is to hand Claude a path
to something it can read for itself.

The client-supplied ``Content-Type`` is NOT trusted anywhere and never was;
it is logged and otherwise ignored. Type is inferred from the sanitised
extension only.

Storage layout: ``<working_dir>/.cloude_uploads/<uuid8>-<safe_name>``. The
uuid prefix makes collisions between two uploads of the same original name
practically impossible while the suffix preserves the name and extension the
user recognises, which is the whole reason a path is worth injecting. The
write still goes through ``O_EXCL`` so a collision FAILS rather than silently
clobbering an existing file. The directory is created with mode 0o700 and
files written with mode 0o600 so other local users can't enumerate or read
what's inside another session's upload bucket. The ``.cloude_uploads``
dotfile prefix keeps the bucket out of casual ``ls`` output and out of the
project's working tree when the session shells happen to inhabit a git repo.
"""

import os
import re
import unicodedata
import uuid
from io import BytesIO
from pathlib import Path, PureWindowsPath
from typing import Tuple

import structlog
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

logger = structlog.get_logger()

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

#: Longest sanitised basename we will write. Keeps the total path clear of
#: the per-component 255-byte limit on both APFS and ext4 once the 9-char
#: uuid prefix is added, and stops a pathological 4 KB filename from being
#: injected into the terminal.
MAX_BASENAME_LENGTH: int = 128

#: Characters kept verbatim in a sanitised basename. Everything else
#: collapses to an underscore. Deliberately conservative ASCII: the injected
#: string ends up in a shell prompt buffer, so the safe set is the set that
#: needs no quoting at all. Unicode is transliterated away rather than
#: preserved - a name Claude can open beats a name that looks pretty.
_SAFE_BASENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: Collapse runs of the replacement character so "a   b" is "a_b", not "a___b".
_REPEATED_UNDERSCORE = re.compile(r"_{2,}")

#: Fallback stem when sanitisation eats the entire name (a name that was
#: nothing but separators, or a pure-emoji filename).
_FALLBACK_BASENAME: str = "upload"

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


def sanitize_filename(declared_filename: str) -> str:
    """Reduce a client-supplied filename to a safe, injectable basename.

    NEVER trust the client's name. This strips every directory component
    (POSIX *and* Windows, so a smuggled ``..\\..\\evil`` cannot survive),
    normalises unicode to its ASCII skeleton, replaces anything outside
    ``[A-Za-z0-9._-]`` with an underscore, and refuses to emit a name that
    begins with a dot or a dash (a leading dot would hide the file from the
    sweeper's own ``ls``-style expectations; a leading dash would be read as
    an option flag by anything the user later pipes the path into).

    Traversal is defeated here by CONSTRUCTION - the result contains no
    separator at all - and then checked again for real in
    :func:`save_upload_to_session_dir`, which resolves the final path and
    asserts containment. Two independent guards, because this one is a
    string transform and string transforms are where assumptions hide.

    Args:
        declared_filename: Raw ``UploadFile.filename`` from the multipart
            body. May be empty, may be hostile.

    Returns:
        A non-empty basename of at most :data:`MAX_BASENAME_LENGTH`
        characters, containing only ``[A-Za-z0-9._-]``, never starting with
        ``.`` or ``-``. The extension is preserved when one survives.

    Example:
        >>> sanitize_filename("../../etc/passwd")
        'passwd'
        >>> sanitize_filename('my "quoted" report.pdf')
        'my_quoted_report.pdf'
    """
    raw = (declared_filename or "").strip()

    # Strip directory components under BOTH path flavours. PurePosixPath
    # alone leaves "..\\..\\evil.txt" fully intact on a POSIX server, which
    # is exactly the kind of thing a non-browser client would send.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    raw = PureWindowsPath(raw).name or raw

    # NFKD + ASCII fold turns "resumé.pdf" into "resume.pdf" rather
    # than into a pile of underscores.
    raw = unicodedata.normalize("NFKD", raw)
    raw = raw.encode("ascii", "ignore").decode("ascii")

    # Drop control characters explicitly before the class filter, so a
    # newline in a filename can never reach the terminal injection layer.
    raw = "".join(ch for ch in raw if ch.isprintable())

    safe = _SAFE_BASENAME_CHARS.sub("_", raw)
    safe = _REPEATED_UNDERSCORE.sub("_", safe).strip("_")

    # A name that was entirely "." / ".." / separators lands here empty.
    while safe.startswith(".") or safe.startswith("-"):
        safe = safe[1:]

    if not safe or safe in (".", ".."):
        return _FALLBACK_BASENAME

    if len(safe) > MAX_BASENAME_LENGTH:
        # Trim the STEM, never the extension - a path whose type Claude
        # cannot recognise is much less useful than a shortened name.
        stem, dot, ext = safe.rpartition(".")
        if dot and len(ext) <= 16:
            keep = max(1, MAX_BASENAME_LENGTH - len(ext) - 1)
            safe = f"{stem[:keep]}.{ext}"
        else:
            safe = safe[:MAX_BASENAME_LENGTH]

    return safe


def extension_of(safe_basename: str) -> str:
    """Lowercase extension of an already-sanitised basename, without the dot.

    Args:
        safe_basename: Output of :func:`sanitize_filename`.

    Returns:
        The extension, lowercased and dot-stripped, or ``""`` when the name
        carries none.
    """
    stem, dot, ext = safe_basename.rpartition(".")
    if not dot or not stem:
        return ""
    return ext.lower()


def validate_upload(
    data: bytes,
    declared_filename: str,
    max_image_size_mb: int,
    max_file_size_mb: int,
) -> Tuple[bytes, str]:
    """Validate any uploaded buffer and return it with a safe basename.

    Dispatches on the sanitised extension. An image extension keeps the
    original strict contract (:func:`validate_image`: size cap plus a
    magic-byte cross-check via PIL). Anything else is size-capped only and
    its bytes are never parsed.

    Args:
        data: Raw bytes from the multipart upload.
        declared_filename: Filename as declared by the client. Sanitised
            here; the raw value is never used for anything else.
        max_image_size_mb: Per-upload cap applied to image uploads.
        max_file_size_mb: Per-upload cap applied to non-image uploads.

    Returns:
        Tuple of ``(validated_bytes, safe_basename)``.

    Raises:
        HTTPException: 400 on an empty payload, an oversize buffer, or (for
            images only) a corrupt buffer or magic-byte mismatch.
    """
    safe_name = sanitize_filename(declared_filename)
    ext = extension_of(safe_name)

    if ext in ALLOWED_EXTENSIONS:
        validate_image(data, safe_name, max_image_size_mb)
        return data, safe_name

    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file payload")

    max_bytes = max_file_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds maximum size of {max_file_size_mb} MB",
        )

    return data, safe_name


def save_upload_to_session_dir(
    data: bytes, safe_basename: str, working_dir: str
) -> Path:
    """Persist a validated buffer into the session's upload bucket.

    Writes ``<working_dir>/.cloude_uploads/<uuid8>-<safe_basename>`` with
    ``O_EXCL`` so an existing file is NEVER silently overwritten, then chmods
    to 0o600. The bucket directory is created on demand with mode 0o700.

    Containment is re-asserted after ``resolve()`` in the same shape as
    ``config_files.resolve_safe_path()``: build the candidate, resolve it,
    and require ``relative_to(upload_dir)`` to succeed. ``safe_basename``
    already cannot contain a separator, so this is a second, independent
    guard rather than the only one.

    Args:
        data: Validated bytes.
        safe_basename: Output of :func:`sanitize_filename`.
        working_dir: Session working directory; user-facing path that may
            include ``~`` or relative segments.

    Returns:
        Absolute ``Path`` to the saved file.

    Raises:
        HTTPException: 400 if the resolved path escapes the bucket, 500 if
            the directory cannot be created or the file cannot be written.
    """
    try:
        base = Path(working_dir).expanduser().resolve()
        upload_dir = base / ".cloude_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        upload_dir = upload_dir.resolve()
    except OSError as exc:
        logger.error(
            "upload_dir_create_failed", working_dir=working_dir, error=str(exc)
        )
        raise HTTPException(
            status_code=500, detail="Failed to prepare upload directory"
        ) from exc

    target = (upload_dir / f"{uuid.uuid4().hex[:8]}-{safe_basename}").resolve()
    try:
        target.relative_to(upload_dir)
    except ValueError:
        logger.error(
            "upload_path_escaped_bucket",
            target=str(target),
            upload_dir=str(upload_dir),
        )
        raise HTTPException(status_code=400, detail="Invalid upload filename")

    try:
        # O_EXCL is the no-silent-overwrite guarantee. The uuid prefix makes
        # a collision practically impossible; if one happens anyway we want
        # a loud 500, not a clobbered file.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        target.chmod(0o600)
    except FileExistsError as exc:
        logger.error("upload_target_exists", target=str(target))
        raise HTTPException(
            status_code=409, detail="Upload target already exists"
        ) from exc
    except OSError as exc:
        logger.error("upload_write_failed", target=str(target), error=str(exc))
        raise HTTPException(
            status_code=500, detail="Failed to write uploaded file"
        ) from exc

    return target


def validate_image(
    data: bytes, declared_filename: str, max_size_mb: int
) -> Tuple[bytes, str]:
    """Validate an uploaded image buffer against the allowlist + size cap.

    Args:
        data: Raw image bytes from the multipart upload.
        declared_filename: Filename as declared by the client. Only the
            extension is consulted - the basename is discarded by the caller.
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
