"""The merged tree envelope, and the unattributed count the rail hides on.

THE HIDE IS THE DANGEROUS PART. The rail removes the "no project" node
when the count is zero, and those transcripts are unreachable from the
project tree by construction - so a wrong hide is permanent and silent.
The server therefore has to make "0" and "I could not count" impossible
to confuse, which is what ``counted`` is for. Measured 2026-09-01:
corpus 1 has 0, corpus 2 has 5, corpus 3 has 0.
"""

from __future__ import annotations

import sqlite3

from src.core.archive_merged_tree import UNATTRIBUTED_HIDE_RULE, merged_projects


def _archive(unattributed_in_corpus_2: int = 5) -> sqlite3.Connection:
    """Build a two-host, three-corpus archive shaped like the real one.

    Description: mirrors the live topology - one project on both
      machines, one on each - so a merge that works here is evidence
      about the merge that runs live.
    Inputs: unattributed_in_corpus_2 (int).
    Output: sqlite3.Connection.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE message_hosts (id INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE message_corpora (
            id INTEGER PRIMARY KEY, host_id INTEGER, corpus_key TEXT);
        CREATE TABLE message_projects (
            id INTEGER PRIMARY KEY, corpus_id INTEGER, slug TEXT, observed_cwd TEXT);
        CREATE TABLE message_transcripts (
            id INTEGER PRIMARY KEY, project_id INTEGER, corpus_id INTEGER,
            session_ref_scheme TEXT, newest_message_ts TEXT);
        INSERT INTO message_hosts VALUES (1, 'Joe-MBP-M1'), (2, 'Mac mini');
        INSERT INTO message_corpora VALUES
            (1, 1, 'claude-projects'), (2, 1, 'agent-sessions'),
            (3, 2, 'claude-projects');
        INSERT INTO message_projects VALUES
            (1, 1, '-Users-j-Media', '/Users/j/Media'),
            (2, 3, '-Users-j-Media', '/Users/j/Media'),
            (3, 1, '-Users-j-Solo',  '/Users/j/Solo');
        INSERT INTO message_transcripts VALUES
            (1, 1, 1, 'uuid',  '2026-08-30T10:00:00Z'),
            (2, 1, 1, 'agent', '2026-08-29T10:00:00Z'),
            (3, 2, 3, 'uuid',  '2026-07-01T10:00:00Z'),
            (4, 3, 1, 'agent', '2025-12-29T10:00:00Z');
        """
    )
    for n in range(unattributed_in_corpus_2):
        conn.execute(
            "INSERT INTO message_transcripts "
            "(project_id, corpus_id, session_ref_scheme) VALUES (NULL, 2, 'uuid')"
        )
    return conn


def test_the_merged_list_collapses_the_cross_host_project():
    """3 project rows over 2 hosts become 2 nodes, one naming both."""
    env = merged_projects(_archive())
    assert env["result_status"] == "ok"
    assert env["meta"]["merge"]["project_rows"] == 3
    assert env["meta"]["merge"]["merged_nodes"] == 2
    assert env["meta"]["merge"]["nodes_on_more_than_one_host"] == 1
    media = [n for n in env["result"] if n["display_name"] == "Media"][0]
    assert media["hosts"] == ["Joe-MBP-M1", "Mac mini"]
    assert media["transcript_count"] == 3


def test_every_node_carries_the_contract_fields():
    """display_name, full_path and hosts are consumed by another agent."""
    env = merged_projects(_archive())
    for node in env["result"]:
        assert "display_name" in node
        assert "full_path" in node
        assert isinstance(node["hosts"], list)


def test_the_host_list_lets_a_client_filter_without_a_second_request():
    """The machine is a field now, so the filter's options ship with it."""
    meta = merged_projects(_archive())["meta"]
    assert [h["display_name"] for h in meta["hosts"]] == ["Joe-MBP-M1", "Mac mini"]
    assert {h["host_id"]: h["project_count"] for h in meta["hosts"]} == {1: 2, 2: 1}


def test_a_corpus_with_none_reports_zero_rather_than_being_absent():
    """Absence and zero are different findings; only zero may hide."""
    rows = merged_projects(_archive())["meta"]["unattributed"]["by_corpus"]
    assert [r["corpus_id"] for r in rows] == [1, 2, 3]
    by_id = {r["corpus_id"]: r for r in rows}
    assert by_id[1]["transcript_count"] == 0 and by_id[1]["counted"] is True
    assert by_id[2]["transcript_count"] == 5 and by_id[2]["counted"] is True
    assert by_id[3]["transcript_count"] == 0 and by_id[3]["counted"] is True


def test_an_uncountable_corpus_reports_null_and_counted_false():
    """THE THIRD OUTCOME, on the number a hide is decided from.

    A zero here would make the rail hide a node on a count nobody
    produced - the exact false green this API is written against.
    """
    conn = _archive()
    conn.execute("DROP TABLE message_transcripts")
    conn.executescript(
        "CREATE TABLE message_transcripts (id INTEGER PRIMARY KEY, project_id INTEGER);"
    )
    rows = merged_projects(conn)["meta"]["unattributed"]["by_corpus"]
    assert all(r["transcript_count"] is None for r in rows)
    assert all(r["counted"] is False for r in rows)


