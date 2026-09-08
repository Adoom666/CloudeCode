"""The interactive/automated/unknown ladder, against REAL record shapes.

Every record dict below is the shape a real transcript on the owner's box
carries, trimmed to the fields the ladder reads. Nothing here is invented
from the docs.

THE NEGATIVE CONTROLS ARE THE POINT OF THIS MODULE. A matcher that always
finds something is worse than useless, so three of these tests assert the
classifier does NOT fire:

  * a transcript with no markers at all is ``unknown``, not automated -
    305 rows on the owner's box are exactly this, all of them written by
    a claude old enough to predate the ``entrypoint`` field;
  * a title that READS as delegated work ("Implement the following
    plan: ...") classifies INTERACTIVE, because a ``planContent`` record
    is a human approving a plan in the TUI;
  * a file carrying BOTH a headless and an interactive entrypoint keeps
    the row, because a human was demonstrably in one of those turns.

And one asserts it fires where a TITLE rule would miss: a scheduler run
titled "Urbackup completion proof". Three of those exist on the owner's
box.
"""

from __future__ import annotations

import pytest

from src.core.session_kind import (
    KIND_AUTOMATED,
    KIND_INTERACTIVE,
    KIND_UNKNOWN,
    MARKER_HEADLESS_ENTRYPOINT,
    MARKER_INTERACTIVE_ENTRYPOINT,
    MARKER_NONE,
    MARKER_PLAN_APPROVED,
    MARKER_SCHEDULER_TAG,
    MARKER_TUI_RECORD,
    MARKER_UNREADABLE,
    SESSION_KINDS,
    classify_records,
    classify_transcript,
)

# --- real record shapes, trimmed --------------------------------------

#: The scheduler's own enqueue record. The tag is what it writes.
SCHEDULER_ENQUEUE = {
    "type": "queue-operation",
    "operation": "enqueue",
    "sessionId": "94d7ab22-f546-46a6-9e1c-0002ca583503",
    "content": (
        '<scheduled-task name="hirsch-9pm-reboots" '
        'file="/Users/x/.claude/scheduled-tasks/hirsch-9pm-reboots/SKILL.md">\n'
    ),
}

#: A scheduled run's first user record. NOTE the entrypoint: a scheduled
#: run is stamped claude-desktop exactly like a human one, which is why
#: the scheduler rung has to come FIRST in the ladder.
SCHEDULER_USER = {
    "type": "user",
    "userType": "external",
    "entrypoint": "claude-desktop",
    "promptSource": "sdk",
    "permissionMode": "default",
    "isSidechain": False,
}

#: A headless `claude -p` probe's user record.
HEADLESS_USER = {
    "type": "user",
    "userType": "external",
    "entrypoint": "sdk-cli",
    "promptSource": "sdk",
    "permissionMode": "bypassPermissions",
    "isSidechain": False,
}

#: A TUI session's user record: the entrypoint this app's tmux panes use.
TUI_USER = {
    "type": "user",
    "userType": "external",
    "entrypoint": "cli",
    "permissionMode": "bypassPermissions",
    "isSidechain": False,
}

#: The desktop app, a human at a keyboard.
DESKTOP_USER = dict(TUI_USER, entrypoint="claude-desktop")

#: A plan-approval record. The title on rows carrying this reads
#: "Implement the following plan: ..." and looks entirely delegated.
PLAN_USER = {
    "type": "user",
    "userType": "external",
    "cwd": "/Users/x/Development/Web/eastern",
    "isSidechain": False,
    "planContent": "# Unified Data Grid System\n\n1. ...",
}

#: A pre-2.1.87 user record. No entrypoint field existed yet.
OLD_USER = {
    "type": "user",
    "userType": "external",
    "cwd": "/Users/x/Development/CloudeCode",
    "sessionId": "0f0f",
    "version": "2.1.39",
    "isSidechain": False,
    "permissionMode": "default",
}

#: The title the owner typed. Only the interactive TUI writes it.
CUSTOM_TITLE = {
    "type": "custom-title",
    "customTitle": "Agent - Infrastructure (HA)",
    "sessionId": "1f48367b",
}


# --- POSITIVE: automated ----------------------------------------------


def test_scheduler_tag_is_automated():
    """The scheduler wraps its prompt in a tag nothing else writes."""
    verdict = classify_records([SCHEDULER_ENQUEUE, SCHEDULER_USER])
    assert verdict.kind == KIND_AUTOMATED
    assert verdict.marker == MARKER_SCHEDULER_TAG


def test_scheduler_tag_outranks_the_interactive_entrypoint():
    """ORDER IS LOAD-BEARING.

    A scheduled run stamps entrypoint 'claude-desktop' - the same value a
    human at the desktop app leaves. Reading the entrypoint first would
    call all 259 measured scheduler runs interactive, so the tag rung
    must be reached first even though the interactive marker is present.
    """
    verdict = classify_records([SCHEDULER_ENQUEUE, SCHEDULER_USER, CUSTOM_TITLE])
    assert verdict.kind == KIND_AUTOMATED


