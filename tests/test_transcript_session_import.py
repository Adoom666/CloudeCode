"""The transcript importer: what it imports, and what it refuses to.

THE CLAIM THIS FILE DEFENDS is the owner's, verbatim: "everything real
should be accounted for". The failure mode of a rule like that is a
matcher that always finds something, so the negative controls are the
point of this file, not an afterthought:

  * a cwd that does not exist on disk still imports (existence is not
    the rule, realness of the path is);
  * a conversation already held by a sessions row is SKIPPED and
    counted, never overwritten;
  * a transcript with no cwd anywhere is NOT_A_CONVERSATION, a named
    third outcome, and never a manufactured row in a manufactured
    project;
  * a scratch-path conversation does not land, and the apply pass is
    asserted on the DATABASE after the fact, not on the plan.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    PROJECT_SOURCE_TRANSCRIPT_IMPORT,
    SESSION_ORIGIN_IMPORTED,
    SESSION_OWNED_ORIGINS,
)
from src.core.transcript_import_facts import (
    TRANSCRIPT_NO_CONVERSATION,
    TRANSCRIPT_READ,
    TRANSCRIPT_UNREADABLE,
    read_transcript_facts,
)
from src.core.transcript_import_plan import (
    PLAN_ALREADY_PRESENT,
    PLAN_DUPLICATE_TRANSCRIPT,
    PLAN_EXCLUDED_SCRATCH,
    PLAN_IMPORT,
    PLAN_NOT_A_CONVERSATION,
    PLAN_NOT_A_SESSION_REF,
    dedupe_by_uuid,
    git_top_level,
    match_project,
    plan_transcript,
    truncate_title,
)
from src.core.transcript_import_write import apply_plans, display_name_for

UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def write_transcript(directory: Path, uuid: str, records) -> Path:
    """Write a jsonl transcript and return its path.

    Inputs: directory (Path), uuid (str), records (iterable[dict]).
    Output: Path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def conversation(cwd: str, uuid: str, *, text: str = "fix the deploy script"):
    """The minimum records a real conversation carries."""
    return [
        {
            "type": "user",
            "cwd": cwd,
            "sessionId": uuid,
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {"content": text},
        },
        {
            "type": "assistant",
            "cwd": cwd,
            "sessionId": uuid,
            "timestamp": "2026-03-01T10:05:00.000Z",
            "message": {"content": [{"type": "text", "text": "done"}]},
        },
    ]


@pytest.fixture()
def db(tmp_path: Path):
    """A migrated datastore with no sessions and no projects."""
    state = tmp_path / "state"
    state.mkdir()
    assert ensure_db_migrated(state).status == "ok"
    conn = connect(db_path_for(state), create=False)
    yield conn
    conn.close()


# --- reading a transcript --------------------------------------------------


def test_a_real_conversation_yields_every_fact_the_row_needs(tmp_path: Path):
    """cwd, uuid, both timestamps and the first user message."""
    path = write_transcript(
        tmp_path / "slug", UUID_A, conversation("/Users/x/proj", UUID_A)
    )
    facts = read_transcript_facts(str(path))
    assert facts.status == TRANSCRIPT_READ
    assert facts.cwd == "/Users/x/proj"
    assert facts.session_id == UUID_A
    assert facts.first_ts == "2026-03-01T10:00:00.000Z"
    assert facts.last_ts == "2026-03-01T10:05:00.000Z"
    assert facts.first_user_text == "fix the deploy script"
    assert facts.is_conversation


def test_a_custom_title_record_outranks_the_first_message(tmp_path: Path):
    """`/rename` is the owner typing a name. Nothing outranks that."""
    records = conversation("/Users/x/proj", UUID_A) + [
        {"type": "custom-title", "customTitle": "Deploy", "sessionId": UUID_A}
    ]
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    facts = read_transcript_facts(str(path))
    assert facts.custom_title == "Deploy"
    plan = plan_transcript(facts, held_uuids=[], project_roots=[])
    assert plan.title == "Deploy"
    assert plan.title_source == "custom-title"


def test_a_bookkeeping_only_file_holds_no_conversation(tmp_path: Path):
    """NEGATIVE CONTROL: the 319-file case that would fool a bad matcher.

    Measured on the live corpus 2026-09-08: 319 of 1,486 top-level
    transcripts carry no ``cwd`` anywhere in the file, being made only of
    file-history-snapshot / summary / bridge-session records. A matcher
    that manufactured a row for these would invent 319 sessions in
    invented projects and look like it had done more work, not less.
    """
    path = write_transcript(
        tmp_path / "slug",
        UUID_A,
        [
            {"type": "file-history-snapshot", "messageId": "x"},
            {"type": "summary", "summary": "a thing happened"},
        ],
    )
    facts = read_transcript_facts(str(path))
    assert facts.status == TRANSCRIPT_NO_CONVERSATION
    assert not facts.is_conversation
    plan = plan_transcript(facts, held_uuids=[], project_roots=[])
    assert plan.outcome == PLAN_NOT_A_CONVERSATION
    assert plan.project_root is None


