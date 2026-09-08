#!/usr/bin/env python3
"""Scan staged changes or a working tree for credential material.

TWO MODES, ONE DETECTOR SET. ``--staged`` is what the pre-commit hook
runs: it looks at the lines the commit ADDS and nothing else, so a
credential that was already in a file you happened to touch does not
block you, and so the scan stays inside its speed budget. Everything else
is audit mode: it walks a path and reports.

EXIT CODES ARE THREE-VALUED, ON PURPOSE.
  0  scanned, found nothing
  1  scanned, found credential material
  2  COULD NOT SCAN - git failed, a path does not exist, no files were
     eligible. This is not a pass. A scanner that returns 0 when it never
     looked is the exact false green this repository has spent months
     removing from its monitoring.

NOTHING HERE PRINTS A SECRET. Findings render as path, line, column,
detector, length and a sha256 prefix, plus an optional excerpt in which
every matched span is replaced before anything is truncated. ``--no-
excerpt`` drops even that.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.message_model_secrets import (  # noqa: E402
    ALL_DETECTORS,
)
from src.core.secret_scan import (  # noqa: E402
    FILE_DETECTORS,
    FileFinding,
    iter_candidate_files,
    read_text_or_none,
    scan_content,
    should_skip,
)

EXIT_CLEAN: int = 0
EXIT_FINDINGS: int = 1
EXIT_CANNOT_DETERMINE: int = 2

#: A unified-diff hunk header. ``@@ -a,b +c,d @@`` - only the ``+c`` is
#: needed, because the scan reads added lines and reports them at their
#: position in the NEW file, which is what a person opening the file sees.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def _run_git(args: Sequence[str], repo: Path) -> Optional[str]:
    """Run a git command in a repo and return stdout, or None on failure.

    Description: None is the could-not-determine signal, kept distinct
      from empty stdout, which is a real "git answered, nothing matched".
    Inputs: args (sequence of str - git subcommand and flags), repo (Path).
    Output: str or None.
    Example: _run_git(["rev-parse", "--show-toplevel"], Path(".")) -> "/x\\n"
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def staged_added_lines(repo: Path) -> Optional[Dict[str, List[Tuple[int, str]]]]:
    """The lines each staged change ADDS, keyed by path.

    Description: parses ``git diff --cached -U0``. Zero context lines
      means every ``+`` line in the output is genuinely new, so the scan
      cannot fire on content the commit is not introducing. Binary and
      deleted files contribute nothing.
    Inputs: repo (Path) - repository root.
    Output: dict mapping path to a list of (line number in the new file,
      line text), or None when git could not be asked.
    Example: staged_added_lines(Path(".")) -> {"a.py": [(3, "x = 1")]}
    """
    out = _run_git(
        ["diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"], repo
    )
    if out is None:
        return None
    added: Dict[str, List[Tuple[int, str]]] = {}
    path: Optional[str] = None
    lineno = 0
    for raw in out.split("\n"):
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target[2:]
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            lineno = int(hunk.group("start"))
            continue
        if path and raw.startswith("+"):
            added.setdefault(path, []).append((lineno, raw[1:]))
            lineno += 1
    return added


def scan_staged(
    repo: Path, detectors: Sequence[str], excerpts: bool, pragma: bool,
) -> Tuple[Optional[List[FileFinding]], int, int]:
    """Scan the lines the staged commit adds.

    Description: each file's added lines are joined into one block,
      scanned, and each finding's synthetic line number is mapped back to
      its real position in the new file.
    Inputs: repo (Path), detectors (sequence of str), excerpts (bool),
      pragma (bool - whether an inline allow comment suppresses).
    Output: (list[FileFinding] or None when git could not be asked, int
      count of files examined, int count of pragma suppressions).
    Example: scan_staged(Path("."), FILE_DETECTORS, True, True) -> ([], 0, 0)
    """
    added = staged_added_lines(repo)
    if added is None:
        return None, 0, 0
    findings: List[FileFinding] = []
    suppressed = 0
    for path, entries in sorted(added.items()):
        # Resolved against the repo root: git reports repository-relative
        # paths, which do not resolve from the caller's directory.
        if should_skip(repo / path):
            continue
        numbers = [n for n, _ in entries]
        block = "\n".join(text for _, text in entries)
        block_found, block_suppressed = scan_content(
            path, block, detectors, excerpts, pragma
        )
        suppressed += block_suppressed
        for found in block_found:
            index = min(found.line - 1, len(numbers) - 1)
            findings.append(
                FileFinding(
                    path=found.path,
                    line=numbers[index],
                    column=found.column,
                    detector=found.detector,
                    length=found.length,
                    value_sha256=found.value_sha256,
                    excerpt=found.excerpt,
                )
            )
    return findings, len(added), suppressed


