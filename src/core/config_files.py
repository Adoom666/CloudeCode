"""
Claude-config + project file tree / editor - server-side business logic.

Three roots:

    - "user"    ``~/.claude``               (CLAUDE_HOME, reused from
                                              ``slash_command_discovery`` -
                                              single source of truth for that
                                              path rather than a second
                                              hardcoded ``Path.home()``)
    - "project" ``<working_dir>/.claude``    (the active session's config,
                                              same allow-list model as "user")
    - "workdir" ``<working_dir>``            (the active session's WORKING
                                              DIRECTORY itself - general
                                              project file browsing, added
                                              2026-08. No allow-list: any
                                              non-hidden entry is browsable.)

``project`` and ``workdir`` are both derived from the same client-supplied
``project_path`` (the session's working directory) - there is no server-side
"current project" state; the caller must have gotten this path from a real
session, never trust it as arbitrary free text.

ALLOWED_TOP_LEVEL_FILES / ALLOWED_TOP_LEVEL_DIRS / HIDE_NAMES /
HIDE_PREFIXES / SENSITIVE_NAMES / SENSITIVE_PREFIXES / SENSITIVE_SUFFIXES
below are the single source of truth for what this feature shows and how.
Every list/read/write call re-derives its answer from these constants -
nothing is duplicated in the routes layer.

Security posture (the client is assumed hostile, per the API layer):
    - every relative path is resolved with ``Path.resolve()`` and checked
      for containment inside the resolved root before any filesystem call
      touches it (blocks ``../`` traversal AND symlink escapes, since
      resolve() follows symlinks). This applies identically to all three
      roots, including "workdir" - dropping the allow-list for general
      project browsing does NOT relax the containment check.
    - "user" and "project" additionally enforce an allow-list of top-level
      names (this app's own config surface, not a general browser).
      "workdir" has no allow-list - it is a real project directory - but
      the hide-list (HIDE_NAMES / HIDE_PREFIXES) still applies, and applies
      MORE, not less: ``.git`` internals, and known-huge dependency/build
      directories (node_modules, venv, .venv, dist, build, __pycache__) are
      never listed and never resolvable by a guessed path either.
    - files matching SENSITIVE_* (``.env*``, ``.credentials.json``,
      ``id_rsa``, ``id_ed25519``, ``*.pem``, ``*.key``) are NOT refused -
      this app already grants a full remote shell, so a hard refusal here
      would be inconsistent theater, not real access control. They ARE
      flagged ``is_sensitive`` in every tree/read/write response so the
      client can mask on-screen display until the user explicitly reveals,
      and a write to one requires ``acknowledge_sensitive=True`` the same
      way an executable write requires ``acknowledge_executable=True``.
      Content is never logged for any file, sensitive or not.
    - every write is backed up first (``<name>.bak``, one generation,
      same convention as ``Settings.update_settings_config()``), and a
      ``.json`` write is parsed before it touches disk - a parse failure
      raises and nothing is written.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Optional

import structlog

from src.core.slash_command_discovery import CLAUDE_HOME
from src.core.config_files_constants import (
    ALLOWED_TOP_LEVEL_FILES,
    ALLOWED_TOP_LEVEL_DIRS,
    READONLY_COLLAPSED_DIRS,
    BLOCKLIST_ONLY_ROOTS,
    HIDE_NAMES,
    HIDE_PREFIXES,
    MAX_READ_BYTES,
)
# classify/atomic_write/is_sensitive_name live in config_files_io so this
# module and config_files_create share ONE implementation of each - see
# that module's docstring.
from src.core.config_files_io import atomic_write, classify, is_sensitive_name

logger = structlog.get_logger()


class ConfigFileError(ValueError):
    """Raised for any client-caused failure: bad root, disallowed path,
    traversal attempt, hidden file, or oversized read. Routes layer maps
    this to HTTP 400/403 - never a 500, since these are all "the request
    was invalid", not server faults."""


class ConfigFileUnreadableError(ConfigFileError):
    """Raised when a ROOT exists but its own contents could not be
    enumerated (``OSError`` from ``iterdir()`` - typically a permissions
    problem, not a bad request). This is the THREE-OUTCOME RULE's third
    state for this endpoint: distinct from "listed, zero entries" (a
    genuinely empty/absent directory) and from an ordinary
    ``ConfigFileError`` (client sent something invalid). Routes layer
    maps this to HTTP 503 rather than 400, so the client can render "I
    could not check this" instead of silently treating it as "there is
    nothing here" - see config_files.py's module docstring and
    CLAUDE.md's THREE-OUTCOME RULE section for why that conflation is a
    named, recurring bug class in this project.

    A subdirectory (not the root itself) hitting the same OSError does
    NOT raise this - it would abort the whole tree over one bad
    subdirectory. That case sets ``TreeNode.list_error`` instead, so the
    rest of the tree still renders and only that one node says it
    could not be evaluated.
    """


