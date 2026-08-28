"""Which backup a code rollback selects, and when it refuses to select one.

Covers scripts/upgrade_lib/trail_select.py, the data half of
scripts/rollback.sh (design section 9.8).

THE ONE THAT MATTERS IS test_selects_the_backup_taken_at_the_target_version.
Design 9.8's sentence ("the last schema/config entry with started_at
before the target code entry") identifies the data VERSION that was in
force at the target release. It does not identify the backup, because a
backup is taken BEFORE a step and is therefore attached to the step that
moved AWAY from that version - an entry that started after the target
code point. Selecting the entry the sentence names would restore one
version too far back while looking exactly as principled, so the test
asserts a specific file that is neither the newest nor the oldest.

The refusal tests are not edge cases, they are the feature. A rollback
tool that picks the newest backup when it cannot read the history is the
failure this whole section exists to close, so every path that cannot
establish an answer is asserted to exit non-zero, name a reason, and
leave the trail file byte-identical.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "upgrade_lib" / "trail_select.py"
)


def _load_module():
    """Import trail_select.py by path.

    Description: the module lives under scripts/, which is not a package,
      and it deliberately imports nothing from src so it survives the
      source tree being checked out to an older tag mid-rollback.
    Inputs: none.
    Output: the imported module object.
    """
    spec = importlib.util.spec_from_file_location("trail_select", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ts = _load_module()


def line(uuid, kind, status, started, **kw) -> str:
    """Build one trail JSONL line.

    Inputs: uuid (str), kind (str), status (str), started (str - an ISO
      timestamp), plus any of from_version, to_version, backup_path,
      backup_verified, completed_at, app_version, detail.
    Output: str - one JSON object, no trailing newline.
    """
    body = {
        "entry_uuid": uuid,
        "kind": kind,
        "from_version": kw.get("from_version"),
        "to_version": kw.get("to_version"),
        "status": status,
        "started_at": started,
        "completed_at": kw.get("completed_at"),
        "backup_path": kw.get("backup_path"),
        "backup_verified": kw.get("backup_verified"),
        "app_version": kw.get("app_version"),
        "error": None,
        "detail": kw.get("detail"),
    }
    return json.dumps(body)


def step_pair(uuid, kind, started, completed, **kw) -> list:
    """Build the two lines a real migration writes for one step.

    Description: the shipped writer records backup_path ONLY on the
      closing line while started_at appears on both. Tests that build a
      single fat line would never exercise the coalescing the real format
      requires, so every fixture here writes the pair.
    Inputs: uuid (str), kind (str), started (str), completed (str), plus
      from_version / to_version / backup_path / backup_verified.
    Output: list[str] - the started line then the completed line.
    """
    opened = line(uuid, kind, "started", started,
                  from_version=kw.get("from_version"),
                  to_version=kw.get("to_version"))
    closed = line(uuid, kind, "completed", started,
                  from_version=kw.get("from_version"),
                  to_version=kw.get("to_version"),
                  completed_at=completed,
                  backup_path=kw.get("backup_path"),
                  backup_verified=kw.get("backup_verified"))
    return [opened, closed]


# code v1 -> schema 1->2 -> code v2 -> schema 2->3 -> code v3 -> schema 3->4.
# Three backups exist, so "the right one" is provably neither the newest
# nor the oldest.
CANONICAL_LINES = (
    step_pair("c1", "code", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
              from_version="0.7.0", to_version="0.8.0")
    + step_pair("s12", "schema", "2026-01-02T00:00:00Z", "2026-01-02T00:00:05Z",
                from_version="1", to_version="2",
                backup_path="cloude.db.bak-v1-20260102T000000Z",
                backup_verified=1)
    + step_pair("c2", "code", "2026-01-03T00:00:00Z", "2026-01-03T00:01:00Z",
                from_version="0.8.0", to_version="0.8.1")
    + step_pair("s23", "schema", "2026-01-04T00:00:00Z", "2026-01-04T00:00:05Z",
                from_version="2", to_version="3",
                backup_path="cloude.db.bak-v2-20260104T000000Z",
                backup_verified=1)
    + step_pair("c3", "code", "2026-01-05T00:00:00Z", "2026-01-05T00:01:00Z",
                from_version="0.8.1", to_version="0.8.2")
    + step_pair("s34", "schema", "2026-01-06T00:00:00Z", "2026-01-06T00:00:05Z",
                from_version="3", to_version="4",
                backup_path="cloude.db.bak-v3-20260106T000000Z",
                backup_verified=1)
)


def write_trail(tmp_path: Path, lines, name="migration_trail.jsonl") -> Path:
    """Write a list of JSONL lines to a trail file.

    Inputs: tmp_path (Path), lines (iterable[str]), name (str).
    Output: Path - the written file.
    """
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_cli(trail: Path, target: str):
    """Invoke trail_select.py as the shell does.

    Inputs: trail (Path), target (str) - the code version.
    Output: (returncode, parsed_stdout_dict).
    """
    proc = subprocess.run(
        [sys.executable, str(_MODULE_PATH), "select",
         "--trail", str(trail), "--target-code", target],
        capture_output=True, text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def schema_item(plan):
    """Pull the schema item out of a plan.

    Inputs: plan (dict).
    Output: dict - the item whose kind is "schema".
    """
    return [i for i in plan["items"] if i["kind"] == "schema"][0]


# --- selection ---------------------------------------------------------------


def test_selects_the_backup_taken_at_the_target_version(tmp_path):
    """Rolling back to code 0.8.1 restores the v2 backup, not v1 or v3."""
    trail = write_trail(tmp_path, CANONICAL_LINES)
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_OK, plan
    item = schema_item(plan)
    assert item["outcome"] == ts.OUTCOME_RESTORE
    assert item["version_at_target"] == "2"
    assert item["backup_path"] == "cloude.db.bak-v2-20260104T000000Z"
    # Explicitly not the newest and not the oldest.
    assert item["backup_path"] != "cloude.db.bak-v3-20260106T000000Z"
    assert item["backup_path"] != "cloude.db.bak-v1-20260102T000000Z"


def test_selection_is_by_timestamp_not_line_position(tmp_path):
    """Shuffling file order changes nothing; started_at is the order."""
    ordered = write_trail(tmp_path / "a", CANONICAL_LINES) if False else None
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ordered = write_trail(tmp_path / "a", CANONICAL_LINES)
    # Reverse the file so line position is the exact opposite of time
    # order, and interleave the code lines into the middle for good
    # measure. Every started_at is unchanged.
    scrambled_lines = list(reversed(CANONICAL_LINES))
    scrambled = write_trail(tmp_path / "b", scrambled_lines)
    assert scrambled.read_text() != ordered.read_text()

    rc_ordered, plan_ordered = run_cli(ordered, "0.8.1")
    rc_scrambled, plan_scrambled = run_cli(scrambled, "0.8.1")
    assert rc_ordered == rc_scrambled == ts.EXIT_OK
    assert schema_item(plan_scrambled) == schema_item(plan_ordered)
    assert schema_item(plan_scrambled)["backup_path"] == (
        "cloude.db.bak-v2-20260104T000000Z"
    )

    # 0.8.2 has TWO data entries before it, so "the last one before the
    # target" and "the first one in the file before the target" are
    # different entries. A reader that ordered by line position would
    # answer v2 here and be indistinguishable from a correct one on the
    # 0.8.1 case above, where only one entry precedes the target.
    rc_ordered2, plan_ordered2 = run_cli(ordered, "0.8.2")
    rc_scrambled2, plan_scrambled2 = run_cli(scrambled, "0.8.2")
    assert rc_ordered2 == rc_scrambled2 == ts.EXIT_OK
    assert schema_item(plan_ordered2)["version_at_target"] == "3"
    assert schema_item(plan_scrambled2) == schema_item(plan_ordered2)
    assert schema_item(plan_scrambled2)["backup_path"] == (
        "cloude.db.bak-v3-20260106T000000Z"
    )


def test_a_kind_that_never_migrated_is_not_a_refusal(tmp_path):
    """config never moved, so the rollback proceeds and says why."""
    trail = write_trail(tmp_path, CANONICAL_LINES)
    _, plan = run_cli(trail, "0.8.1")
    config = [i for i in plan["items"] if i["kind"] == "config"][0]
    assert config["outcome"] == ts.OUTCOME_NOT_APPLICABLE
    assert "never" in config["reason"]


def test_latest_arrival_wins_when_a_version_was_installed_twice(tmp_path):
    """Two arrivals at 0.8.1: the later one anchors the data question."""
    extra = step_pair("c2b", "code", "2026-01-07T00:00:00Z",
                      "2026-01-07T00:01:00Z",
                      from_version="0.8.2", to_version="0.8.1")
    trail = write_trail(tmp_path, list(CANONICAL_LINES) + extra)
    rc, plan = run_cli(trail, "0.8.1")
    assert plan["target_code_entry_uuid"] == "c2b"
    # Schema was at 4 by then and nothing has moved away from 4, so there
    # is nothing to restore - a real outcome, not a refusal.
    assert rc == ts.EXIT_OK
    assert schema_item(plan)["outcome"] == ts.OUTCOME_ALREADY_CURRENT
    assert schema_item(plan)["version_at_target"] == "4"


def test_a_version_left_twice_selects_the_move_nearest_the_target(tmp_path):
    """After a restore back down, the same version is left again.

    Description of the hazard: a restore to v2 followed by a second
    v2 -> v3 migration puts TWO entries in the trail whose from_version is
    2, each with its own backup. They are not interchangeable - the later
    one contains everything written between the restore and the second
    migration, which is data the target code point never saw. The move
    closest in time to the target code point is the correct snapshot.
    """
    lines = list(CANONICAL_LINES) + step_pair(
        "s23b", "schema", "2026-02-01T00:00:00Z", "2026-02-01T00:00:05Z",
        from_version="2", to_version="3",
        backup_path="cloude.db.bak-v2-20260201T000000Z", backup_verified=1)
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_OK, plan
    assert schema_item(plan)["backup_path"] == "cloude.db.bak-v2-20260104T000000Z"
    assert schema_item(plan)["backup_path"] != "cloude.db.bak-v2-20260201T000000Z"
    assert schema_item(plan)["source_entry_uuid"] == "s23"


# --- refusals ----------------------------------------------------------------


def test_unreadable_middle_line_refuses_and_touches_nothing(tmp_path):
    """A bad line in the middle: exit 3, a named reason, file unchanged."""
    lines = list(CANONICAL_LINES)
    lines[5] = '{"entry_uuid": "s23", "kind": "sch'  # bad JSON, mid-file
    trail = write_trail(tmp_path, lines)
    before = trail.read_bytes()

    rc, out = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_UNREADABLE
    assert out["trail_status"] == ts.READ_UNREADABLE
    assert out["corrupt_line"] == 6
    assert "corrupt at line 6" in out["error"]
    # Named, not generic: it says WHAT is wrong with the line.
    assert "entry_uuid" in out["error"]
    assert trail.read_bytes() == before
    # And it did not answer the question anyway.
    assert "items" not in out


def test_a_truncated_final_line_is_not_unreadable(tmp_path):
    """A crash mid-write is recoverable and must not block a rollback."""
    text = "\n".join(CANONICAL_LINES) + "\n" + '{"entry_uuid": "c9", "kind'
    trail = tmp_path / "migration_trail.jsonl"
    trail.write_text(text, encoding="utf-8")
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_OK
    assert schema_item(plan)["backup_path"] == "cloude.db.bak-v2-20260104T000000Z"


def test_unverified_backup_is_treated_as_a_backup_that_does_not_exist(tmp_path):
    """backup_verified=0 refuses; it never degrades to "use it anyway"."""
    lines = [
        ln.replace('"backup_verified": 1', '"backup_verified": 0')
        if "bak-v2-" in ln else ln
        for ln in CANONICAL_LINES
    ]
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_REFUSED
    assert schema_item(plan)["outcome"] == ts.OUTCOME_CANNOT_DETERMINE
    assert "backup_verified=0" in schema_item(plan)["reason"]
    assert "cloude.db.bak-v2-20260104T000000Z" in schema_item(plan)["reason"]


def test_a_version_swallowed_by_a_jump_refuses_rather_than_approximating(tmp_path):
    """v2 exists only inside a 1->3 run, so no backup was taken at it."""
    lines = (
        step_pair("c1", "code", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
                  from_version="0.7.0", to_version="0.8.0")
        + step_pair("s01", "schema", "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:05Z", from_version="1", to_version="2",
                    backup_path="cloude.db.bak-v1-20260102T000000Z",
                    backup_verified=1)
        + step_pair("c2", "code", "2026-01-03T00:00:00Z",
                    "2026-01-03T00:01:00Z",
                    from_version="0.8.0", to_version="0.8.1")
        # One RUN spanning 2 -> 4. No backup was ever taken at 3.
        + step_pair("s24", "schema", "2026-01-04T00:00:00Z",
                    "2026-01-04T00:00:05Z", from_version="3", to_version="4",
                    backup_path="cloude.db.bak-v3-20260104T000000Z",
                    backup_verified=1)
    )
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_REFUSED
    reason = schema_item(plan)["reason"]
    assert "no backup was ever taken AT v2" in reason
    assert "started from v3" in reason
    assert "Refusing" in reason
    # It named no backup as a candidate.
    assert "backup_path" not in schema_item(plan)


def test_no_code_entry_for_the_target_refuses_and_lists_what_it_knows(tmp_path):
    """Asking for a release the trail never saw is answered, not guessed."""
    trail = write_trail(tmp_path, CANONICAL_LINES)
    rc, plan = run_cli(trail, "0.7.4")
    assert rc == ts.EXIT_REFUSED
    assert "no code entry arriving at 0.7.4" in plan["error"]
    assert plan["known_code_versions"] == ["0.8.0", "0.8.1", "0.8.2"]


def test_absent_trail_is_its_own_exit_code(tmp_path):
    """No trail is not a corrupt trail and not a clean one."""
    rc, out = run_cli(tmp_path / "nope.jsonl", "0.8.1")
    assert rc == ts.EXIT_ABSENT
    assert out["trail_status"] == ts.READ_ABSENT


def test_refusing_one_kind_refuses_the_whole_rollback(tmp_path):
    """Never half a restore: the two versions were only tested as a pair."""
    lines = list(CANONICAL_LINES) + step_pair(
        "cfg", "config", "2026-01-06T12:00:00Z", "2026-01-06T12:00:01Z",
        from_version="3", to_version="4",
        backup_path="config.json.bak-v3-20260106T120000Z", backup_verified=1)
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, "0.8.1")
    # schema resolves cleanly; config's only entry is AFTER the target.
    assert schema_item(plan)["outcome"] == ts.OUTCOME_RESTORE
    assert rc == ts.EXIT_REFUSED
    assert "Refusing the whole rollback" in plan["error"]


# --- the confirmation text ---------------------------------------------------


def test_confirmation_differs_for_two_different_steps(tmp_path):
    """Two targets, two prompts: the text is read, not written."""
    trail = write_trail(tmp_path, CANONICAL_LINES)
    _, plan_a = run_cli(trail, "0.8.1")
    _, plan_b = run_cli(trail, "0.8.2")
    text_a = plan_a["confirmation"]
    text_b = plan_b["confirmation"]

    assert text_a != text_b
    # Each names ITS OWN backup and ITS OWN timestamp, from the trail.
    assert "cloude.db.bak-v2-20260104T000000Z" in text_a
    assert "cloude.db.bak-v2-20260104T000000Z" not in text_b
    assert "cloude.db.bak-v3-20260106T000000Z" in text_b
    assert "2026-01-04T00:00:00Z" in text_a
    assert "2026-01-06T00:00:00Z" in text_b
    # Both name the loss and its irreversibility.
    for text in (text_a, text_b):
        assert "OVERWRITE live data" in text
        assert "is discarded" in text
        assert "This cannot be undone." in text


def test_confirmation_names_the_artifact_and_the_target_release(tmp_path):
    """The prompt says which file, which version, and when it was taken."""
    trail = write_trail(tmp_path, CANONICAL_LINES)
    _, plan = run_cli(trail, "0.8.1")
    text = plan["confirmation"]
    assert "cloude.db" in text
    assert "0.8.1" in text
    assert "schema v2" in text
    assert plan["target_code_started_at"] in text


# --- the real file format ----------------------------------------------------


REAL_SHAPE = [
    # Exactly the shape of the live trail on 2026-08-18: one entry per
    # migration RUN spanning the whole jump, backup_path only on the
    # closing line, and no config or code entries at all.
    line("boot", "bootstrap", "started", "2026-08-18T15:38:34.885731Z",
         from_version="0", to_version="1"),
    line("boot", "bootstrap", "completed", "2026-08-18T15:38:34.885731Z",
         from_version="0", to_version="1",
         completed_at="2026-08-18T15:38:34.896423Z"),
    line("s13", "schema", "started", "2026-08-18T18:41:12.980830Z",
         from_version="1", to_version="3"),
    line("s13", "schema", "completed", "2026-08-18T18:41:12.980830Z",
         from_version="1", to_version="3",
         completed_at="2026-08-18T18:41:12.996695Z",
         backup_path="cloude.db.bak-v1-20260818T184112Z", backup_verified=1),
    line("s34", "schema", "started", "2026-08-18T22:44:40.298360Z",
         from_version="3", to_version="4"),
    line("s34", "schema", "completed", "2026-08-18T22:44:40.298360Z",
         from_version="3", to_version="4",
         completed_at="2026-08-18T22:44:40.310717Z",
         backup_path="cloude.db.bak-v3-20260818T224440Z", backup_verified=1),
]


def test_backup_path_is_read_off_the_closing_line(tmp_path):
    """started carries the timestamp, completed carries the backup."""
    lines = REAL_SHAPE + step_pair(
        "code1", "code", "2026-08-18T20:00:00Z", "2026-08-18T20:01:00Z",
        from_version="0.8.1", to_version="0.8.2")
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, "0.8.2")
    assert rc == ts.EXIT_OK, plan
    item = schema_item(plan)
    # At 20:00 the schema was at 3 (the 1->3 run finished at 18:41), and
    # the backup taken AT v3 hangs off the 3->4 run that started later.
    assert item["version_at_target"] == "3"
    assert item["backup_path"] == "cloude.db.bak-v3-20260818T224440Z"


def test_the_live_trail_has_no_code_entries_so_it_cannot_anchor_a_rollback(
    tmp_path,
):
    """The gap this branch closes, asserted rather than described."""
    trail = write_trail(tmp_path, REAL_SHAPE)
    rc, plan = run_cli(trail, "0.8.1")
    assert rc == ts.EXIT_REFUSED
    assert plan["known_code_versions"] == []
    assert "no code entry arriving at 0.8.1" in plan["error"]


@pytest.mark.parametrize(
    "target,expected_version,expected_backup",
    [("0.8.2", "3", "cloude.db.bak-v3-20260818T224440Z"),
     ("0.8.1", "1", "cloude.db.bak-v1-20260818T184112Z")],
)
def test_two_code_points_over_the_real_shape_select_different_backups(
    tmp_path, target, expected_version, expected_backup
):
    """Rehearsal shape: each code point maps to its own era's backup.

    The 0.8.1 case is the one the `bootstrap` handling exists for: at
    17:00 the only entry on record is the bootstrap that created the
    database at v1, and a code point in that window is resolvable
    precisely because a bootstrap establishes a version.
    """
    lines = REAL_SHAPE + step_pair(
        "codeA", "code", "2026-08-18T17:00:00Z", "2026-08-18T17:01:00Z",
        from_version="0.8.0", to_version="0.8.1"
    ) + step_pair(
        "codeB", "code", "2026-08-18T20:00:00Z", "2026-08-18T20:01:00Z",
        from_version="0.8.1", to_version="0.8.2")
    trail = write_trail(tmp_path, lines)
    rc, plan = run_cli(trail, target)
    assert rc == ts.EXIT_OK, plan
    assert schema_item(plan)["version_at_target"] == expected_version
    assert schema_item(plan)["backup_path"] == expected_backup