def test_the_hide_rule_travels_with_the_response():
    """The rail and the server cannot drift about when to hide."""
    meta = merged_projects(_archive())["meta"]
    assert meta["unattributed"]["hide_when"] == UNATTRIBUTED_HIDE_RULE
    assert "counted is false" in UNATTRIBUTED_HIDE_RULE


def test_each_unattributed_row_carries_a_reachable_href():
    """The node has to lead somewhere or it is only an accusation."""
    rows = merged_projects(_archive())["meta"]["unattributed"]["by_corpus"]
    assert rows[1]["href"].endswith("/corpora/2/unattributed")


def test_a_populated_archive_can_never_merge_to_an_empty_list():
    """The zero-row guard: nodes may only be empty when rows are.

    WHY THIS EXISTS. Every other test here asserts a SHAPE - the fields a
    node carries, the hosts it names, the counts beside it - and every
    one of them passes vacuously against an empty result, because a loop
    over nothing asserts nothing. So the suite could stay green while the
    endpoint returned `[]`, which a rail renders as the perfectly
    plausible "no projects in this view" rather than as a fault.

    The invariant is the one thing a shape assertion cannot express:
    merging is a fold, so it can only ever REDUCE the row count, never
    take it to zero. `merged_nodes == 0` therefore implies
    `project_rows == 0`, and any other combination is a merge that lost
    its input - which is the whole failure this guards.
    """
    env = merged_projects(_archive())
    merge = env["meta"]["merge"]

    assert merge["project_rows"] == 3
    assert len(env["result"]) > 0, (
        "3 project rows merged to an EMPTY list - a fold cannot reduce a "
        "non-empty input to nothing"
    )
    assert len(env["result"]) == merge["merged_nodes"]
    if merge["merged_nodes"] == 0:
        assert merge["project_rows"] == 0


def test_an_archive_with_no_projects_says_ok_and_empty_rather_than_failing():
    """The other half: a genuinely empty archive is not a fault.

    The guard above must not be satisfiable by making emptiness an
    error, because an archive with no projects is a real, healthy state.
    Empty-and-ok is correct here precisely BECAUSE `project_rows` is 0 -
    the number that makes the emptiness legible rather than ambiguous.
    """
    conn = _archive()
    conn.execute("DELETE FROM message_transcripts")
    conn.execute("DELETE FROM message_projects")

    env = merged_projects(conn)

    assert env["result"] == []
    assert env["result_status"] == "ok"
    assert env["meta"]["merge"]["project_rows"] == 0
    assert env["meta"]["merge"]["merged_nodes"] == 0


def test_every_underlying_project_id_is_reachable_from_some_node():
    """No project id may be unaddressable after the merge.

    A node carries `project_id` for its FIRST member only, so the ids of
    the folded-up rows survive nowhere except `members`. A deep link
    names one of THOSE ids just as legitimately, and on the live corpus
    exactly 3 of 80 are in that position - the 3 cross-machine projects.
    Losing them from `members` would strand a link with no error
    anywhere, so the reachable set is asserted against the table.
    """
    conn = _archive()
    env = merged_projects(conn)

    reachable = set()
    for node in env["result"]:
        reachable.add(node["project_id"])
        for member in node["members"]:
            reachable.add(member["project_id"])

    every_id = {r["id"] for r in conn.execute("SELECT id FROM message_projects")}
    assert reachable == every_id
    assert len(every_id) > len(env["result"]), "fixture must exercise a fold"


# ---------------------------------------------------------------------------
# The per-project SESSION count
#
# THE CARD SHOWS THIS NUMBER AS THE PRIMARY ONE, so all three of its
# outcomes are pinned here rather than only the happy path. The live
# corpus contains no project with zero own-sessions (measured 2026-09-02:
# all 77 nodes counted, 1,446 sessions across 21,034 attributed
# transcripts), so a genuine zero exists ONLY in this fixture. Without
# these tests the zero path ships unexecuted.
# ---------------------------------------------------------------------------


def _archive_without_scheme() -> sqlite3.Connection:
    """The same archive on a schema that cannot answer the question.

    Description: ``message_transcripts`` with no ``session_ref_scheme``
      column, which is what an older archive version looks like. Used to
      prove the count degrades to a NAMED unknown rather than to a zero,
      and that the totals survive the degradation.
    Output: sqlite3.Connection.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE message_hosts (id INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE message_corpora (
            id INTEGER PRIMARY KEY, host_id INTEGER, corpus_key TEXT);
        CREATE TABLE message_projects (
            id INTEGER PRIMARY KEY, corpus_id INTEGER, slug TEXT, observed_cwd TEXT);
        CREATE TABLE message_transcripts (
            id INTEGER PRIMARY KEY, project_id INTEGER, corpus_id INTEGER);
        INSERT INTO message_hosts VALUES (1, 'Joe-MBP-M1');
        INSERT INTO message_corpora VALUES (1, 1, 'claude-projects');
        INSERT INTO message_projects VALUES (1, 1, '-Users-j-Solo', '/Users/j/Solo');
        INSERT INTO message_transcripts VALUES (1, 1, 1), (2, 1, 1);
        """
    )
    return conn


