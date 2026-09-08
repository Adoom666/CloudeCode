"""Carry a name typed into the TUI back to the row the browser shows.

ONE NAME PER SESSION, AND THE TRANSCRIPT IS WHERE THE TWO SIDES MEET.
A session can be renamed from two places and they do not talk to each
other: the browser writes ``sessions.title``, and ``/rename`` typed into
the pane appends a ``custom-title`` record to the conversation's jsonl.
NO HOOK EVENT CARRIES THAT RECORD - measured 2026-09-08, a ``/rename`` in
a live pane wrote the record and the row's title did not move - so the
only way to learn about it is to read the file.

The rendezvous is the transcript, because BOTH sides write to it: the TUI
writes the record directly, and a browser rename writes one indirectly
through the out-of-band ``claude -p --resume <uuid> "/rename ..."`` push
in ``claude_rename``. So this module syncs in ONE direction at the row -
transcript to ``sessions.title`` - and the other direction is the push,
which is a different module's job.

WHY ``sessions.claude_title`` IS THE MARKER AND NOT A NEW COLUMN.
The column already exists (``db_steps._step_v9_to_v10``) and has had zero
readers since the day it was added. It records what CLAUDE last said the
session was called, which is exactly the fact needed to make this
idempotent: apply a title only when it differs from the one already
recorded. The same event twice therefore writes nothing the second time,
which is the property every hook consumer in this codebase has to have -
hook events are unordered, duplicated and droppable.

THE FIRST SIGHT OF A TITLE IS A BASELINE, NOT AN INSTRUCTION.
``claude_title`` starts NULL on every existing row, on purpose: v10
refused to copy ``title`` into it because that would have invented an
attribution. So the first pass over a session can find a ``custom-title``
record with NO WAY OF KNOWING whether it predates or postdates the label
the row currently carries. ``custom-title`` records carry no timestamp
(measured: every one of them has ``timestamp`` absent), so there is
nothing to compare and no rule that could be evaluated. The first sight
is therefore recorded as a baseline and the visible title is left alone;
only a title that CHANGES while we are watching moves the row. Acting on
the first sight would silently overwrite a name the user set in the
browser with one they typed days ago.

THE 64 KB TAIL IS A DELIBERATE BOUND. The real corpus on this machine
holds a 73 MB transcript, and this runs on every hook event, including
``PreToolUse``/``PostToolUse`` which fire on every single tool call.
Reading the whole file is therefore not an option that can be traded off
against accuracy - it is not an option. A rename far enough back in the
file falls outside the window and reads as
:data:`CUSTOM_TITLE_NO_RECORD`, which never changes anything. That is the
right bias: this wants NEW news, and an old rename the row has already
been living without is not news. Do not "fix" it by widening the scan to
the whole file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

#: The record type Claude Code appends when a conversation is renamed,
#: from ``/rename`` in the TUI or from a headless ``claude -p --resume``
#: run. Verified against both on 2026-09-08. It is APPENDED in place, not
#: rewritten at the head of the file, which is what makes a tail read
#: correct rather than merely cheap.
CUSTOM_TITLE_RECORD_TYPE: str = "custom-title"

#: The field on that record holding the name.
CUSTOM_TITLE_FIELD: str = "customTitle"

#: How much of the end of the transcript to read. See the module
#: docstring for why this is a bound and not a tuning knob.
DEFAULT_TAIL_BYTES: int = 64 * 1024

# ---------------------------------------------------------------------
# Reader outcomes. THREE, never two: "looked and found nothing" and
# "could not look" are different facts and are never spelled the same
# way, the same discipline session_transcript_presence applies to a
# missing conversation.
# ---------------------------------------------------------------------

#: A ``custom-title`` record was read out of the window.
CUSTOM_TITLE_FOUND: str = "found"

#: The window was read and holds no ``custom-title`` record. A DEFINITE
#: negative about the window, and NOT a claim the conversation has never
#: been renamed - the record may simply sit further back than
#: :data:`DEFAULT_TAIL_BYTES`.
CUSTOM_TITLE_NO_RECORD: str = "no_record"

#: The file could not be read at all: absent, unreadable, or a path that
#: is not a file. Never treated as an absence.
CUSTOM_TITLE_UNREADABLE: str = "unreadable"

# ---------------------------------------------------------------------
# Application outcomes.
# ---------------------------------------------------------------------

#: The visible title moved. The caller writes both columns and tells the
#: browsers.
TITLE_APPLIED: str = "applied"

#: First sight of a claude-side title for this row. ``claude_title`` is
#: recorded so a later CHANGE can be detected; ``title`` is untouched and
#: nothing is broadcast. See the module docstring.
TITLE_BASELINE_RECORDED: str = "baseline_recorded"

#: The claude-side title is the one already recorded. Nothing is written.
#: This is the steady state and the common case.
TITLE_UNCHANGED: str = "unchanged"

#: Nothing could be read, so nothing is decided. Distinct from
#: :data:`TITLE_UNCHANGED`, which is a measured agreement.
TITLE_NOT_MEASURED: str = "not_measured"


@dataclass(frozen=True)
class CustomTitleRead:
    """The newest ``custom-title`` in the tail of one transcript.

    Description: the return of :func:`read_newest_custom_title`. Frozen
      because it reports a measurement of a file, not a plan.

      - ``status``: one of the three ``CUSTOM_TITLE_*`` constants.
      - ``title``: the name, on :data:`CUSTOM_TITLE_FOUND` only.
      - ``file_size``: the file's size in bytes at read time. A caller
        that wants to skip an unchanged file passes this back as
        ``from_offset`` next time.
      - ``scanned_from``: the byte offset the read actually started at,
        so the cost of a pass is inspectable rather than assumed.
      - ``detail``: a plain sentence naming why, for the two statuses
        that are not FOUND.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    status: str
    title: Optional[str] = None
    file_size: int = 0
    scanned_from: int = 0
    detail: Optional[str] = None

    @property
    def found(self) -> bool:
        """True only when a name was actually read.

        Description: the one-line test a write may be built on.
          Deliberately False for both non-FOUND statuses so "no record"
          and "could not look" can never be confused at a call site.
        Inputs: n/a.
        Output: bool.
        Example: read_newest_custom_title(p).found
        """
        return self.status == CUSTOM_TITLE_FOUND