@dataclass
class TreeNode:
    """One entry in the rendered file tree.

    Inputs (fields): name (str) - basename; rel_path (str) - path
      relative to the root, forward-slash separated; is_dir (bool);
      is_executable (bool) - True for a .py/.cjs/.js/.sh file under
      hooks/ or scripts/; is_sensitive (bool) - True for a
      credentials/secret/key-shaped file (see SENSITIVE_*); read_only
      (bool) - True for anything under a READONLY_COLLAPSED_DIRS root;
      collapsed (bool) - hint to the frontend to render this subtree
      closed by default; children (list[TreeNode]) - populated for
      directories; list_error (str|None) - set instead of an empty
      `children` when THIS directory's own entries could not be
      enumerated (OSError) - the three-outcome rule's "could not
      evaluate", kept distinct from a directory that was read
      successfully and is simply empty.
    """
    name: str
    rel_path: str
    is_dir: bool
    is_executable: bool = False
    is_sensitive: bool = False
    read_only: bool = False
    collapsed: bool = False
    children: list = field(default_factory=list)
    list_error: Optional[str] = None


def _is_hidden(name: str) -> bool:
    """
    Description: True if a filesystem entry name matches the
      never-shown, never-resolvable hide-list (exact name, or a state
      directory/VCS-internal/huge-dependency-tree name).
    Inputs: name (str) - a single path component (basename).
    Output: bool.
    Example: _is_hidden("node_modules") -> True
    """
    if name in HIDE_NAMES:
        return True
    return any(name.startswith(p) for p in HIDE_PREFIXES)


def resolve_roots(project_path: Optional[str]) -> dict:
    """
    Description: resolve the "user" root always, and the "project" /
      "workdir" roots only when a working project_path is given.
      "project" additionally requires that ``.claude`` exist under it;
      "workdir" only requires the working directory itself to exist (a
      session always has one).
    Inputs: project_path (str|None) - absolute path to a project's
      working directory, as tracked by the session (not client-supplied
      free text used directly - callers must have gotten this from a
      real session, never trust it as arbitrary).
    Output: dict[str, Path] - keyed "user" (always present), "project"
      and/or "workdir" (present only when applicable).
    """
    roots = {"user": CLAUDE_HOME.resolve()}
    if project_path:
        claude_candidate = Path(project_path) / ".claude"
        if claude_candidate.is_dir():
            roots["project"] = claude_candidate.resolve()
        workdir_candidate = Path(project_path)
        if workdir_candidate.is_dir():
            roots["workdir"] = workdir_candidate.resolve()
    return roots


