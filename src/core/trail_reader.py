"""Reading migration_trail.jsonl, with three outcomes and never two.

The counterpart to src/core/migration_trail.py's writer. Everything about
classifying a trail file - clean, truncated-at-the-tail, or corrupt
somewhere in the middle - lives here.

THE DISTINCTION THAT MATTERS. A truncated LAST line is the expected shape
of "the process died mid-write()", it is recoverable, and it resolves to
an interrupted step the app can retry. Corruption anywhere ELSE is
COULD-NOT-EVALUATE for the trail's whole history, and it pauses automatic
migration. Collapsing the second into "no trail exists, must be a fresh
install" would re-run every migration over live data, which is the single
worst thing this subsystem can do.

DELIBERATE DEVIATION FROM THE DESIGN DOC (section 9.4 case 4). The design
lists "out-of-order timestamps" as a corruption signal. This module does
NOT treat them as one. Corruption pauses all automatic migration, and a
benign clock adjustment (an NTP step, or two entries inside the same
second from a bash writer with one-second resolution) would then take a
healthy install offline for upgrades with nothing actually wrong.
STRUCTURAL corruption - a line that is not JSON, is not an object, or is
missing a required field - is detected and escalated exactly as designed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.core.db_models import TRAIL_CLOSING_STATUSES, TRAIL_STATUS_STARTED
from src.core.trail_entry import REQUIRED_FIELDS, TrailEntry, utc_now

_UUID_RE = re.compile(r'"entry_uuid"\s*:\s*"([^"]+)"')
_KIND_RE = re.compile(r'"kind"\s*:\s*"([^"]+)"')
_STARTED_AT_RE = re.compile(r'"started_at"\s*:\s*"([^"]+)"')

# Read outcomes. Three, never two, and the third is not a flavour of
# either other one.
TRAIL_READ_OK = "ok"                    # every line parsed, file ends cleanly
TRAIL_READ_ABSENT = "absent"            # no file at all: a genuine fresh install
TRAIL_READ_TRUNCATED_TAIL = "truncated_tail"   # last line cut mid-write, recovered
TRAIL_READ_UNREADABLE = "unreadable"    # corruption not confined to the last line


@dataclass
class TrailReadResult:
    """The outcome of reading migration_trail.jsonl. Three states, named.

    Description: ``status`` is one of TRAIL_READ_OK, TRAIL_READ_ABSENT,
      TRAIL_READ_TRUNCATED_TAIL or TRAIL_READ_UNREADABLE. UNREADABLE is
      never collapsed into ABSENT: "no trail exists, must be a fresh
      install" would re-run every migration over live data, which is the
      single worst thing this subsystem can do.
    Inputs (constructor): status (str), entries (list[TrailEntry] - every
      line that DID parse, in file order), corrupt_line_no (int | None -
      1-based line number of the first structurally bad line), error
      (str | None - human-readable reason), line_count (int).
    Output: a TrailReadResult instance.
    """

    status: str
    entries: List[TrailEntry] = field(default_factory=list)
    corrupt_line_no: Optional[int] = None
    error: Optional[str] = None
    line_count: int = 0

    @property
    def is_usable(self) -> bool:
        """Whether automatic migration is allowed to proceed on this trail.

        Inputs: none.
        Output: bool - True for OK, ABSENT and TRUNCATED_TAIL (the last
          being the expected shape of a crash mid-write, which is
          recoverable). False for UNREADABLE, which pauses migration.
        """
        return self.status != TRAIL_READ_UNREADABLE


def _recover_partial_line(raw: str) -> Optional[TrailEntry]:
    """Rebuild what can be salvaged from a line cut off mid-write.

    Description: implements design section 9.4 case 3. A process killed
      during write() leaves a prefix of a line. Because entry_uuid and
      kind are written FIRST (see FIELD_ORDER), that prefix usually still
      carries both, which is enough to treat the record as an unclosed
      ``started`` entry - identical handling to case 2. When the prefix is
      too short to carry them, no entry is recovered and the caller
      escalates that ONE line to the unreadable state.
    Inputs: raw (str) - the incomplete final line, without a newline.
    Output: TrailEntry | None - a synthetic entry with
      ``recovered_partial=True`` and ``status='started'``, or None when
      entry_uuid or kind could not be read out of the fragment.
    """
    uuid_match = _UUID_RE.search(raw)
    kind_match = _KIND_RE.search(raw)
    if not uuid_match or not kind_match:
        return None
    started_match = _STARTED_AT_RE.search(raw)
    return TrailEntry(
        entry_uuid=uuid_match.group(1),
        kind=kind_match.group(1),
        status=TRAIL_STATUS_STARTED,
        started_at=started_match.group(1) if started_match else utc_now(),
        detail="recovered from a truncated trail line",
        recovered_partial=True,
    )


def read_trail(path: Path) -> TrailReadResult:
    """Parse migration_trail.jsonl into a three-outcome read result.

    Description: implements design section 9.4 cases 1 to 4.

      * File absent  -> TRAIL_READ_ABSENT (a genuine fresh install).
      * Every line valid, file ends with a newline -> TRAIL_READ_OK.
      * Every line valid except the last, which lacks a trailing newline
        and is a recoverable fragment -> TRAIL_READ_TRUNCATED_TAIL. The
        fragment becomes an unclosed ``started`` entry.
      * Any structurally bad line that is NOT a recoverable tail (bad
        JSON in the middle, a non-object line, a line missing a required
        field, or an unrecoverable tail fragment) -> TRAIL_READ_UNREADABLE,
        naming the 1-based line number. Every line that DID parse is
        still returned, because "I could not read line 7" is not "I could
        not read anything".

      An OSError reading the file is also UNREADABLE, never ABSENT: a
      permission error is not evidence that a user has no history.
    Inputs: path (Path) - the migration_trail.jsonl path.
    Output: TrailReadResult.
    Example: read_trail(state_dir / "migration_trail.jsonl").status == "ok"
    """
    if not path.exists():
        return TrailReadResult(status=TRAIL_READ_ABSENT)

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return TrailReadResult(
            status=TRAIL_READ_UNREADABLE,
            error=f"could not read {path.name}: {exc}",
        )

    if not raw_bytes:
        # An empty file is not the same as no file, but neither is it
        # corrupt: it is what an interrupted create leaves behind.
        return TrailReadResult(status=TRAIL_READ_OK, line_count=0)

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return TrailReadResult(
            status=TRAIL_READ_UNREADABLE,
            error=f"{path.name} is not valid UTF-8: {exc}",
        )

    ends_cleanly = text.endswith("\n")
    lines = text.split("\n")
    if ends_cleanly:
        lines = lines[:-1]  # split leaves a trailing empty element

    entries: List[TrailEntry] = []
    last_index = len(lines) - 1
    for index, raw_line in enumerate(lines):
        line_no = index + 1
        stripped = raw_line.strip()
        if not stripped:
            continue
        parsed = _parse_line(stripped)
        if parsed is not None:
            entries.append(parsed)
            continue
        is_tail = index == last_index and not ends_cleanly
        if is_tail:
            recovered = _recover_partial_line(stripped)
            if recovered is not None:
                entries.append(recovered)
                return TrailReadResult(
                    status=TRAIL_READ_TRUNCATED_TAIL,
                    entries=entries,
                    corrupt_line_no=line_no,
                    error=(
                        f"{path.name} line {line_no} was cut off mid-write; "
                        "entry_uuid and kind were recovered and the step is "
                        "treated as interrupted"
                    ),
                    line_count=len(lines),
                )
            return TrailReadResult(
                status=TRAIL_READ_UNREADABLE,
                entries=entries,
                corrupt_line_no=line_no,
                error=(
                    f"{path.name} line {line_no} is an incomplete final line "
                    "and no entry_uuid could be recovered from it"
                ),
                line_count=len(lines),
            )
        return TrailReadResult(
            status=TRAIL_READ_UNREADABLE,
            entries=entries,
            corrupt_line_no=line_no,
            error=f"{path.name} is corrupt at line {line_no}",
            line_count=len(lines),
        )

    return TrailReadResult(
        status=TRAIL_READ_OK, entries=entries, line_count=len(lines)
    )


def _parse_line(stripped: str) -> Optional[TrailEntry]:
    """Parse one complete JSONL line, or report it unusable.

    Inputs: stripped (str) - one line, whitespace-trimmed, non-empty.
    Output: TrailEntry | None - None when the line is not a JSON object
      or is missing any of REQUIRED_FIELDS.
    """
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for required in REQUIRED_FIELDS:
        if data.get(required) in (None, ""):
            return None
    return TrailEntry.from_dict(data)


def find_unclosed(entries: List[TrailEntry]) -> List[TrailEntry]:
    """Return every ``started`` entry with no closing line.

    Description: implements design section 9.4 case 2. An entry is
      INTERRUPTED when its entry_uuid has a ``started`` line and no line
      in db_models.TRAIL_CLOSING_STATUSES. This is DETECTED, not guessed:
      the started line exists on disk and nothing closes it.
    Inputs: entries (list[TrailEntry]) - all parsed lines, in file order.
    Output: list[TrailEntry] - the unclosed started entries, in file
      order. Empty when the trail is fully closed.
    """
    closed = {e.entry_uuid for e in entries if e.status in TRAIL_CLOSING_STATUSES}
    return [
        e
        for e in entries
        if e.status == TRAIL_STATUS_STARTED and e.entry_uuid not in closed
    ]


def prior_interrupt_uuid(
    read: TrailReadResult, started: TrailEntry
) -> Optional[str]:
    """Find an earlier INTERRUPTED attempt at the same transition.

    Description: lets a successful retry close as
      ``completed_after_interrupt`` referencing the first attempt, so the
      trail shows attempted / died / retried / finished rather than
      erasing the first attempt.

      "INTERRUPTED" MEANS UNCLOSED, AND THIS USED TO MEAN "STARTED".
      Every attempt writes a ``started`` line, including attempts that
      went on to close cleanly as ``failed``. Matching any earlier
      ``started`` line therefore matched successful and failed attempts
      too, so a retry after a CLEAN FAILURE recorded
      ``completed_after_interrupt`` and asserted an interrupt that never
      happened. The trail's whole value is that it is the one artifact
      nobody has to infer from, so a fabricated interrupt in it is worse
      than a missing one.

      An entry is interrupted when its ``entry_uuid`` has a ``started``
      line and NO closing line, which
      :func:`src.core.trail_reader.find_unclosed` already determines
      from the file itself. Reusing it means the definition of
      "interrupted" is stated once, so this function and the startup
      report that surfaces interrupted entries cannot disagree about
      which attempts those are.
    Inputs: read (TrailReadResult) - the trail as read BEFORE this run's
      started line was appended. started (TrailEntry) - this run's entry.
    Output: str | None - the entry_uuid of the most recent unclosed
      attempt at the same ``to_version``, or None when every earlier
      attempt at it was closed (whether it succeeded or failed).
    """
    for entry in reversed(find_unclosed(read.entries)):
        if entry.entry_uuid == started.entry_uuid:
            continue
        if entry.to_version == started.to_version:
            return entry.entry_uuid
    return None
