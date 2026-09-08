"""Pure path and label helpers for the transcript importer.

Split out of ``transcript_import_plan`` for the 500-line rule, and the
seam is a real one rather than an arbitrary cut: nothing here reads the
database, the plan vocabulary or a ``TranscriptFacts``. It is the
filesystem and string arithmetic the planner does before it decides
anything, which is also why it is the part with the most test cases per
line.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence, Tuple

#: Directory prefixes that are per-run scratch, never a project root.
#: ``/private/tmp`` and ``/tmp`` are the same directory on macOS and both
#: spellings appear in the live corpus, so both are listed rather than
#: relying on a resolve that a vanished path cannot perform.
SCRATCH_PREFIXES: Tuple[str, ...] = ("/private/tmp", "/tmp", "/var/folders")

#: The longest a reconstructed title may be. Long enough to carry a real
#: first sentence, short enough for a sidebar row.
TITLE_MAX = 80


def canonical(path: Optional[str]) -> Optional[str]:
    """The one spelling this codebase stores for a directory.

    Description: ``realpath``, which turns ``~/Development/x`` into its
      long iCloud spelling. It does NOT require the path to exist -
      resolving a vanished directory is well defined and returns the
      caller's own absolute spelling, which is exactly what a row for a
      deleted project should hold.
    Inputs: path (str | None).
    Output: str | None - the canonical path, or None.
    Example: canonical('/Users/x/Development/p')  # '/Users/x/Library/.../p'
    """
    if not path:
        return None
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def is_scratch(path: Optional[str]) -> bool:
    """Whether a cwd is a per-run scratch directory.

    Description: tested on BOTH the literal string and its resolved
      form, because ``/tmp`` resolves to ``/private/tmp`` on macOS while
      a vanished ``/private/tmp/...`` resolves to itself. Testing one
      only would miss half the corpus's spellings.
    Inputs: path (str | None).
    Output: bool - False for None, which is not a scratch path, it is no
      path at all.
    Example: is_scratch('/private/tmp/claude-501/x')  # True
    """
    if not path:
        return False
    for candidate in (path, canonical(path) or path):
        for prefix in SCRATCH_PREFIXES:
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
    return False


def git_top_level(path: str, *, home: Optional[str] = None) -> Optional[str]:
    """The git repository root the path sits inside, or None.

    Description: walks up looking for a ``.git`` entry, stopping at
      ``$HOME`` so a stray ``.git`` above the home directory cannot
      swallow every project on the machine. No subprocess: the answer is
      a filesystem fact, deterministic, and a test can build one. A path
      that does not exist has no repository, which is correct - the
      answer is about the disk as it is now.
    Inputs: path (str) - a canonical directory. home (str | None) -
      override for tests; defaults to the real home directory.
    Output: str | None - the repository root.
    Example: git_top_level('/Users/x/repo/src')  # '/Users/x/repo'
    """
    ceiling = canonical(home or os.path.expanduser("~")) or "/"
    current = path
    while True:
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return current
        except OSError:
            return None
        if current == ceiling or current == "/":
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def truncate_title(text: Optional[str], limit: int = TITLE_MAX) -> Optional[str]:
    """Cut a first message down to a label, on a word boundary.

    Description: newlines and runs of whitespace collapse first, so a
      multi-line prompt does not become a multi-line row. The cut is
      taken at the last space inside the limit, and an ellipsis marks
      that there was more - a title silently sliced mid-word reads as
      corrupt data rather than as an abbreviation. A single word longer
      than the limit is cut hard, because there is no boundary to find.
    Inputs: text (str | None). limit (int).
    Output: str | None - None for empty input, which means "nothing
      named this conversation".
    Example: truncate_title('fix the deploy script and rerun it', 12)
      # 'fix the...'
    """
    if not text:
        return None
    flat = " ".join(str(text).split())
    if not flat:
        return None
    if len(flat) <= limit:
        return flat
    window = flat[: limit - 3]
    cut = window.rfind(" ")
    if cut > 0:
        window = window[:cut]
    return window.rstrip(" ,.;:-") + "..."


def match_project(
    working_dir: str, project_roots: Sequence[str]
) -> Optional[str]:
    """The existing project whose root contains this directory, deepest.

    Description: both sides are already canonical by the time they get
      here. Deepest wins, which is what ``derived_deepest`` means
      everywhere else in this schema: a project rooted at ``$HOME`` does
      not outrank one rooted at the actual repository.
    Inputs: working_dir (str) - canonical. project_roots (Sequence[str])
      - canonical roots of every existing project.
    Output: str | None - the matching root.
    Example: match_project('/a/b/c', ['/a', '/a/b'])  # '/a/b'
    """
    best: Optional[str] = None
    for root in project_roots:
        if working_dir == root or working_dir.startswith(root.rstrip("/") + "/"):
            if best is None or len(root) > len(best):
                best = root
    return best
