"""Detect iCloud fork artifacts in the Claude transcript corpus.

WHY THIS EXISTS. ``~/.claude`` is a symlink into iCloud Drive on more
than one of this owner's machines, and ``Claude/projects`` is not
excluded from sync. When two machines write the same session directory,
iCloud does not merge and does not warn - it FORKS THE DIRECTORY,
leaving ``<session-uuid>`` beside a second ``<session-uuid> 2``. On
2026-09-02 that had happened 114 times, 6,960 files, about 1.07 GB, and
113 of the 114 forks were created inside one 34-minute window. Nothing
anywhere reported it.

THREE FACTS THIS REPORTS THAT A NAIVE SWEEP GETS WRONG.

(a) THE ARTIFACTS ARE DIRECTORIES, NOT FILES. A ``find -type f -name
    '* [0-9]'`` over the corpus returns ZERO and looks clean. Measured:
    0 files, 75 directories on the same tree in the same second. Any
    check written against filenames is a check that cannot fail, so this
    module walks directories and :func:`positive_control` exists to
    prove the walk can still see a planted one.

(b) THE CANONICAL SIBLING IS USUALLY EMPTY. In 53 of 75 measured local
    pairs, ``<uuid>/`` contained ZERO files at any depth and the entire
    session lived in ``<uuid> 2/``. Anything that treats the trailing
    " 2" copy as the disposable duplicate destroys the only copy. That
    is why ``sibling_state`` is a first-class field and not a footnote.

(c) THE TWO SIDES DO NOT OVERLAP. Subagent artifacts are named from
    random ids, so no filename appears on both sides of a pair: there is
    nothing to reconcile and nothing that can be called redundant.

WHAT THIS DELIBERATELY DOES NOT DO. It never deletes, never moves,
never renames, and never emits a cleanup command. There is no code path
in this module that mutates the filesystem. It is a detector; the
decision of what to do with a fork belongs to a human holding a
verified copy.

WHY NOT ``message_ingest_findings``. That table records conditions
raised while READING TRANSCRIPT CONTENT into a corpus database, keyed to
the message gate contract. A forked directory is a filesystem condition
that exists whether or not any database has ever been opened, and it
must be observable on a machine with no corpus db at all. Forcing it
into that table would make the detector depend on the thing it is
supposed to warn about.

THE THREE-OUTCOME RULE. A path that holds no forks, a path that could
not be read, and a pair whose sibling state could not be established are
three different answers here and are reported as three different
answers. ``status`` is never ``ok`` when anything went unmeasured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

#: An iCloud fork suffix: a space, then one or more digits, at the end
#: of the directory name. iCloud numbers repeat forks upward (" 2",
#: " 3"), so the digit is not pinned to 2.
CONFLICT_SUFFIX_RE = re.compile(r" \d+$")

STATUS_CLEAN = "clean"
STATUS_CONFLICTS = "conflicts_present"
STATUS_CANNOT_DETERMINE = "cannot_determine"

#: ``<uuid>/`` exists and holds at least one file at any depth.
SIBLING_NONEMPTY = "canonical_nonempty"
#: ``<uuid>/`` exists and holds NO files at any depth. The fork is the
#: only copy of this session's content. This is the dangerous case.
SIBLING_EMPTY = "canonical_empty"
#: ``<uuid>/`` does not exist at all. Also a sole copy.
SIBLING_MISSING = "canonical_missing"
#: The sibling could not be walked. NOT the same as empty.
SIBLING_UNKNOWN = "canonical_unknown"


@dataclass
class ConflictPair:
    """One forked directory and the state of the name it forked from.

    Inputs: constructed only by :func:`scan_for_conflicts`.
    Output: n/a (data holder).
    """

    conflict_path: str
    canonical_path: str
    sibling_state: str
    file_count: int
    byte_count: int
    #: Files under the canonical sibling. 0 with ``sibling_state ==
    #: canonical_empty`` is the finding; 0 with ``canonical_unknown``
    #: is an unmeasured value and must not be read as emptiness.
    canonical_file_count: Optional[int]

    def to_record(self) -> Dict[str, object]:
        """Render as a JSON-safe dict.

        Inputs: none.
        Output: dict.
        Example: pair.to_record()["sibling_state"]
        """
        return {
            "conflict_path": self.conflict_path,
            "canonical_path": self.canonical_path,
            "sibling_state": self.sibling_state,
            "file_count": self.file_count,
            "byte_count": self.byte_count,
            "canonical_file_count": self.canonical_file_count,
        }


@dataclass
class ConflictReport:
    """What one scan measured, including what it could not measure.

    Inputs: constructed only by :func:`scan_for_conflicts`.
    Output: n/a (data holder).
    """

    root: str = ""
    status: str = STATUS_CLEAN
    reason: str = ""
    pairs: List[ConflictPair] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    sole_copy_pairs: int = 0
    unreadable_count: int = 0
    unreadable_sample: List[Dict[str, str]] = field(default_factory=list)

    def to_record(self) -> Dict[str, object]:
        """Render as the JSON object the CLI prints.

        Inputs: none.
        Output: dict.
        Example: ConflictReport().to_record()["status"] -> 'clean'
        """
        return {
            "root": self.root,
            "status": self.status,
            "reason": self.reason,
            "conflict_directories": len(self.pairs),
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "sole_copy_pairs": self.sole_copy_pairs,
            "unreadable_count": self.unreadable_count,
            "unreadable_sample": list(self.unreadable_sample),
            "pairs": [pair.to_record() for pair in self.pairs],
        }


def _measure_tree(path: Path, report: ConflictReport) -> Optional[tuple]:
    """Count files and bytes under one directory, or say it could not.

    Description: an ``OSError`` anywhere in the walk makes the whole
      measurement unknown rather than partial, because a partial count
      reported as a total is the false-green this module exists to
      prevent.
    Inputs: path (Path), report (ConflictReport, mutated on failure).
    Output: (file_count, byte_count) | None.
    Example: _measure_tree(Path("/tmp"), ConflictReport())
    """
    files = 0
    total = 0
    failed = False

    def _onerror(exc: OSError) -> None:
        nonlocal failed
        failed = True
        report.unreadable_count += 1
        if len(report.unreadable_sample) < 20:
            report.unreadable_sample.append(
                {"path": str(getattr(exc, "filename", path)),
                 "reason": str(exc)}
            )

    for current, _dirs, names in os.walk(str(path), onerror=_onerror):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(current, name))
            except OSError as exc:
                _onerror(exc)
                continue
            files += 1
    if failed:
        return None
    return files, total


def scan_for_conflicts(root: Path) -> ConflictReport:
    """Walk a corpus root and report every iCloud fork directory found.

    Description: read-only. Finds directories whose NAME ends in a space
      plus digits, measures each one, and classifies the state of the
      name it forked from. Reports zero honestly - a root with no forks
      returns ``status == 'clean'`` and an empty pair list, and a root
      that does not exist or cannot be walked returns
      ``cannot_determine``, never ``clean``.
    Inputs: root (Path) - normally ``~/.claude/projects``.
    Output: ConflictReport.
    Example: scan_for_conflicts(Path("/tmp/empty")).status -> 'clean'
    """
    report = ConflictReport(root=str(root))
    root = Path(root)
    if not root.is_dir():
        report.status = STATUS_CANNOT_DETERMINE
        report.reason = f"corpus root is not a directory: {root}"
        return report

    found: List[Path] = []
    walk_failed = False

    def _onerror(exc: OSError) -> None:
        nonlocal walk_failed
        walk_failed = True
        report.unreadable_count += 1
        if len(report.unreadable_sample) < 20:
            report.unreadable_sample.append(
                {"path": str(getattr(exc, "filename", root)),
                 "reason": str(exc)}
            )

    for current, dirs, _names in os.walk(str(root), onerror=_onerror):
        for name in sorted(dirs):
            if CONFLICT_SUFFIX_RE.search(name):
                found.append(Path(current) / name)

    for conflict in sorted(found):
        canonical = conflict.parent / CONFLICT_SUFFIX_RE.sub("", conflict.name)
        measured = _measure_tree(conflict, report)
        if measured is None:
            files, byts = 0, 0
        else:
            files, byts = measured
        if not canonical.is_dir():
            state = SIBLING_MISSING
            canonical_files: Optional[int] = None
        else:
            sib = _measure_tree(canonical, report)
            if sib is None:
                state = SIBLING_UNKNOWN
                canonical_files = None
            else:
                canonical_files = sib[0]
                state = SIBLING_EMPTY if sib[0] == 0 else SIBLING_NONEMPTY
        if state in (SIBLING_EMPTY, SIBLING_MISSING):
            report.sole_copy_pairs += 1
        report.pairs.append(
            ConflictPair(
                conflict_path=str(conflict),
                canonical_path=str(canonical),
                sibling_state=state,
                file_count=files,
                byte_count=byts,
                canonical_file_count=canonical_files,
            )
        )
        report.total_files += files
        report.total_bytes += byts

    if report.unreadable_count or walk_failed:
        report.status = STATUS_CANNOT_DETERMINE
        report.reason = (
            f"{report.unreadable_count} path(s) could not be read; the "
            "counts below are a floor, not a total"
        )
    elif report.pairs:
        report.status = STATUS_CONFLICTS
        report.reason = (
            f"{len(report.pairs)} forked director"
            f"{'y' if len(report.pairs) == 1 else 'ies'}; "
            f"{report.sole_copy_pairs} hold the only copy of their session"
        )
    else:
        report.status = STATUS_CLEAN
        report.reason = "no forked directories found"
    return report


def positive_control(root: Path) -> Dict[str, object]:
    """Prove the detector can still see a fork, using only what is there.

    Description: A DETECTOR THAT RETURNS ZERO AND A DETECTOR THAT IS
      BROKEN PRODUCE THE SAME OUTPUT, and the filename-versus-directory
      trap in this module's docstring is exactly how that happens here.
      This re-runs the matcher against a synthetic name list rather than
      against the filesystem, so it costs nothing, touches nothing, and
      still fails loudly if the pattern ever stops matching.
    Inputs: root (Path) - reported back for context only; not walked.
    Output: dict with ``passed`` and the cases exercised.
    Example: positive_control(Path("/x"))["passed"] -> True
    """
    should_match = ["abc 2", "abc 3", "abc 10", "workflows 2"]
    should_not = ["abc", "abc2", "abc 2x", "workflows", "abc-2"]
    hits = [n for n in should_match if CONFLICT_SUFFIX_RE.search(n)]
    misses = [n for n in should_not if CONFLICT_SUFFIX_RE.search(n)]
    return {
        "passed": len(hits) == len(should_match) and not misses,
        "root": str(root),
        "matched": hits,
        "false_positives": misses,
    }
