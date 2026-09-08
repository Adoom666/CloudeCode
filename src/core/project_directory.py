"""Compose and validate the directory a NEW project is created in.

WHY THIS MODULE EXISTS. The "start empty" flow had no folder step at all.
The client posted a project name and nothing else, so
``SessionManager.create_session`` fell through to its last-resort branch
(``work_path = settings.get_working_dir() / session_id``) and the project
called "Punchlist Test" was born at ``<projects root>/ses_5a756046`` - a
random session id the user never chose and cannot recognise, written into
``sessions.working_dir`` as the project's permanent home. Worse, that path
was built with ``Path.expanduser()``, which expands ``~`` but does NOT
resolve symlinks, so on this machine it stored the SHORT spelling
``/Users/jsugamele/Development/...`` for a directory that really lives in
iCloud. See gotcha 6 in CLAUDE.md: two spellings of one directory split a
session in two.

Both defects are fixed here, on the server, because the server is the only
place that can be trusted about the filesystem.

THE FIELD IS NEW ON PURPOSE. This validates ``project_parent_dir``, which
did not exist before, and never ``working_dir``. Three shipped flows post
``working_dir`` with a folder chosen from anywhere on disk - "open an
existing folder", the new-console FAB (it posts ``"~"``), and clone - so
attaching a root restriction to that field would refuse folders those
flows have always accepted. A restriction on a field nothing has ever sent
cannot regress anything.

TRAVERSAL AND SYMLINK ESCAPE ARE ONE CHECK, NOT TWO. The parent is passed
through ``os.path.realpath`` BEFORE anything is compared: ``..`` cannot
survive that, and a symlink pointing out of the allowed roots resolves to
where it really points and is refused there. The comparison is
component-wise, never ``str.startswith``, so ``/Users/jsugamelevil`` is not
read as living under ``/Users/jsugamele``.

THE ALLOWED ROOTS ARE DELIBERATELY GENEROUS. The owner's instruction was
"make sure new projects can select proper folder", and his projects live
under the iCloud Sync path rather than under ``DEFAULT_WORKING_DIR``. A
root set of only the projects root would refuse the exact folders the
feature exists to offer, which is a silent way of not shipping it. Home is
the boundary that admits every folder a user would name while still
refusing ``/etc``, ``/System``, ``/usr`` and ``/``.

Resolution is PURE - it creates nothing. ``ensure_project_directory`` is
the single side-effecting function, called only after a verdict says ok.

Example:
    >>> v = resolve_project_directory('/Users/me/code', 'My Project')
    >>> v.ok, v.path
    (True, '/Users/me/code/My Project')
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import structlog

logger = structlog.get_logger()

#: Longest single path component macOS will accept (NAME_MAX), in bytes.
#: A name over this fails at ``mkdir`` with a bare OSError, so it is
#: refused up front with a sentence a human can act on instead.
MAX_NAME_BYTES = 255

#: Characters that may never appear in a project directory NAME. ``/`` is
#: the path separator, ``\\`` is a separator on the other platform and a
#: reliable source of confusion on this one, and NUL terminates the string
#: the kernel actually receives. Everything else a user types - spaces
#: very much included, since the owner's own projects have them - is used
#: verbatim.
ILLEGAL_NAME_CHARS = ("/", "\\", "\x00")

#: Names that are not names. ``.`` and ``..`` address existing
#: directories, and a leading dot makes a project invisible to every tool
#: the user would reach for next.
RESERVED_NAMES = (".", "..")

#: Fallback projects root, used ONLY when ``Settings`` cannot say where
#: projects go. It is the LONG iCloud spelling on purpose: the short
#: ``~/Development`` is a symlink into this path, and storing the short
#: form is the bug this module exists to stop. Named here rather than
#: written inline in a handler so there is exactly one copy of it.
FALLBACK_PROJECTS_ROOT = (
    "/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs"
    "/Sync/Development"
)


@dataclass(frozen=True)
class ProjectDirectoryVerdict:
    """The outcome of resolving a project directory, named rather than boolean.

    Attributes:
        ok: True only when ``path`` is safe to create and use.
        path: The absolute, symlink-resolved directory, or None when refused.
        code: One of ``ok``, ``illegal_name``, ``parent_missing``,
            ``parent_not_dir``, ``outside_allowed_roots``, ``exists_not_dir``,
            ``exists_non_empty``, ``cannot_determine``.
        message: Lowercase plain-English sentence, safe to show a user.
    """

    ok: bool
    path: Optional[str]
    code: str
    message: str


def _real(path: str) -> str:
    """Canonicalise a path: expand ``~``, make absolute, resolve symlinks.

    Description: the single canonicalisation point for this module. It is
      ``os.path.realpath`` rather than ``Path.expanduser`` because
      expanduser resolves the tilde and stops, leaving
      ``~/Development`` as the short spelling of a symlink into iCloud.
    Inputs: path (str) - any user- or config-supplied path.
    Output: str - an absolute path with every symlink resolved.
    Example: _real('~/Development') -> '/Users/me/Library/.../Development'
    """
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _is_within(child: str, root: str) -> bool:
    """Report whether an already-canonical path sits at or under a root.

    Description: component-wise containment. ``str.startswith`` would read
      ``/Users/jsugamelevil`` as living under ``/Users/jsugamele``, which
      is precisely the kind of near-miss a path guard exists to catch.
    Inputs: child (str), root (str) - both already canonicalised.
    Output: bool - True when child is root or is nested under it.
    Example: _is_within('/a/b/c', '/a/b') -> True
    """
    child_parts = Path(child).parts
    root_parts = Path(root).parts
    if len(child_parts) < len(root_parts):
        return False
    return child_parts[: len(root_parts)] == root_parts


def projects_root(settings=None) -> str:
    """Resolve where new projects go by default, in the long spelling.

    Description: reads ``Settings.get_working_dir()`` (backed by
      ``DEFAULT_WORKING_DIR``) and canonicalises it. Falls back to
      ``FALLBACK_PROJECTS_ROOT`` only when settings cannot answer, so a
      misconfigured install still lands somewhere real rather than
      raising during a project create.
    Inputs: settings (Settings | None) - the app settings object.
    Output: str - an absolute, symlink-resolved directory path.
    Example: projects_root(settings) -> '/Users/me/Library/.../Development'
    """
    if settings is not None:
        try:
            return _real(str(settings.get_working_dir()))
        except (OSError, AttributeError, ValueError) as exc:
            # A working dir that cannot be read or created is a config
            # problem, not a reason to fail the user's create. Logged so
            # it is not invisible, then the named fallback is used.
            logger.warning("projects_root_settings_unreadable", error=str(exc))
    return _real(FALLBACK_PROJECTS_ROOT)


def allowed_roots(settings=None, extra: Optional[Sequence[str]] = None) -> List[str]:
    """List the canonical directories a new project may be created under.

    Description: the projects root, the user's home directory, and any
      explicitly supplied extras. Home is included because the folders a
      user actually names live all over it (including the iCloud Sync
      path); ``/etc``, ``/System``, ``/usr`` and ``/`` are outside it and
      stay refused.
    Inputs: settings (Settings | None); extra (sequence of str | None) -
      additional allowed parents, canonicalised the same way.
    Output: list[str] - unique canonical roots, order stable.
    Example: allowed_roots(settings) -> ['/Users/me/.../Development', '/Users/me']
    """
    roots: List[str] = [projects_root(settings)]
    try:
        roots.append(_real(str(Path.home())))
    except (OSError, RuntimeError) as exc:
        # Path.home() raises RuntimeError when no home can be determined.
        # Not fatal: the projects root alone is still a usable boundary.
        logger.warning("allowed_roots_home_unresolvable", error=str(exc))
    for item in extra or ():
        if item:
            roots.append(_real(str(item)))

    unique: List[str] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def validate_project_dir_name(name: str) -> Optional[str]:
    """Check a project name is usable as a single path component.

    Description: rejects rather than rewrites. A sanitiser that quietly
      turns "My/Project" into "My-Project" creates a folder the user did
      not ask for and cannot find later, so an illegal name comes back as
      a sentence the modal shows inline. Spaces are legal.
    Inputs: name (str) - the project name exactly as typed.
    Output: str | None - a refusal message, or None when the name is fine.
    Example: validate_project_dir_name('a/b') -> "a project name cannot contain '/'"
    """
    if not isinstance(name, str) or not name.strip():
        return "a project name is required"

    trimmed = name.strip()

    for char in ILLEGAL_NAME_CHARS:
        if char in trimmed:
            shown = "a null character" if char == "\x00" else f"'{char}'"
            return f"a project name cannot contain {shown}"

    if any(ord(char) < 32 or ord(char) == 127 for char in trimmed):
        return "a project name cannot contain control characters"

    if trimmed in RESERVED_NAMES:
        return "a project name cannot be '.' or '..'"

    if trimmed.startswith("."):
        return "a project name cannot start with a dot"

    if len(trimmed.encode("utf-8")) > MAX_NAME_BYTES:
        return f"a project name cannot be longer than {MAX_NAME_BYTES} bytes"

    return None


def _directory_is_empty(path: Path) -> Optional[bool]:
    """Report whether a directory holds nothing, or that it could not be read.

    Description: three outcomes, not two. ``None`` means the listing
      failed, which is NOT the same as "it has contents" and must not be
      reported as one.
    Inputs: path (Path) - an existing directory.
    Output: bool | None - True empty, False non-empty, None cannot determine.
    Example: _directory_is_empty(Path('/tmp/empty')) -> True
    """
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except (PermissionError, OSError):
        return None
    return False


def resolve_project_directory(
    parent_dir: str,
    name: str,
    settings=None,
    extra_roots: Optional[Sequence[str]] = None,
) -> ProjectDirectoryVerdict:
    """Work out the directory a new project should occupy, or refuse to.

    Description: canonicalises the parent, checks it exists, is a
      directory and sits under an allowed root, validates the name as a
      path component, then joins the two. Creates nothing - see
      ``ensure_project_directory``. The returned path is in the LONG
      spelling because the parent went through ``os.path.realpath``, which
      is what stops a symlinked ``~/Development`` from being recorded as
      the short form.
    Inputs: parent_dir (str) - the chosen parent; name (str) - the project
      name as typed; settings (Settings | None); extra_roots (sequence of
      str | None) - extra allowed parents.
    Output: ProjectDirectoryVerdict.
    Example: resolve_project_directory('~/code', 'My App').path
             -> '/Users/me/code/My App'
    """
    if not isinstance(parent_dir, str) or not parent_dir.strip():
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="parent_missing",
            message="choose a folder to create the project in",
        )

    name_problem = validate_project_dir_name(name)
    if name_problem:
        return ProjectDirectoryVerdict(
            ok=False, path=None, code="illegal_name", message=name_problem
        )

    try:
        parent = _real(parent_dir)
    except (OSError, ValueError) as exc:
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="cannot_determine",
            message=f"that folder could not be read: {exc}",
        )

    roots = allowed_roots(settings, extra_roots)
    if not any(_is_within(parent, root) for root in roots):
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="outside_allowed_roots",
            message=(
                "that folder is outside the places projects may be created. "
                "pick one inside your home folder or the projects folder."
            ),
        )

    parent_path = Path(parent)
    if not parent_path.exists():
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="parent_missing",
            message=f"that folder does not exist: {parent}",
        )
    if not parent_path.is_dir():
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="parent_not_dir",
            message=f"that path is not a folder: {parent}",
        )

    target = parent_path / name.strip()
    target_str = str(target)

    if target.exists():
        if not target.is_dir():
            return ProjectDirectoryVerdict(
                ok=False,
                path=None,
                code="exists_not_dir",
                message=f"a file already exists at {target_str}",
            )
        empty = _directory_is_empty(target)
        if empty is None:
            return ProjectDirectoryVerdict(
                ok=False,
                path=None,
                code="cannot_determine",
                message=(
                    f"cannot tell whether {target_str} is empty, so it was "
                    "not used. this is not a claim that it has contents."
                ),
            )
        if not empty:
            return ProjectDirectoryVerdict(
                ok=False,
                path=None,
                code="exists_non_empty",
                message=(
                    f"{target_str} already exists and is not empty. "
                    "pick another name or another folder."
                ),
            )

    return ProjectDirectoryVerdict(
        ok=True, path=target_str, code="ok", message=f"will use {target_str}"
    )


def ensure_project_directory(verdict: ProjectDirectoryVerdict) -> ProjectDirectoryVerdict:
    """Create the directory a passing verdict names.

    Description: the module's only side effect, and it refuses to act on
      anything but an ``ok`` verdict, so a refusal can never create a
      folder. ``exist_ok=True`` because an existing EMPTY directory is an
      accepted outcome of resolution.
    Inputs: verdict (ProjectDirectoryVerdict) - from resolve_project_directory.
    Output: ProjectDirectoryVerdict - the input when created, or a
      ``cannot_determine`` verdict when the mkdir failed.
    Example: ensure_project_directory(resolve_project_directory(p, n)).ok
    """
    if not verdict.ok or not verdict.path:
        return verdict
    try:
        Path(verdict.path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "project_directory_create_failed", path=verdict.path, error=str(exc)
        )
        return ProjectDirectoryVerdict(
            ok=False,
            path=None,
            code="cannot_determine",
            message=f"could not create {verdict.path}: {exc}",
        )
    return verdict
