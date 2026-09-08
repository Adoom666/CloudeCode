"""Tests for the conversation-turns view over one transcript.

WHAT EACH TEST HERE IS GUARDING. Every assertion below corresponds to a
measured property of the real 17 GB corpus, not to a hypothetical:

  * role is NULL on 1,099,537 of 2,447,028 bodies (44.93 percent), so
    the record_type fallback is the COMMON path and gets its own test.
  * ts is NULL on 33,480 bodies, so a turn must render without one.
  * is_error is NULL on 1,075,007 of 1,348,227 blocks - the key was
    ABSENT - and defaulting it to False would assert success about a
    block that claimed nothing.
  * the subagent spawn linkage resolves 96.04 percent of 19,629 corpus
    spawns, so an unresolved spawn must still appear, and the three
    block-status outcomes must render distinguishably.

Fixture archives only. The real corpus is 17 GB and a test that read it
would measure whatever happens to be in it today.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from src.core.archive_read import open_read_only
from src.core.archive_snippet_gate import SNIPPET_INCLUDED
from src.core.archive_turn_blocks import (
    BLOCKS_EXTRACTED,
    BLOCKS_NEVER_PROCESSED,
    BLOCKS_NONE,
    BLOCKS_UNPARSEABLE,
)
from src.core.archive_turn_subagents import (
    LINK_NO_AGENT_ID,
    LINK_NO_TOOL_RESULT,
    LINK_RESOLVED,
    ORDER_BY_FILE_POSITION,
    ORDER_BY_START_TS,
    SUBAGENTS_CANNOT_DETERMINE,
    SUBAGENTS_NONE_SPAWNED,
    SUBAGENTS_PARTIAL,
    SUBAGENTS_RESOLVED,
    parse_agent_ids,
)
from src.core.archive_turns import (
    ROLE_FROM_RECORD_TYPE,
    ROLE_FROM_ROLE,
    ROLE_NONE,
    ROLE_UNKNOWN_LABEL,
    resolve_role,
    transcript_turns,
)
from tests.archive_fixture import (
    make_state_dir,
    seed_body,
    seed_corpus,
    seed_host,
    seed_project,
    seed_secret_finding,
    seed_transcript,
    writable,
)
from tests.archive_turn_fixture import (
    seed_appearance_agent,
    seed_block,
    seed_block_status,
    seed_body_typed,
)

#: A credential-shaped string used ONLY as gate bait. It is a literal in
#: a test file and is not a real credential; the point is that the gate
#: must withhold it, so it has to look like one.
FAKE_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


def _line(
    conn: sqlite3.Connection,
    transcript_id: int,
    line_no: int,
    body_id: int,
    **kwargs: Any,
) -> None:
    """Attach one body to one line of a transcript.

    Inputs: conn, transcript_id (int), line_no (int), body_id (int),
      **kwargs forwarded to seed_appearance_agent.
    Output: None.
    Example: _line(conn, 1, 0, 5)
    """
    seed_appearance_agent(
        conn, transcript_id=transcript_id, line_no=line_no,
        body_id=body_id, **kwargs,
    )


@pytest.fixture()
def archive(tmp_path):
    """Build a fixture archive exercising every turn state at once.

    Description: ONE transcript carrying, in line order: a normal
      assistant turn with text; a NULL-role NULL-ts turn; a turn whose
      body was never processed; a turn genuinely without content; an
      unparseable turn; a tool_use paired to a tool_result with
      is_error absent; a secret-bearing block; and two subagent spawns,
      one resolvable and one whose result carries no agentId.
    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: dict of the seeded ids, plus ``state_dir``.
    Example: archive["transcript_id"]
    """
    state_dir = make_state_dir(tmp_path)
    conn = writable(state_dir)
    facts: Dict[str, Any] = {}
    try:
        with conn:
            host = seed_host(conn, name="testbox")
            corpus = seed_corpus(conn, host)
            project = seed_project(conn, corpus, slug="p")
            main = seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=project,
                source_path="main.jsonl", line_count=9,
            )
            facts["transcript_id"] = main

            # line 0: an ordinary assistant turn with two text blocks.
            plain = seed_body_typed(
                conn, body_json='{"message":{"usage":{"input_tokens":11,'
                                '"output_tokens":22}}}',
                role="assistant", record_type="assistant",
                model="nemotron-3-super", identity_key="plain",
            )
            seed_block_status(
                conn, body_id=plain, status="blocks_extracted", block_count=2
            )
            seed_block(conn, body_id=plain, seq=0, block_type="thinking",
                       text="pondering")
            seed_block(conn, body_id=plain, seq=1, block_type="text",
                       text="hello world")
            _line(conn, main, 0, plain)
            facts["plain_body"] = plain

            # line 1: NULL role, NULL ts - the 44.93 percent case.
            nullish = seed_body(
                conn, body_json="{}", ts=None, identity_key="nullish"
            )
            seed_block_status(
                conn, body_id=nullish, status="no_message_content"
            )
            _line(conn, main, 1, nullish)
            facts["nullish_body"] = nullish

            # line 2: record_type present, role absent - the fallback.
            progress = seed_body_typed(
                conn, body_json="{}", record_type="progress",
                identity_key="progress",
            )
            seed_block_status(
                conn, body_id=progress, status="blocks_extracted",
                block_count=1,
            )
            seed_block(conn, body_id=progress, seq=0,
                       block_type="_string_content", text="a bare string")
            _line(conn, main, 2, progress)
            facts["progress_body"] = progress

            # line 3: NEVER PROCESSED - deliberately no status row.
            unseen = seed_body(conn, body_json="{}", identity_key="unseen")
            _line(conn, main, 3, unseen)
            facts["unseen_body"] = unseen

            # line 4: the extractor looked and failed.
            broken = seed_body(
                conn, body_json="not json", identity_key="broken"
            )
            seed_block_status(
                conn, body_id=broken, status="unparseable_body",
                detail="json decode failed at byte 0",
            )
            _line(conn, main, 4, broken)
            facts["broken_body"] = broken

            # line 5: a tool_use, is_error ABSENT.
            call = seed_body_typed(
                conn, body_json="{}", role="assistant", identity_key="call"
            )
            seed_block_status(
                conn, body_id=call, status="blocks_extracted", block_count=1
            )
            seed_block(conn, body_id=call, seq=0, block_type="tool_use",
                       text='{"cmd":"ls"}', tool_name="Bash",
                       tool_use_id="toolu_call1")
            _line(conn, main, 5, call)
            facts["call_body"] = call

            # line 6: its tool_result, is_error TRUE.
            answer = seed_body_typed(
                conn, body_json="{}", role="user", identity_key="answer"
            )
            seed_block_status(
                conn, body_id=answer, status="blocks_extracted", block_count=1
            )
            seed_block(conn, body_id=answer, seq=0, block_type="tool_result",
                       text="no such file", tool_use_id="toolu_call1",
                       is_error=True)
            _line(conn, main, 6, answer)
            facts["answer_body"] = answer

            # line 7: a block whose text must be WITHHELD by the gate.
            leaky_json = '{"k":"' + FAKE_SECRET + '"}'
            leaky = seed_body_typed(
                conn, body_json=leaky_json, role="assistant",
                secret_finding_count=1, identity_key="leaky",
            )
            seed_secret_finding(
                conn, body_id=leaky,
                match_offset=leaky_json.index(FAKE_SECRET),
                match_length=len(FAKE_SECRET),
            )
            seed_block_status(
                conn, body_id=leaky, status="blocks_extracted", block_count=1
            )
            seed_block(conn, body_id=leaky, seq=0, block_type="text",
                       text=FAKE_SECRET + " trailing context")
            _line(conn, main, 7, leaky)
            facts["leaky_body"] = leaky

            # Two subagent transcripts. The FIRST has the LATER start
            # time, so an implementation that ordered by spawn position
            # instead of by ts would get the order wrong.
            late = seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=project,
                source_path="agent-alate.jsonl", session_ref_scheme="agent",
                line_count=1,
            )
            conn.execute(
                "UPDATE message_transcripts SET session_ref = ? WHERE id = ?",
                ("agent-alate", late),
            )
            late_body = seed_body_typed(
                conn, body_json="{}", ts="2025-12-29T09:00:00.000Z",
                identity_key="latebody",
            )
            seed_appearance_agent(
                conn, transcript_id=late, line_no=0, body_id=late_body,
                is_sidechain=True, agent_id="alate",
            )
            facts["late_transcript"] = late

            early = seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=project,
                source_path="agent-aearly.jsonl", session_ref_scheme="agent",
                line_count=1,
            )
            conn.execute(
                "UPDATE message_transcripts SET session_ref = ? WHERE id = ?",
                ("agent-aearly", early),
            )
            early_body = seed_body_typed(
                conn, body_json="{}", ts="2025-12-29T07:00:00.000Z",
                identity_key="earlybody",
            )
            seed_appearance_agent(
                conn, transcript_id=early, line_no=0, body_id=early_body,
                is_sidechain=True, agent_id="aearly",
            )
            facts["early_transcript"] = early

            # line 8: ONE turn spawning THREE runs - the late one first
            # in file order, the early one second, and a third whose
            # tool_result carries no agentId at all.
            spawner = seed_body_typed(
                conn, body_json="{}", role="assistant",
                identity_key="spawner",
            )
            seed_block_status(
                conn, body_id=spawner, status="blocks_extracted",
                block_count=3,
            )
            for seq, (tuid, name) in enumerate((
                ("toolu_late", "Agent"),
                ("toolu_early", "Agent"),
                ("toolu_mystery", "Task"),
            )):
                seed_block(conn, body_id=spawner, seq=seq,
                           block_type="tool_use", text="{}", tool_name=name,
                           tool_use_id=tuid)
            _line(conn, main, 8, spawner)
            facts["spawner_body"] = spawner

            results = seed_body_typed(
                conn, body_json="{}", role="user", identity_key="results"
            )
            seed_block_status(
                conn, body_id=results, status="blocks_extracted",
                block_count=3,
            )
            seed_block(conn, body_id=results, seq=0,
                       block_type="tool_result", tool_use_id="toolu_late",
                       text="done\nagentId: alate (use SendMessage)")
            seed_block(conn, body_id=results, seq=1,
                       block_type="tool_result", tool_use_id="toolu_early",
                       text="done\nagentId: aearly (use SendMessage)")
            seed_block(conn, body_id=results, seq=2,
                       block_type="tool_result", tool_use_id="toolu_mystery",
                       text="finished, no id in this older format")
            _line(conn, main, 9, results)
    finally:
        conn.close()
    facts["state_dir"] = state_dir
    return facts


def _turns(archive: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """Run the turns view against the fixture archive.

    Inputs: archive (dict) - the fixture, **kwargs for transcript_turns.
    Output: the envelope dict.
    Example: _turns(archive, limit=2)["result_status"] -> 'ok'
    """
    conn = open_read_only(archive["state_dir"])
    try:
        return transcript_turns(conn, archive["transcript_id"], **kwargs)
    finally:
        conn.close()


def _by_line(env: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Index a page's turns by line_no.

    Inputs: env (dict) - the envelope. Output: dict.
    Example: _by_line(env)[0]["role"] -> 'assistant'
    """
    return {t["line_no"]: t for t in env["result"]}


