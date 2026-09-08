"""Tests for reading a TUI ``/rename`` back out of a transcript.

EVERY FIXTURE IS A REAL RECORD SHAPE. The ``custom-title`` records below
are byte-for-byte the shape Claude Code writes, captured from two live
runs on 2026-09-08: one ``/rename`` typed into a pane, and one issued
headlessly as ``claude -p --resume <uuid> "/rename ..."``. Both append
``{"type":"custom-title","customTitle":...,"sessionId":...}`` with NO
timestamp field, which is the whole reason
:func:`decide_title_application` cannot order two titles and has to treat
its first sight of one as a baseline.

THE NEGATIVE CONTROL IS NOT OPTIONAL. A reader that always finds
something is worse than useless here, because "found a title" is what
authorises a write to the row the user is looking at. So a transcript
carrying every OTHER record type and no ``custom-title`` must return
:data:`CUSTOM_TITLE_NO_RECORD`, and it is asserted right next to the
positive that proves the matcher fires at all.

AND THE THIRD OUTCOME IS TESTED SEPARATELY FROM THE SECOND. "Read the
file and it holds no name" and "could not read the file" are different
facts. A test suite that only checks ``.found`` would pass against an
implementation that collapsed them, and the collapse is precisely what
would make an unreadable transcript silently clear somebody's title.
"""

from __future__ import annotations

import json

import pytest

from src.core.claude_title_sync import (
    CUSTOM_TITLE_FOUND,
    CUSTOM_TITLE_NO_RECORD,
    CUSTOM_TITLE_UNREADABLE,
    DEFAULT_TAIL_BYTES,
    TITLE_APPLIED,
    TITLE_BASELINE_RECORDED,
    TITLE_NOT_MEASURED,
    TITLE_UNCHANGED,
    CustomTitleRead,
    decide_title_application,
    read_newest_custom_title,
)

SESSION_UUID = "916c846d-db44-472a-8e50-65999798ee3c"


def _title_record(name: str) -> str:
    """One ``custom-title`` line, in the shape Claude Code writes it.

    Description: no ``timestamp`` key, deliberately - the real record has
      none, and a fixture that invented one would let an ordering rule
      pass a test it could never pass in production.
    Inputs: name (str) - the customTitle value.
    Output: str - a single jsonl line, no trailing newline.
    Example: _title_record('Spike')
    """
    return json.dumps(
        {
            "type": "custom-title",
            "customTitle": name,
            "sessionId": SESSION_UUID,
        }
    )


def _noise_record(kind: str) -> str:
    """One non-title record, for the negative control.

    Description: the record types that really surround a ``custom-title``
      in a live transcript - captured from the probe run, where the title
      landed at line 14 of 20 with these on either side.
    Inputs: kind (str) - the record ``type``.
    Output: str - a single jsonl line.
    Example: _noise_record('assistant')
    """
    return json.dumps(
        {
            "type": kind,
            "sessionId": SESSION_UUID,
            "timestamp": "2026-09-08T14:55:30.247Z",
        }
    )


def _write(tmp_path, lines, name="transcript.jsonl"):
    """Write jsonl lines to a temp file and return its path.

    Inputs: tmp_path (pathlib.Path) - pytest fixture. lines (list[str]).
      name (str) - filename.
    Output: str - the absolute path.
    Example: _write(tmp_path, [_title_record('x')])
    """
    target = tmp_path / name
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


# ---------------------------------------------------------------------
# The reader: three outcomes, and the newest of several titles.
# ---------------------------------------------------------------------


def test_reads_the_newest_of_several_title_records(tmp_path):
    """The LAST ``custom-title`` in the file wins, not the first.

    A conversation renamed three times carries three records; the row
    must follow the most recent one. Reading the first would pin a
    session to a name the user replaced two renames ago.
    """
    path = _write(
        tmp_path,
        [
            _title_record("First Name"),
            _noise_record("assistant"),
            _title_record("Second Name"),
            _noise_record("user"),
            _title_record("Third Name"),
            _noise_record("mode"),
        ],
    )

    read = read_newest_custom_title(path)

    assert read.status == CUSTOM_TITLE_FOUND
    assert read.title == "Third Name"
    assert read.found is True


def test_a_transcript_with_no_title_record_reports_no_record(tmp_path):
    """THE NEGATIVE CONTROL. Every other record type, and no title.

    Paired with the positive above: together they prove the matcher
    fires on a real record and does NOT fire on a file full of the
    records that surround one.
    """
    path = _write(
        tmp_path,
        [
            _noise_record("queue-operation"),
            _noise_record("user"),
            _noise_record("assistant"),
            _noise_record("agent-name"),
            _noise_record("mode"),
            _noise_record("permission-mode"),
            _noise_record("last-prompt"),
        ],
    )

    read = read_newest_custom_title(path)

    assert read.status == CUSTOM_TITLE_NO_RECORD
    assert read.title is None
    assert read.found is False


