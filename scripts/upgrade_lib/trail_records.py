#!/usr/bin/env python3
"""Parsing migration_trail.jsonl into coalesced steps, with three outcomes.

Split out of scripts/upgrade_lib/trail_select.py, which owns the
SELECTION policy; this module owns only the READING. The seam is the same
one the datastore code uses throughout: the measurement is separated from
the decision it authorises, because the decision here overwrites a live
database and the measurement is where a wrong answer would come from.

Standard library only, and it imports nothing from src, so it keeps
working after `git checkout tags/<old>` has swapped the source tree out
from under a running rollback.

THE REAL FILE FORMAT DIFFERS FROM THE DESIGN'S PROSE, AND THIS BUILDS
AGAINST THE REAL ONE. Design 9.3 reads as though each migration STEP gets
an entry. The shipped writer records one entry per migration RUN spanning
the whole jump: a live trail carries `from_version=1, to_version=3` for a
run that applied steps 2 and 3, with the per-step detail in the log line
only. Two consequences:

  * A logical entry is TWO OR MORE LINES sharing one entry_uuid (a
    `started` line, then a closing line). `backup_path` and
    `backup_verified` are populated only on the CLOSING line, while
    `started_at` is on both. Lines are coalesced by entry_uuid here;
    reading only the `started` lines finds every timestamp and no backups
    at all.
  * Intermediate versions inside a jump have no backup, because no backup
    was taken at them. trail_select.py answers a request to restore to
    one of those with "could not evaluate", never with the nearest
    backup.

A TRUNCATED FINAL LINE IS NOT CORRUPTION. It is the expected shape of a
process killed mid-write(), it can only ever be a `started` line (a
closing line that completed was fsynced), and so it carries no backup
path and nothing selectable is lost by dropping it. A bad line ANYWHERE
ELSE makes the whole trail unreadable, which makes the rollback refuse.
This mirrors src/core/trail_reader.py without importing it, for the
checkout reason above.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REQUIRED_FIELDS = ("entry_uuid", "kind", "status", "started_at")

# Trail read outcomes. Three, and the third is not a flavour of the others.
READ_OK = "ok"
READ_ABSENT = "absent"
READ_UNREADABLE = "unreadable"


class Step:
    """One logical migration step, coalesced from all lines sharing a uuid.

    Description: the shipped writer emits a `started` line and a closing
      line per step. Fields populated only at close (backup_path,
      backup_verified, completed_at, final status) are merged onto the
      step here so callers never have to know the trail is two lines per
      record.
    Inputs (constructor): first (dict) - the first parsed line seen for
      this entry_uuid.
    Output: a Step instance.
    """

    def __init__(self, first: Dict[str, Any]) -> None:
        self.entry_uuid = first["entry_uuid"]
        self.kind = first["kind"]
        self.started_at = first["started_at"]
        self.from_version = first.get("from_version")
        self.to_version = first.get("to_version")
        self.status = first.get("status")
        self.completed_at = first.get("completed_at")
        self.backup_path = first.get("backup_path")
        self.backup_verified = first.get("backup_verified")
        self.app_version = first.get("app_version")

    def merge(self, line: Dict[str, Any]) -> None:
        """Fold a later line for the same entry_uuid onto this step.

        Description: later lines win for every field they actually carry,
          which is how the closing line's backup_path and final status
          reach the step. A None or absent value never overwrites a value
          already recorded, so an unrelated `started` line replayed after
          a close cannot erase the backup path.
        Inputs: line (dict) - a parsed trail line with this entry_uuid.
        Output: None; mutates self.
        """
        for attr in (
            "from_version", "to_version", "completed_at", "backup_path",
            "backup_verified", "app_version",
        ):
            value = line.get(attr)
            if value is not None:
                setattr(self, attr, value)
        status = line.get("status")
        if status:
            self.status = status
        # started_at is the step's own start; the earliest wins.
        started = line.get("started_at")
        if started and started < self.started_at:
            self.started_at = started

    @property
    def has_verified_backup(self) -> bool:
        """Whether this step recorded a backup that was actually verified.

        Description: design 9.6 - "a backup that cannot be verified is
          treated as a backup that does not exist". `backup_verified` is
          1/0/None in the trail and None means "not applicable", which is
          not the same as verified.
        Inputs: none.
        Output: bool.
        """
        return bool(self.backup_path) and self.backup_verified in (1, True)


def _parse_line(raw: str) -> Optional[Dict[str, Any]]:
    """Parse one trail line, or report it structurally unusable.

    Inputs: raw (str) - one line, already whitespace-stripped, non-empty.
    Output: dict | None - None when the line is not a JSON object or is
      missing any of REQUIRED_FIELDS.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for field_name in REQUIRED_FIELDS:
        if data.get(field_name) in (None, ""):
            return None
    return data


def read_steps(path: Path) -> Tuple[str, List[Step], Optional[str], Optional[int]]:
    """Read migration_trail.jsonl into coalesced steps, with three outcomes.

    Description: mirrors src/core/trail_reader.py's classification for
      the cases a rollback cares about, without importing it - this
      program must survive the `src/` tree being checked out to an older
      tag. A truncated FINAL line is tolerated (it is the expected shape
      of a crash mid-write and it can carry no backup_path, since
      backup_path is written on a closing line that completed); a bad
      line ANYWHERE ELSE makes the whole trail unreadable.
    Inputs: path (Path) - the migration_trail.jsonl path.
    Output: (status, steps, reason, corrupt_line_no) - status is READ_OK,
      READ_ABSENT or READ_UNREADABLE; steps are sorted by started_at;
      reason and corrupt_line_no are set only when unreadable.
    Example: read_steps(Path("migration_trail.jsonl"))[0] == "ok"
    """
    if not path.exists():
        return READ_ABSENT, [], f"{path} does not exist", None
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        return READ_UNREADABLE, [], f"could not read {path}: {exc}", None
    except UnicodeDecodeError as exc:
        return READ_UNREADABLE, [], f"{path} is not valid UTF-8: {exc}", None

    ends_cleanly = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]

    steps: Dict[str, Step] = {}
    last_index = len(lines) - 1
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        parsed = _parse_line(stripped)
        if parsed is None:
            if index == last_index and not ends_cleanly:
                # Truncated tail: the expected shape of a crash mid-write.
                # It cannot be a closing line, so it carries no backup and
                # nothing selectable is lost by dropping it.
                continue
            return (
                READ_UNREADABLE,
                [],
                f"{path.name} is corrupt at line {index + 1}: the line is "
                "not a JSON object carrying entry_uuid, kind, status and "
                "started_at",
                index + 1,
            )
        uuid = parsed["entry_uuid"]
        if uuid in steps:
            steps[uuid].merge(parsed)
        else:
            steps[uuid] = Step(parsed)

    ordered = sorted(steps.values(), key=lambda s: (s.started_at, s.entry_uuid))
    return READ_OK, ordered, None, None