# --- Turn assembly: the NULL fallbacks ------------------------------------


def test_role_falls_back_to_record_type_and_then_says_so():
    """resolve_role names its source in all three cases."""
    assert resolve_role("assistant", "assistant")["role_state"] == ROLE_FROM_ROLE
    fallback = resolve_role(None, "progress")
    assert fallback["role"] == "progress"
    assert fallback["role_state"] == ROLE_FROM_RECORD_TYPE
    nothing = resolve_role(None, None)
    assert nothing["role"] == ROLE_UNKNOWN_LABEL
    assert nothing["role_state"] == ROLE_NONE


def test_null_role_and_null_ts_render_without_inventing_either(archive):
    """A NULL-role NULL-ts body is a turn, labelled, with ts still null."""
    turns = _by_line(_turns(archive, limit=50))
    nullish = turns[1]
    assert nullish["ts"] is None, "a missing timestamp must not be invented"
    assert nullish["role_state"] == ROLE_NONE
    assert nullish["role"] == ROLE_UNKNOWN_LABEL
    assert turns[2]["role_state"] == ROLE_FROM_RECORD_TYPE
    assert turns[2]["role"] == "progress"
    assert turns[0]["role_state"] == ROLE_FROM_ROLE
    # A model value that is not a Claude model must survive untouched.
    assert turns[0]["model"] == "nemotron-3-super"


