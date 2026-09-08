"""The normative shape of ``result`` under each result_status.

Section 3.1 of ``docs/message-browser-api.md`` states the rule; this file
is what keeps it true. The rule exists because a client cannot classify
an outcome from the SHAPE of ``result`` unless the shape is consistent,
and before this was written down two endpoints disagreed with the rest:
``/projects/{id}/transcripts`` and ``/corpora/{id}/unattributed``
answered a malformed cursor with ``[]`` while every other paged route
answered ``null``.

THE RULE, IN TWO LINES.
``cannot_determine`` carries ``result: null`` EVERYWHERE, because the
question was not evaluated and so has no payload of any shape - and
because ``[]`` lets a client that reads only ``result`` render a
confident empty state over an answer nobody measured.
``not_found`` carries the route's SUCCESS shape - ``[]`` for a collection
route, ``null`` for a single-object route - because the subject provably
not existing is a MEASUREMENT, and ``scope_status`` is what carries it.

The last test in this file is the one that matters most: it asserts the
rule STRUCTURALLY over every call site rather than over the handful of
endpoints reachable from a fixture, because the drift being guarded
against is a new route added later that quietly passes ``result=[]``.
"""

from __future__ import annotations

import ast
import pathlib
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

from src.core import archive_hierarchy, archive_lines, archive_subagents
from src.core.archive_body import body
from src.core.archive_read import (
    RESULT_CANNOT_DETERMINE,
    RESULT_NOT_FOUND,
    open_read_only,
)
from tests.archive_fixture import (
    make_state_dir,
    seed_appearance,
    seed_body,
    seed_corpus,
    seed_host,
    seed_project,
    seed_transcript,
    writable,
)

#: A cursor that cannot decode, so every paged route reaches its
#: cannot_determine branch by the same door.
BAD_CURSOR = "!!!not-base64url!!!"

#: An id no fixture seeds, so the not_found branch is a measurement.
MISSING_ID = 99999999


@pytest.fixture(name="state_dir")
def state_dir_fixture(tmp_path: Path) -> Path:
    """Seed one host, corpus, project, transcript, body and line.

    Description: the smallest archive on which every route below reaches
      a real scope, so a cannot_determine can only come from the cursor
      and not from an empty database.
    Inputs: tmp_path (Path). Output: Path - the state directory.
    Example: with closing(open_read_only(state_dir)) as conn: ...
    """
    state_dir = make_state_dir(tmp_path)
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id=host_id)
            project_id = seed_project(conn, corpus_id, slug="-proj")
            transcript_id = seed_transcript(
                conn, host_id=host_id, corpus_id=corpus_id,
                project_id=project_id, source_path="/tmp/a.jsonl",
            )
            body_id = seed_body(conn, body_json='{"a":1}')
            seed_appearance(
                conn, transcript_id=transcript_id, line_no=0, body_id=body_id
            )
    return state_dir


def _call(state_dir: Path, fn: Callable[..., Dict[str, Any]], *args: Any,
          **kwargs: Any) -> Dict[str, Any]:
    """Run one core read against the seeded archive and return its envelope.

    Inputs: state_dir (Path), fn (callable), *args/**kwargs forwarded.
    Output: dict envelope.
    Example: _call(sd, archive_hierarchy.hosts)["result_status"]
    """
    with closing(open_read_only(state_dir)) as conn:
        return fn(conn, *args, **kwargs)


#: Every paged route, reached with a cursor that cannot decode. Each must
#: answer cannot_determine with result None.
_BAD_CURSOR_CASES: List[Tuple[str, Callable[..., Dict[str, Any]], Tuple[Any, ...]]] = [
    ("projects_for_corpus", archive_hierarchy.projects_for_corpus, (1,)),
    ("transcripts_for_project", archive_hierarchy.transcripts_for_project, (1,)),
    ("unattributed_for_corpus", archive_hierarchy.unattributed_for_corpus, (1,)),
    ("transcript_lines", archive_lines.transcript_lines, (1,)),
    ("subagents_for_transcript", archive_subagents.subagents_for_transcript, (1,)),
]


@pytest.mark.parametrize(
    "name,fn,args", _BAD_CURSOR_CASES, ids=[c[0] for c in _BAD_CURSOR_CASES]
)
def test_cannot_determine_carries_null_result_on_every_paged_route(
    state_dir: Path, name: str, fn: Callable[..., Dict[str, Any]],
    args: Tuple[Any, ...],
) -> None:
    """A question that was not evaluated has no payload, on any route."""
    env = _call(state_dir, fn, *args, cursor=BAD_CURSOR)
    assert env["result_status"] == RESULT_CANNOT_DETERMINE
    assert env["result"] is None, (
        f"{name} answered a malformed cursor with {env['result']!r}; "
        f"cannot_determine must carry result: null so a client that reads "
        f"only result cannot render a confident empty state"
    )
    # The refusal must also SAY what it could not evaluate.
    assert env["unevaluated"], f"{name} refused without naming a subject"