def test_an_unreadable_path_is_a_named_outcome_not_an_exception(tmp_path):
    """A missing transcript returns UNREADABLE, and never raises.

    This runs on the hook endpoint's critical path. An exception here
    would cost a live session its activity update over a cosmetic
    feature, so the failure has to arrive as a value.
    """
    read = read_newest_custom_title(str(tmp_path / "does-not-exist.jsonl"))

    assert read.status == CUSTOM_TITLE_UNREADABLE
    assert read.found is False
    assert read.detail


def test_a_none_path_is_unreadable_rather_than_empty():
    """No path at all is still the third outcome, not the second.

    ``None`` means nothing was looked at. Reporting it as NO_RECORD would
    claim a measurement that never happened.
    """
    read = read_newest_custom_title(None)

    assert read.status == CUSTOM_TITLE_UNREADABLE
    assert read.found is False


def test_a_directory_in_place_of_a_transcript_is_unreadable(tmp_path):
    """A path that is not a file fails as a value, not a traceback."""
    read = read_newest_custom_title(str(tmp_path))

    assert read.status == CUSTOM_TITLE_UNREADABLE


def test_a_malformed_line_is_skipped_and_the_good_one_still_reads(tmp_path):
    """A half-written trailing line is normal, not an error.

    A transcript is appended to by a process that is still running, so
    the last line is routinely a partial write.
    """
    target = tmp_path / "partial.jsonl"
    target.write_text(
        _title_record("Good Name") + "\n" + '{"type":"custom-ti',
        encoding="utf-8",
    )

    read = read_newest_custom_title(str(target))

    assert read.status == CUSTOM_TITLE_FOUND
    assert read.title == "Good Name"


def test_a_title_record_with_a_blank_name_is_not_a_title(tmp_path):
    """An empty ``customTitle`` cannot rename anything.

    Applying it would blank the row's visible name, which is worse than
    the stale name it replaced.
    """
    path = _write(
        tmp_path,
        [
            _title_record("Real Name"),
            json.dumps(
                {"type": "custom-title", "customTitle": "   ", "sessionId": SESSION_UUID}
            ),
        ],
    )

    read = read_newest_custom_title(path)

    assert read.status == CUSTOM_TITLE_FOUND
    assert read.title == "Real Name"


# ---------------------------------------------------------------------
# The bound. THE COST OF A PASS IS A DESIGN CONSTRAINT, so it is asserted
# rather than described in a comment.
# ---------------------------------------------------------------------


def test_the_read_is_bounded_and_never_scans_the_whole_file(tmp_path):
    """A large transcript is read from near its END, not from byte 0.

    The real corpus on the developer's box holds a 244 MB transcript and
    this runs on every hook event, including PreToolUse which fires on
    every tool call. If this assertion ever fails, a pass has started
    walking the whole file and the event loop is about to pay for it.
    """
    filler = [_noise_record("assistant") for _ in range(3000)]
    path = _write(tmp_path, filler + [_title_record("Tail Name")])

    read = read_newest_custom_title(path)

    assert read.status == CUSTOM_TITLE_FOUND
    assert read.title == "Tail Name"
    assert read.file_size > DEFAULT_TAIL_BYTES
    assert read.scanned_from == read.file_size - DEFAULT_TAIL_BYTES


def test_a_title_older_than_the_window_reads_as_no_record(tmp_path):
    """THE DELIBERATE BLIND SPOT, asserted so nobody "fixes" it.

    A rename far enough back in the file is outside the tail and reads
    as NO_RECORD. That is correct: the sync wants NEW news, NO_RECORD
    never changes anything, and the alternative is scanning 244 MB per
    tool call.
    """
    filler = [_noise_record("assistant") for _ in range(3000)]
    path = _write(tmp_path, [_title_record("Ancient Name")] + filler)

    read = read_newest_custom_title(path)

    assert read.status == CUSTOM_TITLE_NO_RECORD


def test_an_offset_only_ever_moves_the_start_forward(tmp_path):
    """``from_offset`` past the tail narrows the read; it never widens it."""
    path = _write(tmp_path, [_title_record("Only Name")])
    size = read_newest_custom_title(path).file_size

    read = read_newest_custom_title(path, from_offset=size)

    assert read.scanned_from == size
    assert read.status == CUSTOM_TITLE_NO_RECORD