def test_an_unreadable_file_is_not_the_same_as_an_empty_one(tmp_path: Path):
    """Could-not-look and looked-and-found-nothing are different facts."""
    facts = read_transcript_facts(str(tmp_path / "nope.jsonl"))
    assert facts.status == TRANSCRIPT_UNREADABLE
    assert not facts.is_conversation


def test_a_sidechain_user_record_never_supplies_the_title(tmp_path: Path):
    """A subagent's prompt is not the owner's first message."""
    records = [
        {
            "type": "user",
            "isSidechain": True,
            "cwd": "/Users/x/proj",
            "sessionId": UUID_A,
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {"content": "internal subagent brief"},
        }
    ] + conversation("/Users/x/proj", UUID_A, text="what the owner asked")
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    facts = read_transcript_facts(str(path))
    assert facts.first_user_text == "what the owner asked"


# --- the exclusion rule, and its negative control --------------------------


@pytest.mark.parametrize(
    "cwd",
    [
        "/private/tmp",
        "/private/tmp/claude-501/x/scratchpad/forktest",
        "/tmp/permtest-45244",
        "/var/folders/ab/cd/T/x",
    ],
)
def test_a_scratch_cwd_is_excluded(tmp_path: Path, cwd):
    """The ONE exclusion, and it is about the path, not about the work."""
    path = write_transcript(tmp_path / "slug", UUID_A, conversation(cwd, UUID_A))
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_EXCLUDED_SCRATCH
    assert cwd in (plan.detail or "")


@pytest.mark.parametrize(
    "cwd",
    [
        "/Users/x/Development/fstest",
        "/Users/x/Scratch/llmScratch/hlv3-proj",
        "/Users/x/Development/Production/permtest",
    ],
)
def test_the_owners_own_test_directories_are_real_and_import(tmp_path: Path, cwd):
    """NEGATIVE CONTROL for the exclusion rule.

    'fstest', 'scrolltest', 'permtest', a scratch LAB under a real home
    directory - these are real work in directories the owner chose. A
    rule that read the NAME rather than the path would quietly delete
    his history, which is the failure this rule is written to avoid.
    """
    path = write_transcript(tmp_path / "slug", UUID_A, conversation(cwd, UUID_A))
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_IMPORT


def test_a_cwd_that_does_not_exist_still_imports(tmp_path: Path):
    """NEGATIVE CONTROL: existence on disk is NOT the rule.

    Deleting a folder does not un-have the conversation. A rule keyed on
    os.path.isdir would silently drop every finished project, and the
    project row it needs is created ARCHIVED, so nothing appears on a
    screen the user did not ask for.
    """
    gone = "/Users/x/Development/deleted-last-year"
    assert not Path(gone).exists()
    path = write_transcript(tmp_path / "slug", UUID_A, conversation(gone, UUID_A))
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_IMPORT
    assert plan.project_root == gone
    assert plan.project_exists is False


def test_a_uuid_a_row_already_holds_is_skipped_and_counted(tmp_path: Path):
    """NEGATIVE CONTROL: never overwrite a row that may be live."""
    path = write_transcript(
        tmp_path / "slug", UUID_A, conversation("/Users/x/proj", UUID_A)
    )
    plan = plan_transcript(
        read_transcript_facts(str(path)),
        held_uuids=[UUID_A],
        project_roots=[],
    )
    assert plan.outcome == PLAN_ALREADY_PRESENT
    assert plan.writes is False


# --- project resolution ----------------------------------------------------


def test_the_deepest_containing_project_wins():
    """`derived_deepest` means what it says everywhere else too."""
    assert match_project("/a/b/c/d", ["/a", "/a/b", "/a/b/c"]) == "/a/b/c"
    assert match_project("/a/b/c/d", ["/other"]) is None
    assert match_project("/a/bc", ["/a/b"]) is None, (
        "a prefix match on a partial segment would file /a/bc under /a/b"
    )