def resolve_safe_path(root_id: str, rel_path: str, project_path: Optional[str]) -> Path:
    """
    Description: turn a client-supplied (root_id, rel_path) pair into a
      verified-contained absolute Path. This is the ONE function every
      read/write call must go through - the security boundary for path
      traversal and the hide-list both live here, not just in what
      list_tree() chooses to enumerate. The allow-list check is skipped
      for BLOCKLIST_ONLY_ROOTS ("workdir"); containment + hide-list are
      NOT skipped for any root.
    Inputs:
      root_id (str) - "user", "project", or "workdir".
      rel_path (str) - forward-slash relative path from the tree listing;
        may be "" for the root itself.
      project_path (str|None) - required when root_id != "user".
    Output: Path - resolved, absolute, verified inside the root.
    Raises: ConfigFileError - unknown root, empty rel_path components
      ("..", absolute path smuggled in, etc.), path escapes the root
      after resolve(), any path component is hidden, or (for an
      allow-listed root's non-empty rel_path) the top-level component
      isn't in ALLOWED_TOP_LEVEL_FILES/ALLOWED_TOP_LEVEL_DIRS.
    Example: resolve_safe_path("user", "hooks/op-readonly-guard.py", None)
      -> Path("/Users/x/.claude/hooks/op-readonly-guard.py")
    """
    roots = resolve_roots(project_path)
    root = roots.get(root_id)
    if root is None:
        raise ConfigFileError(f"unknown or unavailable root: {root_id}")

    raw = (rel_path or "").strip()
    # Reject an absolute path outright rather than silently normalizing it
    # to relative - for an allow-listed root ("user"/"project") the
    # allow-list check below would catch most of these anyway, but
    # "workdir" has no allow-list, so this is the ONLY thing standing
    # between a client-sent "/etc/passwd" and it quietly being treated as
    # a relative "etc/passwd" under the project root. PureWindowsPath
    # covers a smuggled "C:\..." / "\\host\share" form too, in case a
    # non-POSIX client is ever added.
    if raw.startswith("/") or Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ConfigFileError("absolute paths are not allowed")

    rel_path = raw.strip("/")
    if rel_path in ("", "."):
        return root

    parts = rel_path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ConfigFileError("invalid path")

    if root_id not in BLOCKLIST_ONLY_ROOTS:
        top = parts[0]
        if top not in ALLOWED_TOP_LEVEL_FILES and top not in ALLOWED_TOP_LEVEL_DIRS and top not in READONLY_COLLAPSED_DIRS:
            raise ConfigFileError(f"'{top}' is not an allowed claude-config entry")

    for p in parts:
        if _is_hidden(p):
            raise ConfigFileError("path refers to a hidden/state entry, not a browsable file")

    candidate = (root / Path(*parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ConfigFileError("path escapes the allowed root")

    return candidate


def _build_node(root: Path, path: Path, depth: int, max_depth: int, root_id: str) -> Optional[TreeNode]:
    """
    Description: recursively build one TreeNode for a path, applying the
      hide-list and, for allow-listed roots' depth-0 direct children, the
      allow-list. Depth-limited so a pathological directory (or a
      surprise symlink loop) can't hang a request.
    Inputs: root (Path); path (Path); depth (int) - current recursion
      depth; max_depth (int) - hard cap; root_id (str) - "user",
      "project", or "workdir"; controls whether the allow-list gate
      applies at depth 0.
    Output: TreeNode|None - None if this path should not be shown at all.
    """
    name = path.name
    if _is_hidden(name):
        return None
    if depth == 0 and root_id not in BLOCKLIST_ONLY_ROOTS:
        if path.is_dir() and name not in ALLOWED_TOP_LEVEL_DIRS and name not in READONLY_COLLAPSED_DIRS:
            return None
        if path.is_file() and name not in ALLOWED_TOP_LEVEL_FILES:
            return None

    is_executable, is_sensitive, read_only = classify(root, path)
    rel = str(path.relative_to(root)).replace(os.sep, "/")
    collapsed = read_only  # plugins/ (and any future read-only root) starts collapsed.
    node = TreeNode(
        name=name,
        rel_path=rel,
        is_dir=path.is_dir(),
        is_executable=is_executable,
        is_sensitive=is_sensitive,
        read_only=read_only,
        collapsed=collapsed,
    )

    if path.is_dir() and depth < max_depth:
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            # THREE-OUTCOME RULE: this directory could not be evaluated -
            # do NOT leave it looking like a successfully-listed empty
            # directory (that reads as "nothing here" when the truth is
            # "I could not check"). Mark it and keep going; one unreadable
            # subdirectory must not abort the rest of the tree.
            logger.warning("config_files_listdir_failed", path=str(path), error=str(exc))
            node.list_error = exc.strerror or str(exc)
            entries = []
        for child in entries:
            child_node = _build_node(root, child, depth + 1, max_depth, root_id)
            if child_node is not None:
                node.children.append(child_node)

    return node


def list_tree(root_id: str, project_path: Optional[str], max_depth: int = 6) -> list:
    """
    Description: build the full hide-list-filtered file tree for one
      root ("user"/"project" are additionally allow-listed; "workdir" is
      not).
    Inputs:
      root_id (str) - "user", "project", or "workdir".
      project_path (str|None) - required for "project"/"workdir".
      max_depth (int) - recursion cap (default 6 - deep enough for
        skills/<name>/SKILL.md, shallow enough to bound one request).
    Output: list[dict] - top-level TreeNode entries, JSON-serializable
      (dataclasses.asdict shape). For allow-listed roots the order is
      ALLOWED_TOP_LEVEL_FILES then ALLOWED_TOP_LEVEL_DIRS then
      READONLY_COLLAPSED_DIRS, each sorted alphabetically within its
      group; "workdir" is files-then-dirs, alphabetical.
    Raises:
      ConfigFileError - unknown/unavailable root (client-caused; the
        working directory/root simply doesn't apply here).
      ConfigFileUnreadableError - the root exists but its own entries
        could not be enumerated (OSError - typically permissions). This
        is the three-outcome rule's third state: NOT the same as "root
        exists, zero entries", which returns an empty list normally.
    """
    roots = resolve_roots(project_path)
    root = roots.get(root_id)
    if root is None:
        raise ConfigFileError(f"unknown or unavailable root: {root_id}")
    if not root.is_dir():
        return []

    nodes = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        logger.warning("config_files_list_root_failed", path=str(root), error=str(exc))
        raise ConfigFileUnreadableError(
            f"{root_id} root could not be read: {exc.strerror or exc}"
        ) from exc
    for entry in entries:
        node = _build_node(root, entry, depth=0, max_depth=max_depth, root_id=root_id)
        if node is not None:
            nodes.append(node)

    def _to_dict(n: TreeNode) -> dict:
        return {
            "name": n.name,
            "rel_path": n.rel_path,
            "is_dir": n.is_dir,
            "is_executable": n.is_executable,
            "is_sensitive": n.is_sensitive,
            "read_only": n.read_only,
            "collapsed": n.collapsed,
            "children": [_to_dict(c) for c in n.children],
            "list_error": n.list_error,
        }

    return [_to_dict(n) for n in nodes]


def read_file(root_id: str, rel_path: str, project_path: Optional[str]) -> dict:
    """
    Description: read one browsable file's contents. Sensitive files
      (see SENSITIVE_*) are NOT refused - the caller (routes/client) is
      responsible for masking them on screen until the user explicitly
      reveals; this function only flags `is_sensitive` in the response.
    Inputs: root_id, rel_path, project_path - see resolve_safe_path().
    Output: dict - {"content": str, "is_executable": bool,
      "is_sensitive": bool, "read_only": bool, "size": int}.
    Raises: ConfigFileError - invalid path (via resolve_safe_path), not a
      file, too large, or a decode error (binary file).
    Security: caller (routes layer) must never log the returned content,
      sensitive or not.
    """
    roots = resolve_roots(project_path)
    root = roots[root_id] if root_id in roots else None
    path = resolve_safe_path(root_id, rel_path, project_path)
    if not path.is_file():
        raise ConfigFileError("not a file")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ConfigFileError(f"file too large to edit here ({size} bytes)")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigFileError("file is not text/utf-8 - not editable here")

    is_executable, is_sensitive, read_only = classify(root or path.parent, path)
    return {
        "content": content,
        "is_executable": is_executable,
        "is_sensitive": is_sensitive,
        "read_only": read_only,
        "size": size,
    }


def write_file(
    root_id: str,
    rel_path: str,
    content: str,
    project_path: Optional[str],
    acknowledge_executable: bool = False,
    acknowledge_sensitive: bool = False,
) -> dict:
    """
    Description: write one browsable file, always backing up the
      previous bytes first (atomic tmp-file + fsync + os.replace, same
      convention as ``Settings.update_settings_config()``).
      ``.json``-suffixed files are parsed with ``json.loads`` before
      anything touches disk - a parse failure raises and nothing is
      written (and the exception message never includes file content,
      only json.JSONDecodeError's own line/col/msg). A write to an
      executable file (see EXECUTABLE_DIRS / EXECUTABLE_EXTENSIONS)
      additionally REQUIRES ``acknowledge_executable=True``, and a write
      to a sensitive file (see SENSITIVE_*) additionally REQUIRES
      ``acknowledge_sensitive=True`` - the routes layer only sets either
      flag after the client's confirm modal returned true; this is the
      server-side half of that confirmation, not just a UI nicety.
    Inputs:
      root_id (str); rel_path (str); project_path (str|None) - see
        resolve_safe_path().
      content (str) - new file contents.
      acknowledge_executable (bool) - must be True to write an
        executable file; ignored (no effect) for non-executable files.
      acknowledge_sensitive (bool) - must be True to write a sensitive
        (credentials/secret/key-shaped) file; ignored for others.
    Output: dict - {"backed_up": bool, "is_executable": bool,
      "is_sensitive": bool}.
    Raises: ConfigFileError - invalid/read-only path, malformed JSON for
      a .json file, an executable write without acknowledgement, or a
      sensitive write without acknowledgement.
    """
    path = resolve_safe_path(root_id, rel_path, project_path)
    roots = resolve_roots(project_path)
    root = roots[root_id]

    is_executable, is_sensitive, read_only = classify(root, path)
    if read_only:
        raise ConfigFileError("this file is under a read-only root (plugins/)")
    if is_executable and not acknowledge_executable:
        raise ConfigFileError(
            "this file is executed automatically by claude code - "
            "write refused without explicit confirmation"
        )
    if is_sensitive and not acknowledge_sensitive:
        raise ConfigFileError(
            "this file looks like credentials/a secret/a private key - "
            "write refused without explicit confirmation"
        )

    if path.suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigFileError(f"invalid json: {exc.msg} (line {exc.lineno}, col {exc.colno})")

    backed_up = False
    if path.exists():
        try:
            backup_path = path.with_suffix(path.suffix + ".bak")
            backup_path.write_bytes(path.read_bytes())
            backed_up = True
        except OSError as exc:
            logger.warning("config_files_backup_failed", path=str(path), error=str(exc))

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)

    logger.info(
        "config_files_write",
        root=root_id,
        rel_path=rel_path,
        is_executable=is_executable,
        is_sensitive=is_sensitive,
        backed_up=backed_up,
        bytes=len(content),
    )
    return {"backed_up": backed_up, "is_executable": is_executable, "is_sensitive": is_sensitive}