# --- Blocks ----------------------------------------------------------------


def test_each_block_type_is_carried_with_its_own_fields(archive):
    """thinking, text, _string_content, tool_use and tool_result all land."""
    turns = _by_line(_turns(archive, limit=50))
    kinds = [b["type"] for b in turns[0]["blocks"]]
    assert kinds == ["thinking", "text"]
    assert turns[0]["blocks"][1]["text"] == "hello world"
    assert turns[0]["blocks"][1]["text_length"] == len("hello world")
    assert turns[2]["blocks"][0]["type"] == "_string_content"
    assert turns[5]["blocks"][0]["type"] == "tool_use"
    assert turns[5]["blocks"][0]["tool_name"] == "Bash"


def test_tool_use_pairs_to_its_tool_result_by_id(archive):
    """The call names the id it issued; the answer names the id it answers."""
    turns = _by_line(_turns(archive, limit=50))
    assert turns[5]["tool_use"]["calls"] == ["toolu_call1"]
    assert turns[5]["tool_use"]["results"] == []
    assert turns[6]["tool_use"]["results"] == ["toolu_call1"]
    assert turns[6]["blocks"][0]["tool_use_id"] == "toolu_call1"


def test_is_error_absent_stays_null_and_is_never_defaulted(archive):
    """NULL is_error is the majority case and must not render as False."""
    turns = _by_line(_turns(archive, limit=50))
    call_block = turns[5]["blocks"][0]
    assert call_block["is_error"] is None, (
        "is_error NULL means the key was ABSENT; False would assert success"
    )
    assert turns[6]["blocks"][0]["is_error"] is True


