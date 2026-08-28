"""
File CREATION for the file editor.

Split from ``src/core/config_files.py`` (which owns list/read/write and the
allowed-roots / hide-list constants) purely for this project's 500-line
file rule; the two are one feature and share every guard.

WHAT THIS DOES NOT DO: it does not create DIRECTORIES. Creating a file
inside an existing directory is the whole scope. ``mkdir -p`` on a
client-supplied path is a second, larger decision - it can materialise a
tree the tree view never showed, it interacts with the allow-list in ways
``resolve_safe_path`` was not written to answer, and there is no undo.
Asking for ``skills/newthing/SKILL.md`` under a root with no ``newthing/``
therefore FAILS with a message that says exactly that, rather than half
working. If directory creation is wanted later it belongs here, as its own
function with its own tests.

SECURITY: the create path goes through ``config_files.resolve_safe_path()``
- the same and only path guard the read and write paths use. There is no
second, more permissive resolver here. That function rejects absolute
paths, ``..`` components, hidden/state entries, anything that escapes the
root after ``resolve()``, and (for the allow-listed roots) a top-level name
outside the allow-list.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import structlog

from src.core import config_files
from src.core.config_files import ConfigFileError

logger = structlog.get_logger()


def create_file(
    root_id: str,
    rel_path: str,
    content: str,
    project_path: Optional[str],
    acknowledge_executable: bool = False,
    acknowledge_sensitive: bool = False,
) -> dict:
    """
    Description: create ONE new file under a browsable root. Refuses to
      overwrite: if anything already exists at the resolved path - file,
      directory or symlink - the call fails and nothing is touched. The
      parent directory must already exist; this function never creates
      one (see the module docstring). Everything else matches
      ``config_files.write_file``: ``.json`` content is parsed before it
      touches disk, an executable destination (hooks/, scripts/) requires
      ``acknowledge_executable``, a credentials/secret/key-shaped name
      requires ``acknowledge_sensitive``, a read-only root is refused, and
      the write itself is atomic.
    Inputs:
      root_id (str) - "user", "project", or "workdir".
      rel_path (str) - forward-slash relative path for the NEW file.
      content (str) - initial contents; "" is allowed.
      project_path (str|None) - required when root_id != "user".
      acknowledge_executable (bool) - must be True when the new file would
        be classified executable.
      acknowledge_sensitive (bool) - must be True when the new file's name
        is credentials/secret/key-shaped.
    Output: dict - {"created": True, "rel_path": str, "is_executable":
      bool, "is_sensitive": bool}.
    Raises: ConfigFileError - every client-caused failure: invalid or
      escaping path (via ``resolve_safe_path``), an empty path, a path
      naming an existing entry, a missing parent directory, a read-only
      root, malformed JSON, or a missing acknowledgement. The routes layer
      maps this to HTTP 400.
    Example: create_file("user", "notes.md", "# hi", None)
      -> {"created": True, "rel_path": "notes.md", ...}
    """
    if not (rel_path or "").strip().strip("/"):
        raise ConfigFileError("a file name is required")

    path = config_files.resolve_safe_path(root_id, rel_path, project_path)
    roots = config_files.resolve_roots(project_path)
    root = roots[root_id]

    if path == root:
        raise ConfigFileError("a file name is required")
    # lexists, not exists: a broken symlink is still something occupying
    # this name, and silently replacing it is exactly the overwrite this
    # function refuses to do.
    if path.exists() or path.is_symlink():
        raise ConfigFileError(f"'{rel_path}' already exists - open it instead of creating it")

    parent = path.parent
    if not parent.is_dir():
        raise ConfigFileError(
            f"'{_rel(root, parent)}' does not exist - create the file in an "
            "existing directory (this editor does not make directories)"
        )

    is_executable, is_sensitive, read_only = config_files.classify(root, path)
    if read_only:
        raise ConfigFileError("this location is under a read-only root (plugins/)")
    if is_executable and not acknowledge_executable:
        raise ConfigFileError(
            "a file here is executed automatically by claude code - "
            "create refused without explicit confirmation"
        )
    if is_sensitive and not acknowledge_sensitive:
        raise ConfigFileError(
            "this name looks like credentials/a secret/a private key - "
            "create refused without explicit confirmation"
        )

    if path.suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigFileError(f"invalid json: {exc.msg} (line {exc.lineno}, col {exc.colno})")

    try:
        config_files.atomic_write(path, content)
    except OSError as exc:
        logger.warning("config_files_create_failed", root=root_id, rel_path=rel_path, error=str(exc))
        raise ConfigFileError(f"could not create the file: {exc.strerror or exc}")

    logger.info(
        "config_files_create",
        root=root_id,
        rel_path=rel_path,
        is_executable=is_executable,
        is_sensitive=is_sensitive,
        bytes=len(content),
    )
    return {
        "created": True,
        "rel_path": rel_path.strip().strip("/"),
        "is_executable": is_executable,
        "is_sensitive": is_sensitive,
    }


def _rel(root: Path, path: Path) -> str:
    """
    Description: render a path relative to its root for an error message,
      so a rejection never leaks an absolute filesystem path back to the
      client.
    Inputs: root (Path); path (Path).
    Output: str - forward-slash relative path, or "." for the root itself.
    """
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.name