@dataclass(frozen=True)
class TitleApplication:
    """What to do about a title that was read.

    Description: the return of :func:`decide_title_application`. Frozen
      for the same reason as the read - it is a verdict, and a caller
      that could edit it could route around the rules above.

      - ``action``: one of the four ``TITLE_*`` constants.
      - ``title``: the name to write, on :data:`TITLE_APPLIED` and
        :data:`TITLE_BASELINE_RECORDED`. None otherwise.
      - ``writes_visible_title``: True ONLY on :data:`TITLE_APPLIED`.
        The one flag a broadcast may be gated on, so a baseline can
        never announce a rename that did not happen.
      - ``detail``: a plain sentence naming why.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    action: str
    title: Optional[str] = None
    writes_visible_title: bool = False
    detail: Optional[str] = None


def read_newest_custom_title(
    path: Optional[str],
    *,
    from_offset: int = 0,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> CustomTitleRead:
    """The newest ``custom-title`` record in the tail of a transcript.

    Description: reads AT MOST ``tail_bytes`` from the end of the file,
      never the whole thing - see the module docstring for why that is a
      bound rather than a tuning knob. ``from_offset`` moves the start
      FORWARD only: the read begins at ``max(from_offset, size -
      tail_bytes)``, so passing the previous pass's ``file_size`` makes a
      steady-state pass read only what was appended since, while a file
      that grew by more than ``tail_bytes`` still reads only the last
      ``tail_bytes``. A shrunken or replaced file is handled by the same
      clamp rather than by a special case.

      The window's first line is dropped when the read did not start at
      byte 0, because a mid-file start almost certainly lands inside a
      line and half a json object is not a record. Any line that does not
      parse is skipped in silence - a transcript is append-only and its
      last line is routinely a partial write by a process still running,
      which is a normal condition here and not an error worth logging.

      NEVER RAISES. Every filesystem failure becomes
      :data:`CUSTOM_TITLE_UNREADABLE` with a sentence, because this runs
      on the hook path where an exception would cost a live session its
      status update.
    Inputs: path (str | None) - the transcript jsonl. from_offset (int) -
      a byte offset already consumed; only ever moves the start forward.
      tail_bytes (int) - the hard cap on how much is read.
    Output: CustomTitleRead.
    Example:
      read_newest_custom_title('/x/abc.jsonl').title -> 'Punchlist'
    """
    if not path:
        return CustomTitleRead(
            CUSTOM_TITLE_UNREADABLE,
            detail="no transcript path was given, so nothing was read",
        )

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return CustomTitleRead(
            CUSTOM_TITLE_UNREADABLE,
            detail=f"the transcript could not be measured: {exc.strerror or exc}",
        )

    start = max(0, min(int(from_offset or 0), size), size - max(1, int(tail_bytes)))
    # ``min`` before ``max`` so a from_offset past the end of a truncated
    # file cannot produce a negative or out-of-range start.
    start = min(start, size)

    try:
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            window = handle.read()
    except OSError as exc:
        return CustomTitleRead(
            CUSTOM_TITLE_UNREADABLE,
            file_size=size,
            scanned_from=start,
            detail=f"the transcript could not be read: {exc.strerror or exc}",
        )

    lines = window.split(b"\n")
    if start > 0 and lines:
        # A mid-file start lands inside a line. Half a record is not a
        # record, so it is dropped rather than parsed hopefully.
        lines = lines[1:]

    newest: Optional[str] = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # A partial trailing line from a live writer, or a line the
            # window cut in half. Normal here; see the docstring.
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != CUSTOM_TITLE_RECORD_TYPE:
            continue
        value = record.get(CUSTOM_TITLE_FIELD)
        if isinstance(value, str) and value.strip():
            newest = value.strip()

    if newest is None:
        return CustomTitleRead(
            CUSTOM_TITLE_NO_RECORD,
            file_size=size,
            scanned_from=start,
            detail=(
                "the tail of the transcript holds no custom-title "
                "record, so claude has not renamed this conversation "
                "within the window that was read"
            ),
        )

    return CustomTitleRead(
        CUSTOM_TITLE_FOUND,
        title=newest,
        file_size=size,
        scanned_from=start,
    )


def decide_title_application(
    read: CustomTitleRead,
    *,
    current_title: Optional[str],
    recorded_claude_title: Optional[str],
) -> TitleApplication:
    """Whether a claude-side title should move the visible one.

    Description: PURE. The whole rule lives here so the seam that calls
      it holds no policy and can be read in one screen.

      The ladder, in order:

      1. Nothing was read -> :data:`TITLE_NOT_MEASURED`. Covers both
         non-FOUND statuses; a read that did not answer decides nothing.
      2. The title equals the one already recorded ->
         :data:`TITLE_UNCHANGED`. THIS IS WHAT MAKES REPEATED EVENTS
         FREE, and it is why the same hook arriving twice writes once.
      3. Nothing was recorded before -> :data:`TITLE_BASELINE_RECORDED`.
         First sight is a baseline, never an instruction. See the module
         docstring.
      4. Otherwise the name CHANGED while we were watching ->
         :data:`TITLE_APPLIED`.

      Note rung 4 fires even when the new title already equals
      ``current_title``: that happens right after a browser rename whose
      push landed, and recording it is what stops the next event
      re-deciding the same thing forever. The caller writes both columns
      and may broadcast; a broadcast naming the title the browser
      already shows is a no-op there.
    Inputs: read (CustomTitleRead) - what the transcript said.
      current_title (str | None) - ``sessions.title`` as it stands.
      recorded_claude_title (str | None) - ``sessions.claude_title``.
    Output: TitleApplication.
    Example:
      decide_title_application(r, current_title='Old',
        recorded_claude_title='Old').action -> 'applied'
    """
    if not read.found or not read.title:
        return TitleApplication(
            TITLE_NOT_MEASURED,
            detail=read.detail or "no claude-side title was read",
        )

    observed = read.title
    recorded = (recorded_claude_title or "").strip() or None

    if recorded is not None and observed == recorded:
        return TitleApplication(
            TITLE_UNCHANGED,
            detail="claude's name for this conversation has not changed",
        )

    if recorded is None:
        return TitleApplication(
            TITLE_BASELINE_RECORDED,
            title=observed,
            detail=(
                "first sight of a claude-side name for this session, so "
                "it is recorded as the baseline and the visible title is "
                "left alone - there is no way to tell whether it predates "
                "the label the row already carries"
            ),
        )

    return TitleApplication(
        TITLE_APPLIED,
        title=observed,
        writes_visible_title=(observed != (current_title or "")),
        detail="claude renamed this conversation after we last looked",
    )