def test_the_three_block_status_outcomes_render_differently(archive):
    """Genuinely-empty, never-processed and unparseable are three states."""
    turns = _by_line(_turns(archive, limit=50))
    assert turns[1]["blocks_state"] == BLOCKS_NONE
    assert turns[1]["blocks_complete"] is True
    assert turns[3]["blocks_state"] == BLOCKS_NEVER_PROCESSED
    assert turns[3]["blocks_complete"] is False
    assert turns[4]["blocks_state"] == BLOCKS_UNPARSEABLE
    assert turns[4]["blocks_complete"] is False
    assert turns[0]["blocks_state"] == BLOCKS_EXTRACTED
    # All three produce an EMPTY blocks list, which is exactly why the
    # state field has to exist: len(blocks) cannot tell them apart.
    assert turns[1]["blocks"] == turns[3]["blocks"] == turns[4]["blocks"] == []


def test_unmeasurable_turns_reach_the_envelope_unevaluated_block(archive):
    """A per-turn cannot-determine is not allowed to stay per-turn."""
    env = _turns(archive, limit=50)
    subjects = " ".join(u["subject"] for u in env["unevaluated"])
    assert f"body:{archive['unseen_body']} blocks" in subjects
    assert f"body:{archive['broken_body']} blocks" in subjects


# --- The gate --------------------------------------------------------------


def test_secret_bearing_block_is_withheld_but_still_present(archive):
    """Withholding hides the text and never the block or its length."""
    turns = _by_line(_turns(archive, limit=50))
    block = turns[7]["blocks"][0]
    assert block["text"] is None, "gated text must not be served"
    assert block["text_state"] != SNIPPET_INCLUDED
    assert block["type"] == "text"
    assert block["text_length"] == len(FAKE_SECRET + " trailing context")
    assert FAKE_SECRET not in repr(turns[7]), (
        "no part of the turn may carry the matched value"
    )


def test_include_text_false_withholds_every_preview_by_request(archive):
    """Opting out is a named withhold, not a missing field."""
    turns = _by_line(_turns(archive, limit=50, include_text=False))
    for block in turns[0]["blocks"]:
        assert block["text"] is None
        assert block["text_state"] == "withheld_by_request"
        assert block["text_length"] > 0


# --- Subagents -------------------------------------------------------------


def test_parse_agent_ids_reads_every_id_in_a_fragment():
    """More than one agentId in one result is real; the first is not enough."""
    assert parse_agent_ids("agentId: a1f (use SendMessage)") == ["a1f"]
    assert parse_agent_ids("agentId: a1 x agentId: a2") == ["a1", "a2"]
    assert parse_agent_ids(None) == []
    assert parse_agent_ids("no id here") == []