def test_a_git_repository_root_folds_a_subdirectory_in(tmp_path: Path):
    """17 transcripts ran in claude_4/frontend and belong with claude_4."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "frontend").mkdir()
    assert git_top_level(str(repo / "frontend"), home=str(tmp_path)) == str(repo)


def test_the_git_walk_stops_at_home(tmp_path: Path):
    """A stray .git above HOME must not swallow every project."""
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "home" / "proj"
    inner.mkdir(parents=True)
    assert git_top_level(str(inner), home=str(tmp_path / "home")) is None


def test_the_symlinked_spelling_matches_the_long_one(
    tmp_path: Path, nonscratch_tmp_path: Path
):
    """`~/Development` is a symlink into iCloud. Two spellings, ONE project.

    This is the defect that already manufactured two junk project rows.
    The plan canonicalises both sides, so a conversation recorded under
    the short spelling files into the project stored under the long one.

    The PROJECT directories come from ``nonscratch_tmp_path``, not from
    ``tmp_path``: the planner refuses a cwd under ``/tmp`` as per-run
    scratch, which is correct, and pytest's tmp_path is exactly that on
    a Linux runner. The transcript file itself stays under ``tmp_path``
    - it is read, never situated, so the guard has no opinion on it.
    """
    real = nonscratch_tmp_path / "iCloud" / "Development" / "proj"
    real.mkdir(parents=True)
    link = nonscratch_tmp_path / "Development"
    link.symlink_to(nonscratch_tmp_path / "iCloud" / "Development")

    path = write_transcript(
        tmp_path / "slug", UUID_A, conversation(str(link / "proj"), UUID_A)
    )
    plan = plan_transcript(
        read_transcript_facts(str(path)),
        held_uuids=[],
        project_roots=[str(real.resolve())],
    )
    assert plan.outcome == PLAN_IMPORT
    assert plan.project_exists is True
    assert plan.project_root == str(real.resolve())
    assert plan.working_dir == str(real.resolve()), (
        "the row must store the long canonical spelling"
    )
    assert plan.recorded_cwd == str(link / "proj"), (
        "the literal cwd is kept: claude code derives its transcript "
        "directory from that exact string, so a resume has to run there"
    )


# --- titles ----------------------------------------------------------------


def test_a_title_is_cut_on_a_word_boundary():
    assert truncate_title("fix the deploy script and rerun it", 20) == "fix the deploy..."
    assert truncate_title("short", 20) == "short"
    assert truncate_title("  a   b\nc ", 20) == "a b c"
    assert truncate_title(None) is None
    assert truncate_title("") is None


def test_a_single_unbreakable_word_is_cut_hard():
    """There is no boundary to find, so the ellipsis still marks the cut."""
    assert truncate_title("a" * 40, 12) == "a" * 9 + "..."


# --- display names ---------------------------------------------------------


def test_a_colliding_display_name_widens_by_path_not_by_a_counter():
    """'media-cleanup-qnap (2)' tells the user nothing about which is which."""
    assert display_name_for("/a/b/qnap", []) == "qnap"
    assert display_name_for("/a/personal/qnap", ["qnap"]) == "personal/qnap"
    assert (
        display_name_for("/a/x/y/qnap", ["qnap", "y/qnap"]) == "x/y/qnap"
    )


# --- the write -------------------------------------------------------------


def _plans_for(tmp_path: Path, entries, *, held=(), roots=()):
    """Build plans for a list of (uuid, cwd) pairs."""
    out = []
    for uuid, cwd in entries:
        path = write_transcript(tmp_path / "slug", uuid, conversation(cwd, uuid))
        out.append(
            plan_transcript(
                read_transcript_facts(str(path)),
                held_uuids=list(held),
                project_roots=list(roots),
            )
        )
    return out


def test_apply_writes_archived_rows_in_archived_projects(db, tmp_path: Path):
    """Every row an import writes is archived, session AND project."""
    plans = _plans_for(tmp_path, [(UUID_A, "/Users/x/proj")])
    report = apply_plans(db, plans, now="2026-09-08T12:00:00Z")
    assert report.sessions_written == 1
    assert report.projects_created == 1

    row = db.execute("SELECT * FROM sessions").fetchone()
    assert row["origin"] == SESSION_ORIGIN_IMPORTED
    assert SESSION_ORIGIN_IMPORTED not in SESSION_OWNED_ORIGINS, (
        "an imported row has no tmux session to own"
    )
    assert row["lifecycle"] == "stopped"
    assert row["lifecycle_source"] == "import"
    assert row["claude_session_uuid"] == UUID_A
    assert row["claude_session_uuid_source"] == "correlated"
    assert row["archived_at"] == "2026-09-08T12:00:00Z"
    assert row["created_at"] == "2026-03-01T10:00:00.000Z", (
        "a conversation from March belongs in March, not at the top of "
        "the list because that is when the import ran"
    )
    assert row["last_work_at"] == "2026-03-01T10:05:00.000Z"
    assert row["title"] == "fix the deploy script"

    for column in (
        "tmux_name",
        "tmux_created_epoch",
        "agent_type",
        "agent_family",
        "model",
        "parent_session_id",
        "fork_kind",
    ):
        assert row[column] is None, f"{column} was invented"

    project = db.execute("SELECT * FROM projects").fetchone()
    assert project["source"] == PROJECT_SOURCE_TRANSCRIPT_IMPORT
    assert project["archived_at"] == "2026-09-08T12:00:00Z"
    assert project["presence"] == "unchecked", (
        "the importer never stats the directory, so it must not claim a "
        "presence it did not measure"
    )


def test_apply_is_idempotent_and_never_overwrites_a_held_uuid(db, tmp_path: Path):
    """Running it twice writes once. THE guarantee for a live database."""
    plans = _plans_for(tmp_path, [(UUID_A, "/Users/x/proj")])
    assert apply_plans(db, plans).sessions_written == 1
    second = apply_plans(db, plans)
    assert second.sessions_written == 0
    assert second.sessions_skipped_held == 1
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_two_conversations_in_one_directory_share_one_project(db, tmp_path: Path):
    plans = _plans_for(
        tmp_path, [(UUID_A, "/Users/x/proj"), (UUID_B, "/Users/x/proj")]
    )
    report = apply_plans(db, plans)
    assert report.sessions_written == 2
    assert report.projects_created == 1
    assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_a_scratch_transcript_does_not_land_in_the_database(db, tmp_path: Path):
    """NEGATIVE CONTROL, asserted on the DATABASE and not on the plan."""
    plans = _plans_for(
        tmp_path,
        [(UUID_A, "/Users/x/proj"), (UUID_B, "/private/tmp/claude-501/x")],
    )
    apply_plans(db, plans)
    uuids = [
        r[0] for r in db.execute("SELECT claude_session_uuid FROM sessions")
    ]
    assert uuids == [UUID_A]
    roots = [r[0] for r in db.execute("SELECT root FROM projects")]
    assert roots == ["/Users/x/proj"]


def test_the_unique_index_is_the_backstop_if_the_check_is_bypassed(db, tmp_path: Path):
    """The guarantee is the SCHEMA's, not only the code's."""
    plans = _plans_for(tmp_path, [(UUID_A, "/Users/x/proj")])
    apply_plans(db, plans)
    project_id = db.execute("SELECT id FROM projects").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute(
                "INSERT INTO sessions (session_uuid, project_id, origin, "
                "lifecycle, claude_session_uuid, created_at, updated_at) "
                "VALUES ('dupe', ?, 'imported', 'stopped', ?, 'x', 'x')",
                (project_id, UUID_A),
            )


# --- what is NOT a top-level session ---------------------------------------


def test_a_subagent_transcript_is_not_a_session(tmp_path: Path):
    """THE TRAP THIS RULE EXISTS FOR, measured before it was written.

    364 of 1,167 files carrying a cwd on the live corpus are named
    `agent-<id>.jsonl`, and their records report the PARENT
    conversation's `sessionId` - 123 parent ids across 436 files. Keying
    identity on the recorded sessionId rather than on the file stem
    proposed 134 'already_present' rows against a database holding only
    28 conversations, which is the arithmetic that caught it.
    """
    path = write_transcript(
        tmp_path / "slug",
        "agent-a2a5052",
        conversation("/Users/x/proj", UUID_A),
    )
    facts = read_transcript_facts(str(path))
    assert facts.session_id == UUID_A, "the records name the PARENT"
    plan = plan_transcript(facts, held_uuids=[], project_roots=[])
    assert plan.outcome == PLAN_NOT_A_SESSION_REF
    assert plan.claude_session_uuid == "agent-a2a5052", (
        "the identity is the file stem, because `claude --resume <id>` "
        "opens <id>.jsonl and nothing else can address it"
    )
    assert "subagent" in (plan.detail or "")


def test_an_opaque_stem_is_not_a_session(tmp_path: Path):
    """An `audit` or `journal` file is a real file and not a conversation."""
    path = write_transcript(
        tmp_path / "slug", "audit", conversation("/Users/x/proj", UUID_A)
    )
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_NOT_A_SESSION_REF


def test_a_uuid_stem_with_a_different_recorded_session_id_still_imports(
    tmp_path: Path,
):
    """NEGATIVE CONTROL for the rule above.

    A fork mints a new uuid for the FILE while records may still name the
    id it was forked from. The file is a real, resumable conversation and
    must not be swept up with the subagent runs.
    """
    path = write_transcript(
        tmp_path / "slug", UUID_B, conversation("/Users/x/proj", UUID_A)
    )
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_IMPORT
    assert plan.claude_session_uuid == UUID_B


# --- one conversation, one row ---------------------------------------------


def test_the_same_uuid_under_two_cwd_spellings_yields_one_row(tmp_path: Path):
    """A cwd spelled two ways puts one conversation in two directories.

    Both files are real. The pass keeps the one with the later activity
    and REPORTS the other rather than dropping it, so the count in the
    report and the count in the database agree.
    """
    early = write_transcript(
        tmp_path / "slug-a", UUID_A, conversation("/Users/x/short", UUID_A)
    )
    late_records = conversation("/Users/x/long", UUID_A)
    late_records[-1]["timestamp"] = "2026-06-01T10:00:00.000Z"
    late = write_transcript(tmp_path / "slug-b", UUID_A, late_records)

    plans = dedupe_by_uuid(
        [
            plan_transcript(
                read_transcript_facts(str(p)), held_uuids=[], project_roots=[]
            )
            for p in (early, late)
        ]
    )
    outcomes = [p.outcome for p in plans]
    assert outcomes == [PLAN_DUPLICATE_TRANSCRIPT, PLAN_IMPORT]
    assert plans[1].recorded_cwd == "/Users/x/long"
    assert "same conversation" in (plans[0].detail or "")


# --- titles that the owner did not type ------------------------------------


def test_an_injected_caveat_never_becomes_a_title(tmp_path: Path):
    """46 conversations would otherwise all be called the same thing.

    `<local-command-caveat>` is text Claude Code puts in the user turn.
    The scan skips a record made only of injected envelopes and keeps
    looking, exactly as it skips a sidechain.
    """
    records = [
        {
            "type": "user",
            "cwd": "/Users/x/proj",
            "sessionId": UUID_A,
            "timestamp": "2026-03-01T09:00:00.000Z",
            "message": {
                "content": (
                    "<local-command-caveat>Caveat: The messages below were "
                    "generated by the user while running local commands."
                    "</local-command-caveat>"
                )
            },
        }
    ] + conversation("/Users/x/proj", UUID_A, text="deploy the thing")
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    assert read_transcript_facts(str(path)).first_user_text == "deploy the thing"


def test_a_scheduled_task_is_titled_by_its_task_name(tmp_path: Path):
    """251 of 943 conversations open with one, and the name IS the title."""
    records = [
        {
            "type": "user",
            "cwd": "/Users/x/proj",
            "sessionId": UUID_A,
            "timestamp": "2026-03-01T09:00:00.000Z",
            "message": {
                "content": (
                    '<scheduled-task name="docker-update-check" '
                    'file="/Users/x/.claude/scheduled-tasks/d/SKILL.md">go'
                    "</scheduled-task>"
                )
            },
        }
    ]
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    facts = read_transcript_facts(str(path))
    assert facts.first_user_text == "scheduled task: docker-update-check"


def test_prose_after_an_envelope_is_still_the_title(tmp_path: Path):
    """NEGATIVE CONTROL: the test is on the WHOLE record, not a prefix."""
    records = [
        {
            "type": "user",
            "cwd": "/Users/x/proj",
            "sessionId": UUID_A,
            "timestamp": "2026-03-01T09:00:00.000Z",
            "message": {
                "content": "<system-reminder>noise</system-reminder>\nreal ask"
            },
        }
    ]
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    assert read_transcript_facts(str(path)).first_user_text == "real ask"


def test_a_conversation_with_no_typed_message_gets_no_title(tmp_path: Path):
    """45 of them on the live corpus. A real state, not a defect.

    A manufactured title would be worse: it would be indistinguishable
    from one the owner wrote.
    """
    records = [
        {
            "type": "user",
            "cwd": "/Users/x/proj",
            "sessionId": UUID_A,
            "timestamp": "2026-03-01T09:00:00.000Z",
            "message": {"content": "<local-command-caveat>x</local-command-caveat>"},
        }
    ]
    path = write_transcript(tmp_path / "slug", UUID_A, records)
    plan = plan_transcript(
        read_transcript_facts(str(path)), held_uuids=[], project_roots=[]
    )
    assert plan.outcome == PLAN_IMPORT
    assert plan.title is None
    assert plan.title_source is None