def test_scheduler_tag_is_read_from_a_user_message_too():
    """The tag arrives on a user record as well as a queue-operation."""
    user = dict(
        SCHEDULER_USER,
        message={"role": "user", "content": '<scheduled-task name="x"/>'},
    )
    assert classify_records([user]).kind == KIND_AUTOMATED


def test_headless_sdk_entrypoint_is_automated():
    """`claude -p` / the Agent SDK stamps entrypoint 'sdk-cli'."""
    verdict = classify_records([HEADLESS_USER])
    assert verdict.kind == KIND_AUTOMATED
    assert verdict.marker == MARKER_HEADLESS_ENTRYPOINT


# --- POSITIVE: interactive --------------------------------------------


@pytest.mark.parametrize("record", [TUI_USER, DESKTOP_USER])
def test_human_entrypoints_are_interactive(record):
    """'cli' is the TUI this app launches; 'claude-desktop' is the app."""
    verdict = classify_records([record])
    assert verdict.kind == KIND_INTERACTIVE
    assert verdict.marker == MARKER_INTERACTIVE_ENTRYPOINT


def test_plan_content_is_interactive():
    """NEGATIVE CONTROL FOR THE TITLE RULE.

    28 rows on the owner's box are titled "Implement the following plan:
    ..." and read as delegated or headless work. Every one carries a
    planContent record, which is ExitPlanMode - a human read a plan in
    the TUI and approved it. A title rule would have hidden all 28.
    """
    verdict = classify_records([PLAN_USER])
    assert verdict.kind == KIND_INTERACTIVE
    assert verdict.marker == MARKER_PLAN_APPROVED


def test_custom_title_record_is_interactive():
    """A title the owner typed can only come from the TUI."""
    verdict = classify_records([OLD_USER, CUSTOM_TITLE])
    assert verdict.kind == KIND_INTERACTIVE
    assert verdict.marker == MARKER_TUI_RECORD


def test_mixed_entrypoints_keep_the_row():
    """A human was in at least one of those turns, so it is not hidden.

    Three files on the owner's box carry both a headless and an
    interactive entrypoint. The safe direction for a LIST FILTER is
    always 'keep' - showing one extra row is untidy, hiding a session the
    owner worked in is the failure this filter must never produce.
    """
    verdict = classify_records([HEADLESS_USER, TUI_USER])
    assert verdict.kind == KIND_INTERACTIVE


# --- NEGATIVE: the matcher must be able to answer nothing --------------


def test_a_transcript_with_no_markers_is_unknown_not_automated():
    """305 rows measured on the owner's box are exactly this shape.

    All of them were written by claude <= 2.1.77, which predates the
    entrypoint field; the earliest confirmed scheduler run is 2.1.121 and
    the earliest confirmed headless run is 2.1.198. There is no confirmed
    automated run from that era to derive a marker from, so the honest
    answer is 'unknown' - and unknown STAYS in the owner's lists.
    """
    verdict = classify_records([OLD_USER, dict(OLD_USER, type="assistant")])
    assert verdict.kind == KIND_UNKNOWN
    assert verdict.marker == MARKER_NONE


def test_an_empty_transcript_is_unknown():
    """No records at all is still not evidence a machine ran it."""
    assert classify_records([]).kind == KIND_UNKNOWN


def test_a_non_dict_record_does_not_raise():
    """A corpus scan must not die on a line that decoded to a scalar."""
    assert classify_records([None, 3, "x", OLD_USER]).kind == KIND_UNKNOWN


def test_an_unreadable_file_is_unknown_with_its_own_marker(tmp_path):
    """COULD NOT LOOK and LOOKED AND FOUND NOTHING share a kind, not a marker."""
    verdict = classify_transcript(str(tmp_path / "absent.jsonl"))
    assert verdict.kind == KIND_UNKNOWN
    assert verdict.marker == MARKER_UNREADABLE


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    """A file being appended to while it is read must still classify."""
    import json

    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(SCHEDULER_ENQUEUE) + "\n{ this is not json\n\n"
    )
    assert classify_transcript(str(path)).kind == KIND_AUTOMATED


def test_the_vocabulary_is_exactly_three_words():
    """A fourth value is a bug, not a feature."""
    assert set(SESSION_KINDS) == {KIND_INTERACTIVE, KIND_AUTOMATED, KIND_UNKNOWN}


def test_a_title_alone_would_miss_this_scheduler_run():
    """NEGATIVE CONTROL FOR THE TITLE RULE, THE OTHER DIRECTION.

    Three scheduler runs on the owner's box are titled "Urbackup
    completion proof", "Urbackup recovery check" and "Urbackup recovery
    check 2". No title rule catches any of them; the tag catches all
    three.
    """
    verdict = classify_records([SCHEDULER_ENQUEUE, SCHEDULER_USER])
    assert verdict.kind == KIND_AUTOMATED, (
        "the row whose title reads 'Urbackup completion proof' is a "
        "scheduler run and must be classified from the tag, not the title"
    )