def test_subagents_are_time_ordered_with_an_explicit_order_field(archive):
    """The client never sorts: order is 1-based and ts-driven."""
    turns = _by_line(_turns(archive, limit=50))
    entries = turns[8]["subagents"]
    assert [e["order"] for e in entries] == [1, 2, 3]
    resolved = [e for e in entries if e["link_state"] == LINK_RESOLVED]
    assert len(resolved) == 2
    # aearly starts at 07:00 and was spawned SECOND; alate starts at
    # 09:00 and was spawned FIRST. Time order must beat file order.
    assert resolved[0]["agent_ids"] == ["aearly"]
    assert resolved[0]["order"] == 1
    assert resolved[0]["order_basis"] == ORDER_BY_START_TS
    assert resolved[1]["agent_ids"] == ["alate"]
    assert resolved[0]["transcripts"][0]["transcript_id"] == (
        archive["early_transcript"]
    )


def test_a_subagent_with_no_timestamp_falls_last_and_says_why(archive):
    """A missing start time is stated as file position, never invented."""
    turns = _by_line(_turns(archive, limit=50))
    entries = turns[8]["subagents"]
    unlinked = [e for e in entries if e["link_state"] == LINK_NO_AGENT_ID]
    assert len(unlinked) == 1
    assert unlinked[0]["order"] == 3, "an unordered entry sorts to the end"
    assert unlinked[0]["order_basis"] == ORDER_BY_FILE_POSITION
    assert unlinked[0]["start_ts"] is None
    assert unlinked[0]["transcripts"] == []
    # It is STILL RETURNED. Dropping it would make a turn that spawned
    # three runs look like one that spawned two.
    assert len(entries) == 3


def test_a_turn_with_spawns_that_none_resolve_is_not_reported_as_none(archive):
    """cannot_determine and none_spawned must never collapse together."""
    turns = _by_line(_turns(archive, limit=50))
    assert turns[8]["subagents_state"] == SUBAGENTS_PARTIAL
    assert turns[0]["subagents_state"] == SUBAGENTS_NONE_SPAWNED
    assert turns[0]["subagents"] == []
    env = _turns(archive, limit=50)
    reasons = " ".join(u["reason"] for u in env["unevaluated"])
    assert "could not be linked" in reasons


def test_a_missing_tool_result_is_distinct_from_one_lacking_an_agent_id(
    tmp_path,
):
    """0.17 percent of corpus spawns have NO result; 3.79 have one with no id."""
    state_dir = make_state_dir(tmp_path)
    conn = writable(state_dir)
    try:
        with conn:
            host = seed_host(conn, name="h")
            corpus = seed_corpus(conn, host)
            tid = seed_transcript(
                conn, host_id=host, corpus_id=corpus, project_id=None,
                source_path="t.jsonl", line_count=1,
            )
            body = seed_body_typed(
                conn, body_json="{}", role="assistant", identity_key="lonely"
            )
            seed_block_status(
                conn, body_id=body, status="blocks_extracted", block_count=1
            )
            seed_block(conn, body_id=body, seq=0, block_type="tool_use",
                       text="{}", tool_name="Agent",
                       tool_use_id="toolu_orphan")
            seed_appearance_agent(
                conn, transcript_id=tid, line_no=0, body_id=body
            )
    finally:
        conn.close()
    conn = open_read_only(state_dir)
    try:
        env = transcript_turns(conn, tid, limit=10)
    finally:
        conn.close()
    turn = env["result"][0]
    assert turn["subagents_state"] == SUBAGENTS_CANNOT_DETERMINE
    assert len(turn["subagents"]) == 1
    assert turn["subagents"][0]["link_state"] == LINK_NO_TOOL_RESULT


def test_a_turn_whose_every_spawn_resolves_reports_resolved(archive):
    """The healthy state needs its own assertion, not just the broken ones.

    Description: ``partial`` and ``cannot_determine`` are tested above.
      Without this, a bug that made ``resolved`` unreachable - so that
      every turn reported partial forever - would pass the whole file.
      A state nothing asserts is a state nothing guards.
    """
    conn = open_read_only(archive["state_dir"])
    try:
        env = transcript_turns(conn, archive["transcript_id"], limit=50)
    finally:
        conn.close()
    turn = _by_line(env)[8]
    resolved_only = [
        e for e in turn["subagents"] if e["link_state"] == LINK_RESOLVED
    ]
    # Every RESOLVED entry must carry a real transcript, and the state
    # machine must be able to reach SUBAGENTS_RESOLVED at all.
    assert resolved_only and all(e["transcripts"] for e in resolved_only)
    from src.core.archive_turn_subagents import order_subagents, resolve_spawns

    spawns = [
        e["spawned_by"] for e in resolved_only
    ]
    conn = open_read_only(archive["state_dir"])
    try:
        only_good = resolve_spawns(conn, [
            {"body_id": s["body_id"], "seq": s["block_seq"],
             "line_no": s["line_no"], "tool_name": s["tool_name"],
             "tool_use_id": s["tool_use_id"]}
            for s in spawns
        ])
    finally:
        conn.close()
    state = only_good[archive["spawner_body"]]["state"]
    assert state == SUBAGENTS_RESOLVED, (
        "a turn whose every spawn links must report resolved, not partial"
    )
    assert [e["order"] for e in order_subagents(
        only_good[archive["spawner_body"]]["entries"]
    )] == [1, 2]


