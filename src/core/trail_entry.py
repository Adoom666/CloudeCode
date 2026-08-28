"""One row of the upgrade trail: the record shape, shared by both artifacts.

Split out of src/core/migration_trail.py so the WRITER (that module) and
the READER (src/core/trail_reader.py) can both depend on the record
without depending on each other.

KEY ORDER IS PART OF THE FORMAT. ``entry_uuid`` and ``kind`` are written
as the first two keys of every line so a line truncated by a crash
mid-write() still carries both, which is exactly enough for
src/core/trail_reader.py to recover it as an unclosed ``started`` entry
rather than declaring the whole trail unreadable.
"""

from __future__ import annotations

import json
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

REQUIRED_FIELDS: Tuple[str, ...] = ("entry_uuid", "kind", "status", "started_at")

# Field order on the wire. entry_uuid and kind lead so a truncated tail is
# recoverable; see the module docstring.
FIELD_ORDER: Tuple[str, ...] = (
    "entry_uuid",
    "kind",
    "from_version",
    "to_version",
    "status",
    "started_at",
    "completed_at",
    "backup_path",
    "backup_verified",
    "app_version",
    "error",
    "detail",
)

def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix.

    Description: one formatter for every timestamp this subsystem writes,
      so the trail file sorts lexicographically in real time order and a
      bash reader can compare two values with a plain string compare.
    Inputs: none.
    Output: str - e.g. "2026-08-18T09:00:00.123456Z".
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def new_entry_uuid() -> str:
    """Mint a fresh entry_uuid.

    Description: the join key between the JSONL file and the DB mirror
      table. Minted once per migration step and never reused, including
      by a retry - a retry references the earlier uuid in ``detail``
      instead, so the trail shows attempted/died/retried/finished rather
      than erasing the first attempt.
    Inputs: none.
    Output: str - a UUID4 in canonical hyphenated form.
    """
    return str(_uuid.uuid4())


@dataclass
class TrailEntry:
    """One row of the upgrade trail, in either artifact.

    Description: the single shape written to migration_trail.jsonl and
      mirrored into the migration_trail table. Fields match the design's
      section 9.3 columns exactly.
    Inputs (constructor): entry_uuid (str), kind (str - bootstrap |
      config | schema | import | code), status (str - see
      db_models.TRAIL_STATUSES), started_at (str, ISO-8601 Z), plus the
      optional from_version / to_version / completed_at / backup_path
      (str | None), backup_verified (int | None - 0, 1, or None meaning
      NOT APPLICABLE rather than "not verified"), app_version / error /
      detail (str | None).
    Output: a TrailEntry instance.
    """

    entry_uuid: str
    kind: str
    status: str
    started_at: str
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    completed_at: Optional[str] = None
    backup_path: Optional[str] = None
    backup_verified: Optional[int] = None
    app_version: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None
    # Set by read_trail for a line recovered from a truncated tail. Never
    # written to disk; it is a property of THIS read, not of the record.
    recovered_partial: bool = field(default=False, compare=False)

    def to_line(self) -> str:
        """Serialise to exactly one newline-terminated JSONL line.

        Description: emits keys in FIELD_ORDER so entry_uuid and kind lead
          and a truncated write stays partially recoverable. Never emits a
          literal newline inside the object (json.dumps escapes them).
        Inputs: none.
        Output: str - one line including its trailing "\\n".
        """
        data = asdict(self)
        data.pop("recovered_partial", None)
        ordered = {k: data[k] for k in FIELD_ORDER}
        return json.dumps(ordered, ensure_ascii=False, separators=(", ", ": ")) + "\n"

    def to_row(self) -> Tuple[Any, ...]:
        """Serialise to the migration_trail table's column order.

        Description: used only by the DB mirror writer. The file is
          written by :meth:`to_line`; these two must stay in step, which
          is asserted by a test rather than by convention.
        Inputs: none.
        Output: tuple - values for (entry_uuid, kind, from_version,
          to_version, status, started_at, completed_at, backup_path,
          backup_verified, app_version, error, detail).
        """
        return (
            self.entry_uuid,
            self.kind,
            self.from_version,
            self.to_version,
            self.status,
            self.started_at,
            self.completed_at,
            self.backup_path,
            self.backup_verified,
            self.app_version,
            self.error,
            self.detail,
        )

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "TrailEntry":
        """Build a TrailEntry from an already-parsed JSON object.

        Description: tolerant of unknown extra keys (a newer app version
          may have written a field this code does not know) and of
          unrecognised ``kind`` / ``status`` VALUES, which are carried
          through verbatim rather than treated as corruption - an unknown
          status simply never closes an entry, which is the conservative
          outcome. Absent REQUIRED_FIELDS is corruption and is the
          caller's job to check first.
        Inputs: data (dict) - one parsed JSONL line.
        Output: TrailEntry.
        """
        return TrailEntry(
            entry_uuid=str(data["entry_uuid"]),
            kind=str(data["kind"]),
            status=str(data["status"]),
            started_at=str(data["started_at"]),
            from_version=_opt_str(data.get("from_version")),
            to_version=_opt_str(data.get("to_version")),
            completed_at=_opt_str(data.get("completed_at")),
            backup_path=_opt_str(data.get("backup_path")),
            backup_verified=_opt_int(data.get("backup_verified")),
            app_version=_opt_str(data.get("app_version")),
            error=_opt_str(data.get("error")),
            detail=_opt_str(data.get("detail")),
        )


def _opt_str(value: Any) -> Optional[str]:
    """Coerce a JSON value to str or None.

    Inputs: value (Any).
    Output: str | None - None for JSON null, str(value) otherwise.
    """
    return None if value is None else str(value)


def _opt_int(value: Any) -> Optional[int]:
    """Coerce a JSON value to int or None, never raising.

    Description: backup_verified is tri-valued on purpose - 1 verified,
      0 verification FAILED, None NOT APPLICABLE (no backup was owed).
      A value that will not coerce is reported as None (not applicable
      is wrong here, but inventing a 0 or a 1 is worse).
    Inputs: value (Any).
    Output: int | None.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