def scan_tree(
    roots: Sequence[Path], detectors: Sequence[str], excerpts: bool,
    pragma: bool,
) -> Tuple[List[FileFinding], int, int, int]:
    """Scan every eligible file under the given roots.

    Inputs: roots (sequence of Path), detectors (sequence of str),
      excerpts (bool), pragma (bool).
    Output: (findings, files scanned, files skipped, pragma
      suppressions). Every count is reported rather than swallowed - a
      run that scanned nothing is a could-not-determine, and only the
      counts distinguish it from a clean tree.
    Example: scan_tree([Path("src")], FILE_DETECTORS, False, True)
      -> ([], 40, 0, 0)
    """
    findings: List[FileFinding] = []
    scanned = skipped = suppressed = 0
    for root in roots:
        for path in iter_candidate_files(root):
            if should_skip(path):
                skipped += 1
                continue
            text = read_text_or_none(path)
            if text is None:
                skipped += 1
                continue
            scanned += 1
            found, hidden = scan_content(
                str(path), text, detectors, excerpts, pragma
            )
            findings.extend(found)
            suppressed += hidden
    return findings, scanned, skipped, suppressed


def _report(findings: Sequence[FileFinding], staged: bool) -> None:
    """Print findings and, in hook mode, how to proceed.

    Inputs: findings (sequence of FileFinding), staged (bool).
    Output: None. Writes to stderr.
    Example: _report([], True) -> None
    """
    noun = "credential" if len(findings) == 1 else "credentials"
    print(
        f"\nBLOCKED: {len(findings)} possible {noun} in staged changes"
        if staged else f"\n{len(findings)} possible {noun} found",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    if not staged:
        return
    print(
        "\nThe value itself was not printed. Move it to 1Password and commit\n"
        "an op:// reference or an environment variable instead.\n"
        "If this is a false positive, commit with --no-verify and then say so\n"
        "so the detector can be fixed rather than worked around.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """The command line.

    Inputs: none.
    Output: argparse.ArgumentParser.
    Example: build_parser().parse_args(["--staged"]).staged -> True
    """
    parser = argparse.ArgumentParser(
        prog="scan_secrets.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="files or directories to audit (default: the repository root)",
    )
    parser.add_argument(
        "--staged", action="store_true",
        help="scan only the lines the staged commit adds (hook mode)",
    )
    parser.add_argument(
        "--all-detectors", action="store_true",
        help="also run the generic high-entropy assignment detector, which "
             "is noisy over source and is off by default",
    )
    parser.add_argument(
        "--no-excerpt", action="store_true",
        help="report position only, with no masked excerpt at all",
    )
    parser.add_argument(
        "--no-pragma", action="store_true",
        help="ignore every inline 'secret-scan: allow <reason>' opt-out",
    )
    parser.add_argument("--json", action="store_true", help="machine output")
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="repository root",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code - 0 clean, 1 findings, 2 could not determine.
    Example: main(["--staged"]) -> 0
    """
    args = build_parser().parse_args(argv)
    detectors = list(ALL_DETECTORS if args.all_detectors else FILE_DETECTORS)
    excerpts = not args.no_excerpt
    pragma = not args.no_pragma
    started = time.monotonic()

    if args.staged:
        findings, count, suppressed = scan_staged(
            args.repo, detectors, excerpts, pragma
        )
        if findings is None:
            print(
                "CANNOT DETERMINE: git diff --cached failed, so the staged "
                "changes were not scanned. Refusing rather than passing.",
                file=sys.stderr,
            )
            return EXIT_CANNOT_DETERMINE
        scanned, skipped = count, 0
    else:
        roots = args.paths or [args.repo]
        missing = [p for p in roots if not p.exists()]
        if missing:
            print(
                f"CANNOT DETERMINE: no such path: {missing[0]}", file=sys.stderr
            )
            return EXIT_CANNOT_DETERMINE
        findings, scanned, skipped, suppressed = scan_tree(
            roots, detectors, excerpts, pragma
        )

    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps({
            "findings": [vars(f) for f in findings],
            "files_scanned": scanned, "files_skipped": skipped,
            "pragma_suppressed": suppressed,
            "detectors": detectors, "seconds": round(elapsed, 3),
        }, indent=2))
    elif findings:
        _report(findings, args.staged)
        if suppressed:
            print(
                f"  ({suppressed} further finding(s) suppressed by an inline "
                f"'secret-scan: allow' comment)", file=sys.stderr,
            )
    elif not args.staged:
        note = (
            f", {suppressed} suppressed by inline pragma" if suppressed else ""
        )
        print(
            f"clean: {scanned} files scanned, {skipped} skipped{note}, "
            f"{len(detectors)} detectors, {elapsed:.2f}s"
        )

    if findings:
        return EXIT_FINDINGS
    if not args.staged and scanned == 0:
        print(
            "CANNOT DETERMINE: no eligible files were scanned.", file=sys.stderr
        )
        return EXIT_CANNOT_DETERMINE
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
