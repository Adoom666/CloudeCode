"""The MERGED project tree: one node per project, machine as a badge.

WHY THIS EXISTS AS A SECOND SHAPE RATHER THAN A REPLACEMENT. The rail was
host -> corpus -> project, which is the shape the DATABASE has:
``message_projects`` is ``UNIQUE (corpus_id, slug)`` and a corpus belongs
to exactly one host. That shape answers "what did this machine hold",
which was the right question during ingest and is the wrong one for
navigation - the owner is moving everything onto one machine, at which
point which box a project was born on stops being a level anyone wants
to click through and becomes a detail they occasionally need.

So this module returns ONE FLAT LIST of projects with the machine
demoted to a field, and the host/corpus routes are left exactly as they
were. Nothing is removed: a client that wants the physical shape still
has it, and this response carries ``members`` so the physical shape is
reachable from the merged one without a second request.

WHAT THE UNATTRIBUTED BLOCK IS FOR, AND WHY IT IS PER CORPUS. Transcripts
with ``project_id IS NULL`` are unreachable from a project tree BY
CONSTRUCTION, so merging projects would erase them entirely if the merged
response did not carry them. It reports one row per corpus, ALWAYS,
including at zero. Measured 2026-09-01: corpus 1 has 0, corpus 2 has 5,
corpus 3 has 0.

TWO COUNTS PER NODE, BECAUSE ONE WOULD LIE. ``transcript_count`` is
every transcript; ``session_count`` is only those whose
``session_ref_scheme`` is ``uuid``, which is what the middle column
already shows by default. They differ by more than an order of magnitude
(19,588 of 21,039 transcripts are agent sidechains, measured 2026-09-02),
so a card that labelled the total "sessions" would overstate every
project. Both come out of ONE grouped statement - see
``fetch_project_rows`` for why that matters and what it costs.

THE COUNT IS THE THING THE CLIENT MUST BE ABLE TO TRUST, because the
client hides the node on a known zero. A count this module could not
measure is emitted as ``null`` with ``counted: false``, never as 0. A
client that hides on null would be hiding 5 transcripts on the strength
of a number nobody produced - which is the exact false-green shape the
rest of this API is written against. The rule is stated in the response
itself (``hide_when``) rather than left to the client to remember.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from src.core.message_activity import (
    ACTIVITY_KNOWN,
    ACTIVITY_MEANS,
    ACTIVITY_NONE,
    ACTIVITY_UNKNOWN,
)
from src.core.archive_project_names import (
    MERGE_MEANS,
    NAMES_MEAN,
    SESSIONS_MEAN,
    fetch_project_rows,
    merge_projects,
)
from src.core.archive_read import API_PREFIX, RESULT_OK, envelope

#: The rule the client applies to the unattributed node, shipped in the
#: response so the server and the rail cannot drift about it.
UNATTRIBUTED_HIDE_RULE: str = (
    "hide this node ONLY when counted is true and transcript_count is 0. "
    "When counted is false the count could not be measured and the node "
    "MUST be shown, because hiding on an unmeasured count is "
    "indistinguishable from hiding real transcripts."
)


def _unattributed_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Count the project-less transcripts in every corpus.

    Description: one grouped query over the corpora, so a corpus with no
      unattributed transcripts still gets a row (at 0) rather than being
      absent - absence and zero are different findings and only one of
      them may hide the node. A corpus whose count cannot be read comes
      back ``counted: false`` with a null count.
    Inputs: conn (sqlite3.Connection).
    Output: list[dict] - one row per corpus.
    Example: _unattributed_rows(conn)[1]["transcript_count"] -> 5
    """
    corpora = conn.execute(
        "SELECT k.id, k.corpus_key, k.host_id, h.display_name AS host_display_name "
        "FROM message_corpora k JOIN message_hosts h ON h.id = k.host_id "
        "ORDER BY k.id"
    ).fetchall()
    try:
        counts = {
            row["corpus_id"]: int(row["n"])
            for row in conn.execute(
                "SELECT corpus_id, COUNT(*) AS n FROM message_transcripts "
                "WHERE project_id IS NULL GROUP BY corpus_id"
            ).fetchall()
        }
        counted = True
    except sqlite3.Error:
        # THE THIRD OUTCOME: not zero. A null count keeps the node on
        # screen instead of hiding transcripts nobody counted.
        counts = {}
        counted = False
    return [
        {
            "corpus_id": row["id"],
            "corpus_key": row["corpus_key"],
            "host_id": row["host_id"],
            "host_display_name": row["host_display_name"],
            "transcript_count": counts.get(row["id"], 0) if counted else None,
            "counted": counted,
            "href": f"{API_PREFIX}/corpora/{row['id']}/unattributed",
        }
        for row in corpora
    ]


def merged_projects(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The whole archive's projects as one host-independent list.

    Description: deliberately NOT paginated. 80 project rows merge to 77
      nodes (measured 2026-09-01), and a page of a merged tree would let
      a client conclude a project lives on one machine because the row
      proving otherwise fell on page 2. If this ever grows past a few
      hundred, page it by adding a cursor - do not page it by host, which
      would undo the merge.
      ``meta.hosts`` lists every machine so a client can offer a host
      filter without a second request, and ``meta.merge`` records how
      many rows collapsed, so a reader can see the merge happened rather
      than inferring it from the absence of duplicates.
    Inputs: conn (sqlite3.Connection).
    Output: envelope; ``result`` is a list of merged project nodes.
    Example: merged_projects(conn)["result"][0]["display_name"]
    """
    rows = fetch_project_rows(conn)
    nodes = merge_projects(rows)
    hosts = [
        {
            "host_id": row["id"],
            "display_name": row["display_name"],
            "project_count": sum(
                1 for n in nodes if any(
                    m["host_id"] == row["id"] for m in n["members"]
                )
            ),
        }
        for row in conn.execute(
            "SELECT id, display_name FROM message_hosts ORDER BY id"
        ).fetchall()
    ]
    unattributed = _unattributed_rows(conn)
    return envelope(
        result=nodes,
        result_status=RESULT_OK,
        meta={
            "scope": {"kind": "archive"},
            "merge": {
                "project_rows": len(rows),
                "merged_nodes": len(nodes),
                "nodes_on_more_than_one_host": sum(
                    1 for n in nodes if n["host_count"] > 1
                ),
                "merge_means": MERGE_MEANS,
            },
            "naming": {"names_mean": NAMES_MEAN},
            "counts": {
                "sessions_mean": SESSIONS_MEAN,
                "session_uncounted_nodes": sum(
                    1 for n in nodes if n.get("session_counted") is False
                ),
            },
            # THREE COUNTS, NOT TWO. A node whose timestamp is a measured
            # absence and one whose timestamp nobody could establish are
            # reported separately, because a client that ordered by time
            # has to place them differently: the first is genuinely
            # undated, the second is unread. Collapsing them here would
            # hand the rail a number it could only render as one of them.
            "activity": {
                "activity_means": ACTIVITY_MEANS,
                "known_nodes": sum(
                    1 for n in nodes
                    if n.get("activity_status") == ACTIVITY_KNOWN
                ),
                "none_nodes": sum(
                    1 for n in nodes
                    if n.get("activity_status") == ACTIVITY_NONE
                ),
                "unknown_nodes": sum(
                    1 for n in nodes
                    if n.get("activity_status") == ACTIVITY_UNKNOWN
                ),
            },
            "hosts": hosts,
            "unattributed": {
                "by_corpus": unattributed,
                "hide_when": UNATTRIBUTED_HIDE_RULE,
            },
        },
    )
