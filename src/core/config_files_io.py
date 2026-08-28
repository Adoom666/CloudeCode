"""
Shared primitives for the file-editor modules.

Two things that ``config_files`` (list/read/write) and
``config_files_create`` (create) both need, factored out so there is one
implementation of each rather than two that can drift:

    - ``classify()``    - is this path executable / sensitive / read-only
    - ``atomic_write()`` - durable write with no half-written window

Extracted when adding file creation pushed ``config_files.py`` past this
project's 500-line rule. No behaviour changed in the move.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.core.test_write_guard import assert_test_write_allowed
from src.core.config_files_constants import (
    READONLY_COLLAPSED_DIRS,
    SENSITIVE_NAMES,
    SENSITIVE_PREFIXES,
    SENSITIVE_SUFFIXES,
    EXECUTABLE_EXTENSIONS,
    EXECUTABLE_DIRS,
)


def is_sensitive_name(name: str) -> bool:
    """
    Description: True if a filesystem entry name is credentials/secret/
      key-shaped (``.env*``, ``.credentials.json``, ``id_rsa``,
      ``id_ed25519``, ``*.pem``, ``*.key``). Sensitive entries ARE shown
      and ARE resolvable - this only drives on-screen masking, the
      reveal confirmation, and the write/create acknowledgement, never
      access refusal.
    Inputs: name (str) - a single path component (basename).
    Output: bool.
    Example: is_sensitive_name(".env.local") -> True
    """
    if name in SENSITIVE_NAMES:
        return True
    if any(name.startswith(p) for p in SENSITIVE_PREFIXES):
        return True
    return any(name.endswith(s) for s in SENSITIVE_SUFFIXES)


def classify(root: Path, path: Path) -> tuple[bool, bool, bool]:
    """
    Description: compute (is_executable, is_sensitive, read_only) for one
      path relative to its root. The SINGLE source of these three
      judgements - read, write and create all call this, so a file cannot
      be treated as ordinary by one path and executable by another.

      Note ``is_executable`` and ``is_sensitive`` are False for a path
      that does not exist yet in the ``path.is_file()`` sense; creation
      therefore evaluates them on NAME and LOCATION only, which is
      deliberate - a hook that does not exist yet is still going to run
      once it does.
    Inputs: root (Path); path (Path) - must be inside root.
    Output: tuple[bool, bool, bool].
    """
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = ()
    top = rel_parts[0] if rel_parts else ""
    read_only = top in READONLY_COLLAPSED_DIRS
    in_executable_dir = any(part in EXECUTABLE_DIRS for part in rel_parts[:-1])
    is_executable = path.suffix in EXECUTABLE_EXTENSIONS and in_executable_dir
    is_sensitive = is_sensitive_name(path.name)
    if path.is_dir():
        is_executable = False
        is_sensitive = False
    return is_executable, is_sensitive, read_only


def atomic_write(path: Path, content: str) -> None:
    """
    Description: write text to ``path`` atomically - tmp file, flush,
      fsync, ``os.replace`` - so a crash mid-write can never leave a
      half-written config behind.
    Inputs: path (Path) - destination, already resolved and verified;
      content (str) - the full new contents.
    Output: None.
    Raises: OSError - propagated from the filesystem; callers translate.
    """
    # Same blast-radius control as src/core/claude_hooks.py. This is the
    # single chokepoint for every config-file write in the editor, and
    # the "user" root it serves is literally ``Path.home()/".claude"``
    # (config_files.py -> slash_command_discovery.CLAUDE_HOME) with NO
    # env override of any kind. That is the identical shape as the
    # ensure_hook_settings defect: a test that reaches this function
    # without a redirect writes into the developer's real ~/.claude.
    # Inert in production.
    assert_test_write_allowed(path)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