def test_a_subagent_transcript_says_that_it_is_one(archive):
    """Opening a sidechain must not require guessing from the source path."""
    conn = open_read_only(archive["state_dir"])
    try:
        env = transcript_turns(conn, archive["early_transcript"], limit=10)
        parent = transcript_turns(conn, archive["transcript_id"], limit=1)
    finally:
        conn.close()
    assert env["meta"]["scope"]["is_subagent_transcript"] is True
    assert parent["meta"]["scope"]["is_subagent_transcript"] is False


def test_every_resolved_subagent_carries_a_drill_in_href(archive):
    """Recursive drill-down is a link, not a second lookup by the client."""
    turns = _by_line(_turns(archive, limit=50))
    resolved = [
        e for e in turns[8]["subagents"] if e["link_state"] == LINK_RESOLVED
    ]
    for entry in resolved:
        target = entry["transcripts"][0]
        assert target["messages_href"].endswith(
            f"/transcripts/{target['transcript_id']}/messages"
        )


# --- info ------------------------------------------------------------------


def test_info_carries_the_envelope_detail_and_usage_states(archive):
    """Usage is recorded, not_recorded or cannot_determine - never zeros."""
    turns = _by_line(_turns(archive, limit=50))
    info = turns[0]["info"]
    assert info["usage"]["state"] == "recorded"
    assert info["usage"]["input_tokens"] == 11
    assert info["usage"]["output_tokens"] == 22
    assert info["message_uuid"] is not None
    assert info["line"]["line_no"] == 0
    assert turns[1]["info"]["usage"]["state"] == "not_recorded"
    assert turns[1]["info"]["usage"]["input_tokens"] is None
    assert turns[4]["info"]["usage"]["state"] == "cannot_determine"


# --- Paging ----------------------------------------------------------------


def test_paging_walks_every_turn_exactly_once(archive):
    """The keyset walk is the /lines walk; nothing is skipped or repeated."""
    seen = []
    cursor = None
    for _ in range(20):
        env = _turns(archive, limit=3, cursor=cursor)
        assert env["result_status"] == "ok"
        seen.extend(t["line_no"] for t in env["result"])
        cursor = env["meta"]["paging"]["next_cursor"]
        if not env["meta"]["paging"]["has_more"]:
            break
    assert seen == sorted(seen), "the walk must be monotonic"
    assert len(seen) == len(set(seen)), "no line may be returned twice"
    assert seen == list(range(10))


def test_start_line_opens_the_page_and_refuses_a_cursor_at_the_same_time(
    archive,
):
    """Same start_line contract as /lines, resolved by the same module."""
    env = _turns(archive, limit=2, start_line=5)
    assert [t["line_no"] for t in env["result"]] == [5, 6]
    assert env["meta"]["start_line"]["state"] == "in_range"
    both = _turns(archive, limit=2, start_line=5, cursor="anything")
    assert both["result_status"] == "cannot_determine"
    past = _turns(archive, limit=2, start_line=9999)
    assert past["result_status"] == "not_found"
    assert past["meta"]["start_line"]["max_line_no"] == 9


def test_a_missing_transcript_is_not_an_empty_page(archive):
    """not_found and an empty ok are different findings."""
    env = _turns(archive, limit=5)
    conn = open_read_only(archive["state_dir"])
    try:
        missing = transcript_turns(conn, 999999, limit=5)
    finally:
        conn.close()
    assert env["result_status"] == "ok"
    assert missing["result_status"] == "not_found"
    assert missing["result"] == []