def _by_name(env):
    """Index a merged envelope's nodes by display_name."""
    return {node["display_name"]: node for node in env["result"]}


def test_the_session_count_is_the_uuid_scheme_transcripts_only():
    """Not the total. The fixture makes the two numbers differ."""
    nodes = _by_name(merged_projects(_archive()))
    media = nodes["Media"]
    # 3 transcripts across two hosts, 2 of them the owner's own.
    assert media["transcript_count"] == 3
    assert media["session_count"] == 2
    assert media["session_counted"] is True


def test_a_project_with_only_agent_sidechains_reports_a_measured_zero():
    """0 with counted TRUE. The case the live corpus does not contain."""
    solo = _by_name(merged_projects(_archive()))["Solo"]
    assert solo["transcript_count"] == 1
    assert solo["session_count"] == 0, "a measured zero must be a number"
    assert solo["session_counted"] is True, "and it must say it was measured"


def test_an_unanswerable_schema_reports_null_and_counted_false_not_zero():
    """The third outcome. A zero here would be a verdict nobody measured."""
    node = merged_projects(_archive_without_scheme())["result"][0]
    assert node["session_count"] is None
    assert node["session_counted"] is False


def test_the_totals_survive_a_session_count_that_could_not_be_taken():
    """Losing one number must not silently lose the other."""
    node = merged_projects(_archive_without_scheme())["result"][0]
    assert node["transcript_count"] == 2, (
        "the fallback must still measure the total; dropping both would "
        "turn one unanswerable question into two"
    )


def test_a_measured_zero_and_an_unmeasured_count_are_not_equal():
    """The negative control, stated as an assertion rather than assumed."""
    zero = _by_name(merged_projects(_archive()))["Solo"]
    unknown = merged_projects(_archive_without_scheme())["result"][0]
    assert zero["session_count"] != unknown["session_count"]
    assert zero["session_counted"] != unknown["session_counted"]


def test_the_node_total_is_the_sum_of_its_members_session_counts():
    """The merge must not report one host's count for a two-host project."""
    media = _by_name(merged_projects(_archive()))["Media"]
    assert len(media["members"]) == 2
    assert sum(m["session_count"] for m in media["members"]) == media["session_count"]


def test_one_uncounted_member_makes_the_whole_node_uncounted():
    """A total that omits an unmeasured member is a number nobody measured."""
    from src.core.archive_project_names import merge_projects
    rows = [
        {"project_id": 1, "slug": "-a", "observed_cwd": "/a", "corpus_id": 1,
         "host_id": 1, "host_display_name": "H1", "transcript_count": 4,
         "session_count": 2, "session_counted": True},
        {"project_id": 2, "slug": "-a", "observed_cwd": "/a", "corpus_id": 2,
         "host_id": 2, "host_display_name": "H2", "transcript_count": 1,
         "session_count": None, "session_counted": False},
    ]
    node = merge_projects(rows)[0]
    assert node["session_count"] is None, "2 + unknown is not 2"
    assert node["session_counted"] is False


def test_the_response_says_what_the_session_count_counts():
    """Shipped in meta, so the rail cannot label it something else."""
    meta = merged_projects(_archive())["meta"]
    assert "uuid" in meta["counts"]["sessions_mean"]
    assert "agent sidechains" in meta["counts"]["sessions_mean"]
    assert meta["counts"]["session_uncounted_nodes"] == 0
    unknown = merged_projects(_archive_without_scheme())["meta"]
    assert unknown["counts"]["session_uncounted_nodes"] == 1, (
        "a node nobody could count must be visible in the roll-up, not "
        "only in the row"
    )


def test_the_session_count_costs_no_extra_statement():
    """ONE grouped scan, not two and not one per project.

    The rail paints every project at once, so a per-project query is an
    N+1 on the only way into the archive. Counted by instrumenting the
    connection rather than by reading the code, because the code is what
    is under test.
    """
    conn = _archive()
    statements: list = []
    # set_trace_callback sees every statement SQLite actually executes,
    # which is the measurement. Wrapping conn.execute would only see the
    # calls this module chose to make through that one method.
    conn.set_trace_callback(statements.append)
    merged_projects(conn)
    conn.set_trace_callback(None)

    over_transcripts = [s for s in statements if "message_transcripts" in s]
    # One for the per-project counts, one for the unattributed rows.
    assert len(over_transcripts) == 2, (
        f"expected 2 statements over message_transcripts, got "
        f"{len(over_transcripts)}: {over_transcripts}"
    )
    grouped = [s for s in over_transcripts if "GROUP BY project_id" in s]
    assert len(grouped) == 1, "the two counts must come from ONE statement"
    assert "session_ref_scheme" in grouped[0], (
        "and that one statement must be the one carrying the session count"
    )