def test_an_offset_past_a_truncated_file_is_clamped(tmp_path):
    """A stale offset against a shrunken file cannot seek out of range."""
    path = _write(tmp_path, [_title_record("Only Name")])

    read = read_newest_custom_title(path, from_offset=10_000_000)

    assert read.status == CUSTOM_TITLE_NO_RECORD
    assert read.scanned_from == read.file_size


# ---------------------------------------------------------------------
# The decision, and the idempotence that hook events demand.
# ---------------------------------------------------------------------


def _found(title):
    """A FOUND read carrying ``title``, for the decision tests.

    Inputs: title (str).
    Output: CustomTitleRead.
    Example: _found('Spike')
    """
    return CustomTitleRead(CUSTOM_TITLE_FOUND, title=title, file_size=1, scanned_from=0)


def test_first_sight_of_a_title_is_a_baseline_and_moves_nothing():
    """A row that never recorded a claude title does NOT get renamed.

    The record carries no timestamp, so there is no way to tell whether
    it predates the label the row already shows. Acting on it would
    overwrite a name the user set in the browser with one they typed
    days ago.
    """
    verdict = decide_title_application(
        _found("Typed Days Ago"),
        current_title="Set In The Browser",
        recorded_claude_title=None,
    )

    assert verdict.action == TITLE_BASELINE_RECORDED
    assert verdict.title == "Typed Days Ago"
    assert verdict.writes_visible_title is False


def test_a_title_that_changed_after_the_baseline_is_applied():
    """The rename we CAN attribute: it moved while we were watching."""
    verdict = decide_title_application(
        _found("New Name"),
        current_title="Old Name",
        recorded_claude_title="Old Name",
    )

    assert verdict.action == TITLE_APPLIED
    assert verdict.title == "New Name"
    assert verdict.writes_visible_title is True


def test_the_same_event_twice_writes_nothing_the_second_time():
    """IDEMPOTENCE. Hook events are duplicated; a pass must survive it.

    The second pass compares against what the first pass recorded and
    stops, which is why this needs no dedupe table and no ordering.
    """
    first = decide_title_application(
        _found("New Name"),
        current_title="Old Name",
        recorded_claude_title="Old Name",
    )
    assert first.action == TITLE_APPLIED

    second = decide_title_application(
        _found("New Name"),
        current_title=first.title,
        recorded_claude_title=first.title,
    )

    assert second.action == TITLE_UNCHANGED
    assert second.writes_visible_title is False


def test_a_read_that_did_not_answer_decides_nothing():
    """UNREADABLE must never be spelled the same way as UNCHANGED.

    Collapsing them is what would let an unreadable transcript look like
    agreement, and agreement is what suppresses a later real rename.
    """
    verdict = decide_title_application(
        CustomTitleRead(CUSTOM_TITLE_UNREADABLE, detail="gone"),
        current_title="Whatever",
        recorded_claude_title="Whatever",
    )

    assert verdict.action == TITLE_NOT_MEASURED
    assert verdict.writes_visible_title is False


def test_no_record_decides_nothing_either():
    """The second outcome is also not a licence to write."""
    verdict = decide_title_application(
        CustomTitleRead(CUSTOM_TITLE_NO_RECORD, detail="nothing in the window"),
        current_title="Whatever",
        recorded_claude_title=None,
    )

    assert verdict.action == TITLE_NOT_MEASURED


def test_a_browser_rename_whose_push_landed_records_without_reannouncing():
    """The convergence case, and why APPLIED does not imply a broadcast.

    A browser rename to X pushes a ``/rename X`` that appends a record.
    The next pass reads X, which differs from the recorded claude title
    but already equals the visible one - so the marker is updated and
    nothing is announced.
    """
    verdict = decide_title_application(
        _found("Browser Name"),
        current_title="Browser Name",
        recorded_claude_title="Older Name",
    )

    assert verdict.action == TITLE_APPLIED
    assert verdict.writes_visible_title is False


def test_a_stale_claude_title_does_not_clobber_a_newer_browser_label():
    """The clobber this design exists to prevent.

    Browser renamed to X; the push did not land, so the transcript still
    says Y and ``claude_title`` still says Y. The pass must see agreement
    on claude's side and leave X alone.
    """
    verdict = decide_title_application(
        _found("Stale Claude Name"),
        current_title="Fresh Browser Label",
        recorded_claude_title="Stale Claude Name",
    )

    assert verdict.action == TITLE_UNCHANGED
    assert verdict.writes_visible_title is False


@pytest.mark.parametrize("recorded", ["", "   ", None])
def test_a_blank_recorded_marker_counts_as_never_recorded(recorded):
    """An empty string in the column is absence, not a name to compare."""
    verdict = decide_title_application(
        _found("Some Name"),
        current_title="Label",
        recorded_claude_title=recorded,
    )

    assert verdict.action == TITLE_BASELINE_RECORDED
