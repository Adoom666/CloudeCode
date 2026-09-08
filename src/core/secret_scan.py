"""Scanning files and staged diffs for credential material.

WHAT THIS LAYER ADDS. :mod:`src.core.message_model_secrets` answers "is
there credential material in this string", and it is the only place that
question is answered. This module answers "which file, which line", and
does the two things a file-oriented scanner has to do that a text-
oriented one does not: decide which files are worth reading at all, and
render a finding a human can act on WITHOUT putting the credential in
front of them.

THE VALUE IS NEVER RENDERED. A :class:`FileFinding` carries a path, a
line, a column, the detector name, the length, and a sha256 - the same
discipline the detector module follows. The optional excerpt is built by
masking every matched span on the line and then truncating to a window
around the match, so the credential cannot appear in the excerpt even
partially. There is no code path here that returns or prints a matched
substring.

FALSE POSITIVES ARE THE FAILURE MODE THAT MATTERS. A hook that blocks a
correct commit gets uninstalled, and then there is no scanner at all. So
the default detector set for files is :data:`FILE_DETECTORS` - the
vendor-marker detectors only. The generic high-entropy assignment
detector is excellent over transcript bodies and unusable over source,
where every long constant under a name ending in ``_KEY`` looks like a
credential. It is available behind an explicit opt-in and it is not the
default, because the honest reading of "best we can do without screwing
everything up" is a scanner that only speaks when it is sure.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from src.core.message_model_secrets import (
    VENDOR_DETECTORS,
    SecretFinding,
    scan_text,
)

#: The detector set used when scanning FILES, as opposed to transcript
#: bodies. Vendor-marker detectors only - see this module's docstring for
#: why the generic assignment detector is deliberately absent.
FILE_DETECTORS: Tuple[str, ...] = VENDOR_DETECTORS

#: Files larger than this are not read. A credential is a few dozen
#: bytes; a file this size is a vendored bundle, a media asset or a data
#: dump, and reading it costs the hook its speed budget for nothing.
MAX_FILE_BYTES: int = 1_000_000

#: Directory names never descended into during a non-git walk. In a git
#: repo the file list comes from ``git ls-files`` instead, which excludes
#: these already by virtue of them being ignored.
SKIP_DIRS: Tuple[str, ...] = (
    ".git", "venv", ".venv", "node_modules", "__pycache__", ".worktrees",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".tox",
)

#: Suffixes never scanned. Binary content is caught by the null-byte
#: sniff as well, but skipping by suffix first avoids reading the file.
SKIP_SUFFIXES: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tgz", ".bz2", ".xz", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".mp3", ".mp4", ".m4a", ".ogg", ".wav", ".so", ".dylib",
    ".pyc", ".class", ".jar", ".db", ".sqlite", ".sqlite3",
)

#: Path substrings whose contents are third-party and not ours to fix.
#: A vendored bundle that trips a detector is a finding about somebody
#: else's repository, and it would sit in the output forever - which is
#: how a check becomes furniture nobody reads.
SKIP_PATH_PARTS: Tuple[str, ...] = ("client/vendor/", "scripts/codemirror-vendor/")

#: An inline opt-out, and the ONLY suppression mechanism there is.
#: A line carrying ``secret-scan: allow <reason>`` is not reported.
#:
#: WHY A PRAGMA AND NOT A PATH ALLOWLIST. Something in this repository
#: has to contain credential-shaped strings: a detector's positive
#: control cannot exist otherwise, and a detector with no positive
#: control is one nobody has shown can fire. The obvious fix is to
#: exclude the test files by path, and it is the wrong one - it blinds
#: the scanner to an entire file forever, including to whatever gets
#: added to that file next year. A pragma is per LINE, sits in the diff
#: where a reviewer sees it, and demands a written reason. Suppressions
#: are counted and reported, so they can never become invisible.
PRAGMA_RE = re.compile(r"secret-scan:\s*allow\s+\S")

#: How much of the line either side of a match the excerpt keeps.
EXCERPT_CONTEXT_CHARS: int = 32

#: What a masked span is replaced with. Fixed width on purpose: a mask
#: whose length tracked the secret's length would leak the length twice.
MASK: str = "***"


@dataclass(frozen=True)
class FileFinding:
    """One credential match located in a file, described without the value.

    - ``path``: the file, as given to the scanner.
    - ``line`` / ``column``: 1-based position of the match.
    - ``detector``: which detector fired.
    - ``length``: how many characters the match ran to.
    - ``value_sha256``: sha256 of the matched value, so the same
      credential appearing in two files is recognisable as one.
    - ``excerpt``: the line with every matched span replaced by
      :data:`MASK` and then truncated, or None when excerpts are off.
    """

    path: str
    line: int
    column: int
    detector: str
    length: int
    value_sha256: str
    excerpt: Optional[str]

    def render(self) -> str:
        """One human-readable line describing this finding.

        Description: the single rendering site, so no caller can invent a
          format that prints something the dataclass deliberately does
          not carry.
        Inputs: none.
        Output: str.
        Example: FileFinding("a.py", 1, 1, "d", 4, "ab", None).render()
          -> "a.py:1:1 d (4 chars, sha256 ab)"
        """
        head = (
            f"{self.path}:{self.line}:{self.column} {self.detector} "
            f"({self.length} chars, sha256 {self.value_sha256[:12]})"
        )
        return f"{head}\n    {self.excerpt}" if self.excerpt else head


def _line_starts(text: str) -> List[int]:
    """Offsets at which each line of the text begins.

    Description: built once per file so offset-to-line conversion is a
      bisect rather than a re-count per finding.
    Inputs: text (str).
    Output: list[int] - offset of the start of each line, index 0 first.
    Example: _line_starts("a\\nb") -> [0, 2]
    """
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _locate(starts: Sequence[int], offset: int) -> Tuple[int, int]:
    """Convert an absolute offset into a 1-based line and column.

    Inputs: starts (sequence of int from _line_starts), offset (int).
    Output: tuple[int, int] - (line, column), both 1-based.
    Example: _locate([0, 2], 2) -> (2, 1)
    """
    low, high = 0, len(starts) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if starts[mid] <= offset:
            low = mid
        else:
            high = mid - 1
    return low + 1, offset - starts[low] + 1


def _excerpt_for(
    text: str, starts: Sequence[int], target: SecretFinding,
    all_findings: Iterable[SecretFinding],
) -> str:
    """Build a masked, truncated excerpt of the line holding a match.

    Description: EVERY finding that overlaps the line is masked, not only
      the target, so a line carrying two credentials cannot print one of
      them while reporting the other. The window is then cut to
      EXCERPT_CONTEXT_CHARS either side of the mask, which bounds how
      much unexamined neighbouring text is shown.
    Inputs: text (str - the whole file), starts (from _line_starts),
      target (SecretFinding being rendered), all_findings (every finding
      in this file).
    Output: str - safe to print.
    Example: masked output looks like 'token = "***"'
    """
    line_index = _locate(starts, target.offset)[0] - 1
    begin = starts[line_index]
    end = starts[line_index + 1] - 1 if line_index + 1 < len(starts) else len(text)

    spans = sorted(
        (f.offset, f.offset + f.length)
        for f in all_findings
        if f.offset < end and f.offset + f.length > begin
    )
    pieces: List[str] = []
    cursor = begin
    mask_at = 0
    for span_start, span_end in spans:
        span_start, span_end = max(span_start, begin), min(span_end, end)
        pieces.append(text[cursor:span_start])
        if span_start <= target.offset:
            mask_at = sum(len(p) for p in pieces)
        pieces.append(MASK)
        cursor = span_end
    pieces.append(text[cursor:end])
    masked = "".join(pieces)

    low = max(0, mask_at - EXCERPT_CONTEXT_CHARS)
    high = min(len(masked), mask_at + len(MASK) + EXCERPT_CONTEXT_CHARS)
    window = masked[low:high].strip()
    prefix = "..." if low > 0 else ""
    suffix = "..." if high < len(masked) else ""
    return f"{prefix}{window}{suffix}"


def line_text(text: str, starts: Sequence[int], line: int) -> str:
    """The text of one 1-based line, without its newline.

    Inputs: text (str), starts (from _line_starts), line (int, 1-based).
    Output: str.
    Example: line_text("a\\nb", [0, 2], 2) -> "b"
    """
    begin = starts[line - 1]
    end = starts[line] - 1 if line < len(starts) else len(text)
    return text[begin:end]


def scan_content(
    path: str, text: str, detectors: Optional[Iterable[str]] = None,
    excerpts: bool = True, honour_pragma: bool = True,
) -> Tuple[List[FileFinding], int]:
    """Locate credential material in the text of one file.

    Description: the join between the detector module and file
      coordinates. Findings come back in file order. A line carrying the
      :data:`PRAGMA_RE` opt-out is suppressed and COUNTED - the count is
      returned rather than discarded, so a suppression can never become
      invisible the way a path allowlist does.
    Inputs: path (str - reported, not read), text (str - the content),
      detectors (iterable of str or None, defaulting to FILE_DETECTORS),
      excerpts (bool - whether to build the masked excerpt),
      honour_pragma (bool - False makes the scan ignore every opt-out).
    Output: tuple[list[FileFinding], int] - findings, and how many
      findings the pragma suppressed.
    Example: scan_content("a.env", "GH=ghp_" + "x" * 36) -> ([], 0)
    """
    chosen = FILE_DETECTORS if detectors is None else detectors
    raw = scan_text(text, detectors=chosen)
    if not raw:
        return [], 0
    starts = _line_starts(text)
    out: List[FileFinding] = []
    suppressed = 0
    for finding in raw:
        line, column = _locate(starts, finding.offset)
        if honour_pragma and PRAGMA_RE.search(line_text(text, starts, line)):
            suppressed += 1
            continue
        out.append(
            FileFinding(
                path=path,
                line=line,
                column=column,
                detector=finding.detector,
                length=finding.length,
                value_sha256=finding.value_sha256,
                excerpt=(
                    _excerpt_for(text, starts, finding, raw)
                    if excerpts else None
                ),
            )
        )
    return out, suppressed


def should_skip(path: Path) -> bool:
    """Whether a path is excluded from scanning before it is read.

    Description: suffix, size and third-party-path rules. A skip is a
      deliberate decision, not a silent one - the CLI reports the count,
      because "0 findings" over 0 files scanned is a could-not-determine
      and not a pass.

      A FAILED stat DOES NOT SKIP, and that is the fix for a measured
      bug rather than a preference. This used to return True when stat
      raised, which made the staged scan silently skip every file: it is
      handed repository-relative paths out of ``git diff``, and those do
      not resolve from whatever directory git happened to be run in. The
      hook reported a clean commit over content it had never looked at.
      Size is an optimisation; when it cannot be measured the file is
      scanned, and an unreadable one is then caught by
      :func:`read_text_or_none`, which reports it as a skip out loud.
    Inputs: path (Path).
    Output: bool.
    Example: should_skip(Path("logo.png")) -> True
    """
    posix = path.as_posix()
    if any(part in posix for part in SKIP_PATH_PARTS):
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    try:
        return path.stat().st_size > MAX_FILE_BYTES
    except OSError:
        return False


def read_text_or_none(path: Path) -> Optional[str]:
    """Read a file as UTF-8 text, or None when it is not text.

    Description: a null byte is the binary sniff. Returning None is the
      third outcome - "could not read as text" - and the caller counts it
      separately from "read and found nothing".
    Inputs: path (Path).
    Output: str or None.
    Example: read_text_or_none(Path("/dev/null")) -> ""
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def iter_candidate_files(root: Path) -> Iterator[Path]:
    """Yield the files under a root that are worth scanning.

    Description: inside a git work tree the list comes from
      ``git ls-files``, so ignored trees (venv, node_modules, worktrees)
      are excluded by the repository's own rules rather than by a second
      list that would drift from them. Outside one, it walks and prunes
      SKIP_DIRS. A single file path is yielded as itself.
    Inputs: root (Path) - a directory or a file.
    Output: iterator of Path.
    Example: next(iter_candidate_files(Path("src"))) -> Path(...)
    """
    if root.is_file():
        yield root
        return
    tracked = _git_tracked_files(root)
    if tracked is not None:
        yield from tracked
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _git_tracked_files(root: Path) -> Optional[List[Path]]:
    """Files git tracks under a root, or None when it is not a work tree.

    Inputs: root (Path).
    Output: list[Path] or None. None is "git could not answer", which the
      caller turns into a filesystem walk rather than into an empty list.
    Example: _git_tracked_files(Path("/tmp")) -> None
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = done.stdout.decode("utf-8", "replace").split("\0")
    return [root / name for name in names if name]