def test_unknown_filter_value_is_also_a_null_result(state_dir: Path) -> None:
    """An unresolvable filter is cannot_determine, not an empty page.

    Description: "there is no role called nope" and "no line used that
      role" are different findings, and only the second is an empty ok.
    """
    env = _call(state_dir, archive_lines.transcript_lines, 1, role="nope")
    assert env["result_status"] == RESULT_CANNOT_DETERMINE
    assert env["result"] is None


#: Collection routes: a not_found scope keeps the success shape, [].
_COLLECTION_NOT_FOUND: List[Tuple[str, Callable[..., Dict[str, Any]]]] = [
    ("corpora_for_host", archive_hierarchy.corpora_for_host),
    ("projects_for_corpus", archive_hierarchy.projects_for_corpus),
    ("transcripts_for_project", archive_hierarchy.transcripts_for_project),
    ("unattributed_for_corpus", archive_hierarchy.unattributed_for_corpus),
    ("transcript_lines", archive_lines.transcript_lines),
    ("subagents_for_transcript", archive_subagents.subagents_for_transcript),
]


@pytest.mark.parametrize(
    "name,fn", _COLLECTION_NOT_FOUND, ids=[c[0] for c in _COLLECTION_NOT_FOUND]
)
def test_not_found_on_a_collection_route_keeps_the_list_shape(
    state_dir: Path, name: str, fn: Callable[..., Dict[str, Any]],
) -> None:
    """not_found is a measurement, so the shape stays honest."""
    env = _call(state_dir, fn, MISSING_ID)
    assert env["result_status"] == RESULT_NOT_FOUND
    assert env["result"] == [], f"{name} broke the collection shape"
    assert env["scope_status"] == RESULT_NOT_FOUND


#: Single-object routes: a not_found scope carries null, their success shape.
_OBJECT_NOT_FOUND: List[Tuple[str, Callable[..., Dict[str, Any]]]] = [
    ("transcript_header", archive_lines.transcript_header),
    ("body", body),
]


@pytest.mark.parametrize(
    "name,fn", _OBJECT_NOT_FOUND, ids=[c[0] for c in _OBJECT_NOT_FOUND]
)
def test_not_found_on_a_single_object_route_carries_null(
    state_dir: Path, name: str, fn: Callable[..., Dict[str, Any]],
) -> None:
    """A single-object route's success shape is an object, so absent is null."""
    env = _call(state_dir, fn, MISSING_ID)
    assert env["result_status"] == RESULT_NOT_FOUND
    assert env["result"] is None, f"{name} broke the single-object shape"


def test_an_empty_ok_is_still_a_list(state_dir: Path) -> None:
    """A genuinely empty collection is ``ok`` with ``[]``, never null.

    Description: the other half of the rule. If an empty ok answered
      null, a client could not tell "no rows" from "not evaluated" - the
      exact confusion the rule prevents in the opposite direction.
    """
    env = _call(state_dir, archive_subagents.subagents_for_transcript, 1)
    assert env["result_status"] == "ok"
    assert env["result"] == []


def test_no_call_site_passes_an_empty_list_to_a_cannot_determine(
) -> None:
    """STRUCTURAL: no code anywhere builds a cannot_determine with [].

    Description: the parametrized tests above only cover the routes a
      fixture can reach. This one parses every archive module and fails
      on the pattern itself, so a route added next month that passes
      ``result=[]`` to ``cursor_error_envelope`` is caught the day it is
      written rather than the day a client mis-renders it. This is the
      check that would have caught the original drift.
    """
    builders = {"cannot_determine_envelope", "cursor_error_envelope"}
    offenders: List[str] = []
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in sorted((root / "src").rglob("archive*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in builders:
                continue
            for keyword in node.keywords:
                if keyword.arg != "result":
                    continue
                value = keyword.value
                empty_list = isinstance(value, ast.List) and not value.elts
                if empty_list:
                    offenders.append(
                        f"{path.relative_to(root)}:{node.lineno} "
                        f"{name}(result=[])"
                    )
    assert not offenders, (
        "cannot_determine must carry result: null, never []. Offending "
        "call sites:\n  " + "\n  ".join(offenders)
    )
