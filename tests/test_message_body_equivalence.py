"""Tests for what the duplicate-uuid gate is allowed to call the same message.

THE ASYMMETRY THESE TESTS EXIST FOR. Under-normalising costs a longer
review queue and nothing else. Over-normalising destroys the evidence
that a transcript was edited after it was written, silently and
permanently, because the finding is never raised and nothing downstream
can notice its absence. So for every rule there are two tests: one that
the measured benign class collapses, and one that a GENUINE difference
sitting on or beside that same path still gates.

THE NAMED REGRESSION. uuid 769c6599-5116-4388-be8a-d719699deb67 has two
copies in session 2918 of the owner's live history that differ at exactly
one json path, message.content[0].input.command - one copy carrying a
credential that was later rotated, the other edited to a redaction. That
pair is the reason this gate exists at all, and the shape is reproduced
here (with plainly non-credential text, so no fixture in this repo has to
look like a secret). It must gate, and it must still gate when the two
copies ALSO differ in every recording-context field the rules absorb,
which is the realistic case: an edited record is usually also a replay.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_body_equivalence import (
    ABSENT,
    EQUIVALENCE_RULES,
    NOT_NORMALISED,
    RULE_KINDS,
    bodies_equivalent,
    canonical_identity,
    difference_paths,
    duplicate_verdict,
    normalise_body,
)
from src.core.message_gate_contract import (
    BY_CODE,
    GATE_DUPLICATE_UUID_BODY_CONFLICT,
    GATE_DUPLICATE_UUID_RECORDING_VARIANT,
    SEVERITY_ADVISORY,
    SEVERITY_STOP,
)
from src.core.message_model_ingest import ingest_text

REGRESSION_UUID = "769c6599-5116-4388-be8a-d719699deb67"


def assistant(command: str, **overrides) -> dict:
    """One assistant record shaped like the named regression pair.

    Description: a tool_use content block whose input carries a shell
      command, which is the exact path the live pair differs at. Keyword
      overrides are merged at the top level so a test can vary the
      recording context around an unchanged message.
    Inputs: command (str), overrides (top level fields).
    Output: dict - a parsed transcript record.
    Example: assistant("ls")["uuid"] == REGRESSION_UUID -> True
    """
    body = {
        "type": "assistant",
        "uuid": REGRESSION_UUID,
        "parentUuid": "b5ff1ec3-ce09-4a1f-b393-be0f053b8b26",
        "timestamp": "2026-06-26T15:43:56.868Z",
        "sessionId": "2918-a",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "tool_use", "id": "toolu_x", "name": "Bash",
                 "input": {"command": command}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1027},
        },
    }
    body.update(overrides)
    return body


# ---- the declaration itself --------------------------------------------

def test_every_rule_names_a_known_kind_and_a_measured_class():
    """A rule with no measured class is the defect this table forbids."""
    for rule in EQUIVALENCE_RULES:
        assert rule.kind in RULE_KINDS
        assert rule.groups > 0, f"{rule.path} cites no measured groups"
        assert len(rule.justification) > 60, rule.path


def test_the_rule_paths_are_unique():
    paths = [rule.path for rule in EQUIVALENCE_RULES]
    assert len(paths) == len(set(paths))


def test_the_deliberate_omissions_are_written_down():
    """An omission nobody recorded reads as an oversight next time."""
    assert {entry[0] for entry in NOT_NORMALISED} >= {"parentUuid"}
    for _, _, reason in NOT_NORMALISED:
        assert reason


def test_both_duplicate_conditions_are_registered_with_the_right_severity():
    assert BY_CODE[GATE_DUPLICATE_UUID_BODY_CONFLICT].severity == SEVERITY_STOP
    assert BY_CODE[
        GATE_DUPLICATE_UUID_RECORDING_VARIANT].severity == SEVERITY_ADVISORY


# ---- purity ------------------------------------------------------------

def test_normalising_does_not_mutate_the_caller_s_body():
    """The caller stores this value byte-exactly right after asking."""
    body = assistant("ls", sessionId="s1", cwd="/a")
    before = json.dumps(body, sort_keys=True)
    normalise_body(body)
    assert json.dumps(body, sort_keys=True) == before


def test_a_non_object_line_is_returned_untouched():
    """No rule addresses a shape with no keys, so none is invented."""
    assert normalise_body("bare string") == "bare string"
    assert normalise_body([1, 2]) == [1, 2]


def test_key_order_is_not_a_difference():
    assert canonical_identity({"b": 1, "a": 2}) == canonical_identity(
        {"a": 2, "b": 1})


# ---- the named regression ----------------------------------------------

def test_the_769c6599_pair_still_gates():
    """The live pair differs at message.content[0].input.command only."""
    left = assistant("cat /some/file")
    right = assistant("cat /some/other/file")
    assert difference_paths([left, right]) == [
        "message.content[0].input.command"]
    assert not bodies_equivalent(left, right)


def test_the_769c6599_pair_still_gates_when_it_is_also_a_replay():
    """An edited record is usually ALSO a resume or fork copy. Every
    recording field below is absorbed; the command difference is not."""
    left = assistant("cat /some/file", sessionId="s1", slug="a",
                     version="2.1.73", cwd="/a", gitBranch="main",
                     promptId="p1", entrypoint="cli")
    right = assistant("cat /some/other/file", sessionId="s2", slug="b",
                      version="2.1.99", cwd="/b", gitBranch="dev",
                      promptId="p2", forkedFrom={"sessionId": "s1"})
    right["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
    right["message"]["stop_reason"] = None
    assert not bodies_equivalent(left, right)


def test_the_verdict_detail_names_paths_and_never_values():
    """The detail is written to the findings table, and a body can hold
    credential material."""
    left = assistant("cat /secret/path/alpha")
    right = assistant("cat /secret/path/beta")
    verdict = duplicate_verdict(left, [right], REGRESSION_UUID)
    assert verdict.code == GATE_DUPLICATE_UUID_BODY_CONFLICT
    assert "message.content[0].input.command" in verdict.detail
    assert "alpha" not in verdict.detail
    assert "beta" not in verdict.detail


# ---- each rule collapses its measured class ----------------------------

@pytest.mark.parametrize("key, left, right", [
    ("sessionId", "s1", "s2"),
    ("slug", "starry-roaming-pizza", ABSENT),
    ("version", "2.1.73", "2.1.74"),
    ("promptId", "p1", "p2"),
    ("gitBranch", "feat/x", "main"),
    ("cwd", "/a/app", "/a"),
    ("entrypoint", "cli", ABSENT),
    ("sourceToolAssistantUUID", "7a8ef584", ABSENT),
])
def test_a_recording_context_key_difference_is_absorbed(key, left, right):
    one = assistant("ls")
    two = assistant("ls")
    one[key] = left
    if right is not ABSENT:
        two[key] = right
    assert bodies_equivalent(one, two)


def test_a_forked_from_block_present_on_only_one_copy_is_absorbed():
    one = assistant("ls")
    two = assistant("ls", forkedFrom={"sessionId": "s1",
                                      "messageUuid": REGRESSION_UUID})
    assert bodies_equivalent(one, two)


def test_an_attachment_display_path_is_absorbed_but_the_attachment_is_not():
    one = assistant("ls", attachment={"displayPath": "../a", "id": "at1"})
    two = assistant("ls", attachment={"id": "at1"})
    assert bodies_equivalent(one, two)
    three = assistant("ls", attachment={"displayPath": "../a", "id": "at2"})
    assert not bodies_equivalent(one, three)


def test_a_streaming_snapshot_matches_its_completed_message():
    """stop_reason null with partial usage against the finished pair -
    426 groups, every one with null on a side, zero with two values."""
    snapshot = assistant("ls")
    snapshot["message"]["stop_reason"] = None
    snapshot["message"]["usage"] = {"input_tokens": 1, "output_tokens": 2,
                                    "inference_geo": "not_available"}
    done = assistant("ls")
    assert bodies_equivalent(snapshot, done)


def test_a_null_context_management_matches_an_absent_one():
    one = assistant("ls")
    one["message"]["context_management"] = None
    assert bodies_equivalent(one, assistant("ls"))


def test_two_POPULATED_context_management_values_still_gate():
    """drop_if_null, not drop - the narrow rule the measurement bought."""
    one = assistant("ls")
    two = assistant("ls")
    one["message"]["context_management"] = {"edits": 1}
    two["message"]["context_management"] = {"edits": 2}
    assert not bodies_equivalent(one, two)


def test_a_bare_content_string_matches_its_single_text_block():
    one = {"type": "user", "uuid": "u", "message": {"content": "hello"}}
    two = {"type": "user", "uuid": "u",
           "message": {"content": [{"type": "text", "text": "hello"}]}}
    assert bodies_equivalent(one, two)


# ---- and each rule stays narrow ----------------------------------------

def test_an_extra_content_block_is_a_real_difference():
    """55 of the 73 measured content-differing groups are this, and a
    measured example adds exactly one block reading 'Tool loaded.'"""
    one = {"type": "user", "uuid": "u",
           "message": {"content": [{"type": "text", "text": "hello"}]}}
    two = copy.deepcopy(one)
    two["message"]["content"].append({"type": "text", "text": "Tool loaded."})
    assert not bodies_equivalent(one, two)


def test_differing_text_inside_a_single_block_is_a_real_difference():
    one = {"type": "user", "uuid": "u", "message": {"content": "hello"}}
    two = {"type": "user", "uuid": "u",
           "message": {"content": [{"type": "text", "text": "hello there"}]}}
    assert not bodies_equivalent(one, two)


def test_two_different_parents_under_one_uuid_still_gate():
    """1,474 of the 1,494 parentUuid groups, and 96 percent of what is
    left in the queue. This is the graph differing, not the recording."""
    one = assistant("ls", parentUuid="p1")
    two = assistant("ls", parentUuid="p2")
    assert not bodies_equivalent(one, two)


def test_a_parent_against_null_still_gates():
    one = assistant("ls", parentUuid="p1")
    two = assistant("ls", parentUuid=None)
    assert not bodies_equivalent(one, two)


def test_a_key_the_table_says_nothing_about_still_gates():
    """The rules are a closed list, not a heuristic about field names."""
    assert not bodies_equivalent(assistant("ls", toolUseResult="a"),
                                 assistant("ls", toolUseResult="b"))
    assert not bodies_equivalent(assistant("ls", summary="a"),
                                 assistant("ls", summary="b"))
    assert not bodies_equivalent(assistant("ls", userType="external"),
                                 assistant("ls", userType="internal"))


def test_a_dropped_key_does_not_take_its_neighbours_with_it():
    """message.usage goes; message.model, sitting beside it, does not."""
    one = assistant("ls")
    two = assistant("ls")
    two["message"]["model"] = "claude-opus-4-1"
    assert not bodies_equivalent(one, two)


def test_identical_bodies_are_not_a_finding_of_any_kind():
    assert duplicate_verdict(assistant("ls"), [assistant("ls")],
                             REGRESSION_UUID) is None


# ---- what the store does with the verdict ------------------------------

@pytest.fixture()
def conn():
    """An in-memory database migrated to the current schema version."""
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def _line(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n"


def test_a_recording_variant_is_stored_twice_and_reported_as_advisory(conn):
    """The whole point: BOTH bodies stay, and the queue does not grow."""
    one = assistant("ls", sessionId="s1")
    two = assistant("ls", sessionId="s2", version="2.1.99")
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=_line(one))
        second = ingest_text(conn, source_ref="b", session_ref="s2",
                             text=_line(two))
    assert GATE_DUPLICATE_UUID_RECORDING_VARIANT in second.codes()
    assert GATE_DUPLICATE_UUID_BODY_CONFLICT not in second.codes()
    assert conn.execute(
        "SELECT COUNT(*) FROM message_bodies WHERE message_uuid = ?",
        (REGRESSION_UUID,)).fetchone()[0] == 2


def test_a_genuine_conflict_is_stored_twice_and_still_stops(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1",
                    text=_line(assistant("cat /some/file", sessionId="s1")))
        second = ingest_text(
            conn, source_ref="b", session_ref="s2",
            text=_line(assistant("cat /other/file", sessionId="s2")))
    assert GATE_DUPLICATE_UUID_BODY_CONFLICT in second.codes()
    assert conn.execute(
        "SELECT COUNT(*) FROM message_bodies WHERE message_uuid = ?",
        (REGRESSION_UUID,)).fetchone()[0] == 2


def test_the_advisory_severity_is_what_lands_in_the_findings_table(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1",
                    text=_line(assistant("ls", sessionId="s1")))
        ingest_text(conn, source_ref="b", session_ref="s2",
                    text=_line(assistant("ls", sessionId="s2")))
    rows = conn.execute(
        "SELECT condition_code, severity FROM message_ingest_findings "
        "WHERE condition_code = ?",
        (GATE_DUPLICATE_UUID_RECORDING_VARIANT,)).fetchall()
    assert rows and all(row[1] == SEVERITY_ADVISORY for row in rows)
