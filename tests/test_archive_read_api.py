"""Tests for ``/api/v1/archive/*`` (the message browser route layer).

WHAT THIS FILE IS FOR, AND WHAT IT DELIBERATELY IS NOT. The core modules
have their own suites for query correctness, keyset walking and secret
withholding. This file asserts the ROUTE LAYER's four standing promises,
each of which is invisible in code review and each of which has bitten a
real project:

1. **Auth, against a real request.** A route registered on the wrong
   router looks identical in review. Every route is called with no
   credentials and must answer 401.
2. **``response_model is None`` on every route**, asserted by walking
   ``router.routes``. A FastAPI response model is a FILTER: it silently
   deletes any field it does not declare, which is exactly what would
   happen to ``unevaluated`` and ``meta``. A structural test is the only
   thing that stops a route added next month from reintroducing it.
3. **Every 200 carries a ``result_status`` from the permitted set.** An
   empty ``result`` means three different things depending on it.
4. **A malformed cursor is a 400 ``cannot_determine``, never a page-1
   reset.** A client that silently restarts renders duplicates forever
   and never finishes, and raises no error for anyone to see.

The fixture database is BUILT HERE, from the real migration, and is
deliberately tiny. THE REAL CORPUS IS NEVER OPENED by anything in this
file - not read, not copied, not pointed at.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_arcapi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_arcapi_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.archive_routes import router as archive_router
from src.api.auth import require_auth
from src.core.archive_read import (
    MAX_LINE_LIMIT,
    MAX_PAGE_LIMIT,
    RESULT_STATUSES,
    SCOPE_STATUSES,
    VERIFY_BEFORE_SEND_MAX_BYTES,
)
from src.core.archive_search import SCAN_COMPLETE, SCAN_NOT_RUN
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.message_model_export import export_transcript
from src.core.message_model_serialize import render_line, sha256_text

#: Every archive path, with a concrete id substituted, so a test can hit
#: ALL of them without hand-maintaining a second list per test. Kept in
#: one place: a route missing from here is a route nobody proved needs a
#: token, which is the failure this file exists to prevent.
ALL_ROUTES: List[str] = [
    "/api/v1/archive/hosts",
    "/api/v1/archive/hosts/1/corpora",
    # The MERGED project list: one node per project across every host.
    # Placed among the envelope routes, not appended, because
    # ENVELOPE_ROUTES is ALL_ROUTES[:-2] and the last two entries are
    # the exports, whose 200 is a file rather than an envelope. Adding
    # it at the end would silently push an export out of that slice.
    "/api/v1/archive/projects",
    "/api/v1/archive/corpora/1/projects",
    "/api/v1/archive/corpora/1/unattributed",
    "/api/v1/archive/projects/1/transcripts",
    "/api/v1/archive/transcripts/1",
    "/api/v1/archive/transcripts/1/lines",
    "/api/v1/archive/transcripts/1/subagents",
    # The reading view. Sits WITH the envelope routes, not appended:
    # ENVELOPE_ROUTES is ALL_ROUTES[:-2] and the last two entries are
    # the exports, whose 200 is a file rather than an envelope.
    "/api/v1/archive/transcripts/1/messages",
    "/api/v1/archive/bodies/1",
    "/api/v1/archive/search?q=hello&project_id=1",
    "/api/v1/archive/transcripts/1/export",
    "/api/v1/archive/transcripts/1/export/verified",
]

#: Routes whose 200 body is the three-outcome envelope. The two export
#: routes are excluded because a SUCCESSFUL export is a file, not an
#: envelope - their failure paths ARE envelopes and are asserted
#: separately.
ENVELOPE_ROUTES: List[str] = ALL_ROUTES[:-2]


def _app(authed: bool = True) -> FastAPI:
    """Build a test app mounting only the archive router.

    Args:
        authed: when False, ``require_auth`` stays in place so the real
            401 can be observed instead of inferred from a decorator.

    Returns:
        A configured FastAPI app.
    """
    app = FastAPI()
    app.include_router(archive_router, prefix="/api/v1")
    if authed:
        app.dependency_overrides[require_auth] = lambda: True
    return app


def _line(body: Dict[str, Any]) -> str:
    """Render one JSONL line exactly as the archive stores and rebuilds it.

    Description: uses the model's own ``render_line`` rather than
      ``json.dumps``, so a fixture line's stored hash is produced by the
      same code the export path will use to reproduce it. A fixture built
      any other way would test the fixture, not the export.
    Inputs: body (dict) - the message body.
    Output: str - the rendered line.
    Example: _line({"type": "user"})
    """
    return render_line(body, {}, list(body.keys()), "compact")


def _seed(state: Path) -> Dict[str, Any]:
    """Populate a migrated fixture archive and report what is in it.

    Description: one host, one corpus, two projects, three transcripts -
      one ordinary, one with a subagent line, one with NO project so
      ``/unattributed`` has something real to find. Written through the
      schema directly rather than through the ingester, because this file
      is testing routes and an ingest run here would make a route failure
      and an ingest failure look the same.
    Inputs: state (Path) - a state dir already migrated.
    Output: dict of the ids and expected values the tests assert against.
    Example: _seed(state)["transcript_id"] -> 1
    """
    conn = connect(db_path_for(state), create=False)
    facts: Dict[str, Any] = {}
    try:
        conn.execute(
            "INSERT INTO message_hosts (id, machine_id, machine_id_scheme, "
            "display_name, hostname, platform, first_seen_at) VALUES "
            "(1, 'MID-1', 'platform_uuid', 'fixture-host', 'fixture', "
            "'Darwin 25.6.0', '2026-08-01T00:00:00.000000Z')"
        )
        conn.execute(
            "INSERT INTO message_corpora (id, host_id, corpus_key, root_path, "
            "collected_at, manifest_sha) VALUES "
            "(1, 1, 'claude-projects', '/fixture/projects', "
            "'2026-08-01T00:00:00.000000Z', NULL)"
        )
        conn.execute(
            "INSERT INTO message_projects (id, corpus_id, slug, observed_cwd, "
            "first_seen_at) VALUES "
            "(1, 1, '-fixture-a', '/fixture/a', '2026-08-01T00:00:00.000000Z')"
        )
        conn.execute("INSERT INTO message_roles (id, value) VALUES (1, 'user')")
        conn.execute(
            "INSERT INTO message_roles (id, value) VALUES (2, 'assistant')"
        )
        bodies = [
            {"type": "user", "uuid": "u1", "sessionId": "sess-1",
             "message": {"role": "user", "content": "hello archive"}},
            {"type": "assistant", "uuid": "u2", "sessionId": "sess-1",
             "message": {"role": "assistant", "content": "hello back"}},
        ]
        texts = [_line(b) for b in bodies]
        content = "\n".join(texts) + "\n"
        for tid, project_id, ref in (
            (1, 1, "sess-1"), (2, 1, "sess-2"), (3, None, "sess-3"),
        ):
            conn.execute(
                "INSERT INTO message_transcripts (id, host_id, corpus_id, "
                "project_id, source_ref, session_ref, session_ref_scheme, "
                "source_path, line_ending, has_trailing_newline, line_count, "
                "raw_byte_length, content_sha256, ingested_at, "
                "host_attribution, project_attribution) VALUES "
                "(?, 1, 1, ?, ?, ?, 'uuid', ?, 'LF', 1, 2, ?, ?, "
                "'2026-08-01T00:00:0" + str(tid) + ".000000Z', "
                "'manifest_verified', ?)",
                (tid, project_id, f"MID-1::claude-projects::{ref}.jsonl",
                 ref, f"{ref}.jsonl",
                 len(content.encode("utf-8")), sha256_text(content),
                 "derived" if project_id else "none_declared"),
            )
        body_ids = []
        for index, (body, text) in enumerate(zip(bodies, texts), start=1):
            payload = json.dumps(body, separators=(",", ":"))
            conn.execute(
                "INSERT INTO message_bodies (id, identity_key, message_uuid, "
                "body_json, body_sha256, body_bytes_sha256, parent_uuid, ts, "
                "origin_session_ref, is_compact_boundary, "
                "secret_finding_count, first_seen_at, role_id) VALUES "
                "(?, ?, ?, ?, ?, ?, NULL, ?, 'sess-1', 0, 0, "
                "'2026-08-01T00:00:00.000000Z', ?)",
                (index, f"key-{index}", body["uuid"], payload,
                 sha256_text(payload), sha256_text(payload),
                 f"2026-08-01T00:00:0{index}.000Z", index),
            )
            body_ids.append(index)
        appearance = 1
        for tid in (1, 2, 3):
            for line_no, (text, bid) in enumerate(zip(texts, body_ids), start=1):
                conn.execute(
                    "INSERT INTO message_appearances (id, transcript_id, "
                    "body_id, line_no, seq_in_file, line_status, "
                    "serializer_style, envelope_json, key_order_json, "
                    "line_sha256, line_byte_length, fidelity_outcome, "
                    "is_sidechain, agent_id, raw_line) VALUES "
                    "(?, ?, ?, ?, ?, 'ok', 'compact', '{}', ?, ?, ?, "
                    "'fidelity_verified', ?, ?, NULL)",
                    (appearance, tid, bid, line_no, line_no,
                     json.dumps(list(bodies[line_no - 1].keys())),
                     sha256_text(text), len(text.encode("utf-8")),
                     1 if (tid == 2 and line_no == 2) else 0,
                     "agent-x" if (tid == 2 and line_no == 2) else None),
                )
                appearance += 1
        conn.commit()
        facts.update({
            "transcript_id": 1, "subagent_transcript_id": 2,
            "unattributed_transcript_id": 3, "body_id": 1,
            "content": content, "content_sha256": sha256_text(content),
            "raw_byte_length": len(content.encode("utf-8")),
        })
    finally:
        conn.close()
    return facts


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """A migrated, seeded fixture archive wired into Settings.

    Never the real corpus: the state dir is a pytest tmp_path.
    """
    state = tmp_path / "state"
    ensure_db_migrated(state, 4, "0.8.2")
    facts = _seed(state)
    monkeypatch.setenv("CLOUDE_STATE_DIR", str(state))
    from src.config import settings

    # Settings is a pydantic model, so an instance attribute cannot be
    # set. Patch the METHOD on the class, which is what the route
    # resolves through anyway.
    monkeypatch.setattr(type(settings), "get_state_dir", lambda self: state)
    facts["state_dir"] = state
    return facts


def _bad_cursor() -> str:
    """Return a syntactically valid base64url string that is not a cursor.

    Description: deliberately NOT random garbage - it decodes cleanly as
      base64 and as JSON, and fails on the cursor's own kind/version
      check. That is the case a lenient decoder would wave through and
      restart at page 1 on.
    Inputs: none. Output: str.
    Example: _bad_cursor() -> 'eyJ2Ijo5OSwibm9wZSI6MX0'
    """
    raw = json.dumps({"v": 99, "nope": 1}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# --- Structural contract ---------------------------------------------------


def test_every_route_declares_response_model_none():
    """A response_model would silently DELETE unevaluated and meta."""
    offenders = [
        route.path for route in archive_router.routes
        if getattr(route, "response_model", None) is not None
    ]
    assert offenders == [], (
        f"these archive routes declare a response_model, which filters "
        f"fields out of the envelope: {offenders}"
    )
    # Positive control: the walk actually saw routes. A loop over an
    # empty list passes just as happily as a loop over correct ones.
    assert len(archive_router.routes) == len(ALL_ROUTES)


def test_every_route_carries_a_dependency():
    """Structural half of the auth check; the request half is below."""
    for route in archive_router.routes:
        assert getattr(route, "dependencies", []), (
            f"{route.path} carries no route dependency, so it cannot be "
            f"carrying require_auth"
        )


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_every_route_is_401_without_a_token(archive, path):
    """Asserted against a REAL request, not by reading a decorator."""
    with TestClient(_app(authed=False)) as client:
        assert client.get(path).status_code == 401, path


# --- The envelope on every route ------------------------------------------


@pytest.mark.parametrize("path", ENVELOPE_ROUTES)
def test_every_envelope_route_returns_a_permitted_result_status(archive, path):
    """Every key of section 3 present, and the status from the vocabulary."""
    with TestClient(_app()) as client:
        response = client.get(path)
    assert response.status_code == 200, (path, response.text[:400])
    body = response.json()
    for key in ("result", "result_status", "scope_status", "unevaluated", "meta"):
        assert key in body, f"{path} omitted {key}"
    assert body["result_status"] in RESULT_STATUSES
    assert body["scope_status"] in SCOPE_STATUSES
    assert isinstance(body["unevaluated"], list)
    assert isinstance(body["meta"], dict)


def test_hosts_and_hierarchy_walk(archive):
    """The fixture's own hierarchy round-trips, host to transcript."""
    with TestClient(_app()) as client:
        hosts = client.get("/api/v1/archive/hosts").json()
        corpora = client.get("/api/v1/archive/hosts/1/corpora").json()
        projects = client.get("/api/v1/archive/corpora/1/projects").json()
        transcripts = client.get(
            "/api/v1/archive/projects/1/transcripts"
        ).json()
        unattributed = client.get(
            "/api/v1/archive/corpora/1/unattributed"
        ).json()

    assert hosts["result"][0]["host_id"] == 1
    assert corpora["result"][0]["unattributed_transcript_count"] == 1
    assert projects["result"][0]["project_id"] == 1
    assert {row["transcript_id"] for row in transcripts["result"]} == {1, 2}
    # The project-less transcript is reachable ONLY here. If it were not,
    # it would be invisible by construction.
    assert [row["transcript_id"] for row in unattributed["result"]] == [3]


# --- Three outcomes, kept apart -------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/v1/archive/corpora/1/projects",
    "/api/v1/archive/corpora/1/unattributed",
    "/api/v1/archive/projects/1/transcripts",
    "/api/v1/archive/transcripts/1/lines",
    "/api/v1/archive/transcripts/1/subagents",
    "/api/v1/archive/transcripts/1/messages",
])
def test_a_malformed_cursor_is_400_cannot_determine_not_a_page_one_reset(
    archive, path,
):
    """The whole point: a bad cursor must not silently start over."""
    with TestClient(_app()) as client:
        first = client.get(f"{path}?limit=1").json()
        bad = client.get(f"{path}?cursor={_bad_cursor()}")

    body = bad.json()
    assert bad.status_code == 400, (path, body)
    assert body["result_status"] == "cannot_determine"
    assert body["unevaluated"] and body["unevaluated"][0]["subject"] == "cursor"
    assert body["unevaluated"][0]["reason"]
    # Not a page-1 reset: the refusal carries NO rows at all, so it can
    # never be mistaken for the first page it would otherwise duplicate.
    assert body["result"] in (None, [])
    assert body["result"] != first["result"] or not first["result"]
    assert body["meta"]["paging"]["has_more"] is None


def test_search_scan_bytes_below_one_mib_is_refused_by_the_route(archive):
    """Issue (d): the core accepts >= 1; the ROUTE holds the 1 MiB floor."""
    from src.api.archive_search_routes import MIN_SCAN_BYTES

    with TestClient(_app()) as client:
        refused = client.get(
            f"/api/v1/archive/search?q=hello&project_id=1"
            f"&scan_bytes={MIN_SCAN_BYTES - 1}"
        )
        accepted = client.get(
            f"/api/v1/archive/search?q=hello&project_id=1"
            f"&scan_bytes={MIN_SCAN_BYTES}"
        )
    assert refused.status_code == 400
    body = refused.json()
    assert body["result_status"] == "cannot_determine"
    assert body["unevaluated"][0]["subject"] == "scan_bytes"
    # Positive control: the same request one byte higher is served, so
    # the refusal is the FLOOR talking and not a broken route.
    assert accepted.status_code == 200
    assert accepted.json()["result_status"] == "ok"


def test_search_requires_exactly_one_scope(archive):
    """Neither and both are both refusals, with different reasons."""
    with TestClient(_app()) as client:
        neither = client.get("/api/v1/archive/search?q=hello")
        both = client.get(
            "/api/v1/archive/search?q=hello&project_id=1&transcript_id=1"
        )
    for response, word in ((neither, "neither"), (both, "both")):
        assert response.status_code == 400
        body = response.json()
        assert body["result_status"] == "cannot_determine"
        assert body["unevaluated"][0]["subject"] == "scope"
        assert word in body["unevaluated"][0]["reason"]


def test_search_reports_all_four_scan_statuses_as_a_vocabulary(archive):
    """Issue (c): four values, and not_run's counts are None, never 0."""
    from src.core.archive_search import (
        SCAN_BUDGET_EXHAUSTED, SCAN_LIMIT_REACHED,
    )

    vocabulary = {
        SCAN_COMPLETE, SCAN_BUDGET_EXHAUSTED, SCAN_LIMIT_REACHED, SCAN_NOT_RUN,
    }
    with TestClient(_app()) as client:
        hit = client.get(
            "/api/v1/archive/search?q=hello&project_id=1"
        ).json()
        missing_scope = client.get(
            "/api/v1/archive/search?q=hello&project_id=9999"
        )

    assert hit["meta"]["scan"]["status"] in vocabulary
    assert hit["meta"]["scan"]["status"] == SCAN_COMPLETE
    assert hit["meta"]["scan"]["transcripts_scanned"] == 2

    assert missing_scope.status_code == 404
    scan = missing_scope.json()["meta"]["scan"]
    assert scan["status"] == SCAN_NOT_RUN
    # None, not 0. A zero here would be a measurement nobody took.
    assert scan["transcripts_scanned"] is None
    assert scan["bytes_scanned"] is None
    assert scan["elapsed_seconds"] is None


def test_a_missing_scope_is_404_not_an_empty_ok(archive):
    """not_found and 'genuinely empty' must not render the same."""
    with TestClient(_app()) as client:
        missing = client.get("/api/v1/archive/projects/9999/transcripts")
        empty = client.get("/api/v1/archive/transcripts/1/subagents")

    assert missing.status_code == 404
    assert missing.json()["result_status"] == "not_found"
    assert missing.json()["scope_status"] == "not_found"
    # has_more is null, never false: false claims the end of a list that
    # was never read.
    assert missing.json()["meta"]["paging"]["has_more"] is None

    assert empty.status_code == 200
    assert empty.json()["result_status"] == "ok"
    assert empty.json()["result"] == []
    assert empty.json()["meta"]["paging"]["has_more"] is False


def test_an_unknown_filter_value_is_cannot_determine_not_an_empty_ok(archive):
    """'there is no such model' and 'no line used it' are different."""
    with TestClient(_app()) as client:
        unknown = client.get(
            "/api/v1/archive/transcripts/1/lines?model=gpt-4"
        )
        real = client.get("/api/v1/archive/transcripts/1/lines?role=user")
    assert unknown.status_code == 400
    assert unknown.json()["result_status"] == "cannot_determine"
    assert unknown.json()["unevaluated"][0]["subject"] == "filter:model"
    # Positive control: a filter value that DOES exist is served.
    assert real.status_code == 200
    assert real.json()["result_status"] == "ok"


# --- Parameter bounds ------------------------------------------------------


@pytest.mark.parametrize("path,maximum", [
    ("/api/v1/archive/corpora/1/projects", MAX_PAGE_LIMIT),
    ("/api/v1/archive/projects/1/transcripts", MAX_PAGE_LIMIT),
    ("/api/v1/archive/transcripts/1/lines", MAX_LINE_LIMIT),
    ("/api/v1/archive/transcripts/1/subagents", MAX_PAGE_LIMIT),
])
def test_page_limits_are_clamped_and_the_effective_value_is_reported(
    archive, path, maximum,
):
    """The doc clamps rather than rejects, and REPORTS what it clamped to."""
    with TestClient(_app()) as client:
        high = client.get(f"{path}?limit=99999").json()
        low = client.get(f"{path}?limit=0").json()
    assert high["meta"]["paging"]["limit"] == maximum
    assert low["meta"]["paging"]["limit"] == 1


def test_max_page_bytes_out_of_range_is_rejected_by_validation(archive):
    """Bounded declaratively, so it is refused before any SQL runs."""
    with TestClient(_app()) as client:
        assert client.get(
            "/api/v1/archive/transcripts/1/lines?max_page_bytes=1"
        ).status_code == 422
        assert client.get(
            "/api/v1/archive/transcripts/1/lines?max_page_bytes=99999999"
        ).status_code == 422


# --- Bodies and secrets ----------------------------------------------------


def test_a_body_is_returned_whole_asserted_by_equality(archive):
    """Never a PREFIX. Compared by == against the stored value, not startswith."""
    conn = sqlite3.connect(str(db_path_for(archive["state_dir"])))
    try:
        stored = conn.execute(
            "SELECT body_json FROM message_bodies WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    with TestClient(_app()) as client:
        body = client.get("/api/v1/archive/bodies/1").json()
    assert body["result"]["body_json"] == stored
    assert body["result"]["body_state"] == "included"
    assert body["result"]["secrets"] == []


def test_lines_can_carry_whole_bodies_and_says_it_did(archive):
    """include_bodies is reported in meta, so a client never has to guess."""
    with TestClient(_app()) as client:
        page = client.get(
            "/api/v1/archive/transcripts/1/lines?include_bodies=true"
        ).json()
    assert page["meta"]["bodies"]["included"] is True
    assert page["meta"]["bodies"]["stopped_early"] is False
    assert all(row["body_state"] == "included" for row in page["result"])


# --- Subagents: the line_no gap -------------------------------------------


def test_subagents_rows_carry_line_no(archive):
    """``subagent_edges`` does not return one; the route's own query does."""
    with TestClient(_app()) as client:
        page = client.get("/api/v1/archive/transcripts/2/subagents").json()
    assert page["result_status"] == "ok"
    assert len(page["result"]) == 1
    row = page["result"][0]
    assert row["line_no"] == 2
    assert row["agent_id"] == "agent-x"
    assert row["is_sidechain"] is True
    assert isinstance(page["meta"]["lineage"]["parent_transcripts"], list)


# --- Export, both forms ----------------------------------------------------


def test_streamed_export_is_byte_identical_and_carries_expected_hash(archive):
    """The stream reproduces the stored bytes AND advertises the hash."""
    with TestClient(_app()) as client:
        response = client.get("/api/v1/archive/transcripts/1/export")
    assert response.status_code == 200
    assert response.content == archive["content"].encode("utf-8")
    assert response.headers["x-archive-expected-sha256"] == archive["content_sha256"]
    assert response.headers["x-archive-expected-bytes"] == str(
        archive["raw_byte_length"]
    )
    assert sha256_text(response.text) == archive["content_sha256"]
    # Trailers are unavailable on this stack (uvicorn implements no
    # http.response.trailers extension). The route must SAY so rather
    # than advertise a Trailer header that never arrives.
    assert response.headers["x-archive-verification"] in (
        "trailer", "expected_only",
    )
    if response.headers["x-archive-verification"] == "expected_only":
        assert response.headers["x-archive-trailer-unavailable"]


def test_streamed_export_declares_no_content_length(archive):
    """REGRESSION. A ``content-length: 0`` made the client keep NOTHING.

    ``Response.__init__`` is constructed with no body, so it stamps
    ``content-length: 0``. The server then streamed 182,077,926 bytes and
    logged ``bytes_sent=182077926, verified=true`` while curl saved a
    ZERO-BYTE file, because it stopped reading at the declared length.
    Every server-side signal was green; the defect was only visible from
    the wire. The real length is unknowable before the last line is
    rendered, so the header must be ABSENT and the response chunked.

    This assertion is on the HEADER, not on the body, on purpose: the
    test client's ASGI transport reads every body message regardless of
    ``content-length``, so a body comparison passes even while a real
    HTTP client keeps nothing. Asserting the body here would be a check
    that cannot fail on the thing that actually broke.
    """
    with TestClient(_app()) as client:
        response = client.get("/api/v1/archive/transcripts/1/export")
    assert "content-length" not in {k.lower() for k in response.headers}
    assert len(response.content) == archive["raw_byte_length"]


def test_streamed_export_equals_the_buffered_rendering_path(archive):
    """One rendering path: the stream and export_transcript must agree."""
    conn = connect(db_path_for(archive["state_dir"]), create=False)
    try:
        buffered = export_transcript(conn, 1).text
    finally:
        conn.close()
    with TestClient(_app()) as client:
        streamed = client.get("/api/v1/archive/transcripts/1/export").text
    assert streamed == buffered


def test_verified_export_verifies_before_sending(archive):
    """The actual hash is a real HEADER here: it is known before the send."""
    with TestClient(_app()) as client:
        response = client.get("/api/v1/archive/transcripts/1/export/verified")
    assert response.status_code == 200
    assert response.content == archive["content"].encode("utf-8")
    assert response.headers["x-archive-verification"] == "before_send"
    assert response.headers["x-archive-actual-sha256"] == archive["content_sha256"]
    assert response.headers["x-archive-verified"] == "true"


def test_verified_export_refuses_above_the_cap_and_names_the_stream(archive):
    """413 and a cannot_determine. It must NOT fall back to streaming."""
    conn = connect(db_path_for(archive["state_dir"]), create=False)
    try:
        conn.execute(
            "UPDATE message_transcripts SET raw_byte_length = ? WHERE id = 1",
            (VERIFY_BEFORE_SEND_MAX_BYTES + 1,),
        )
        conn.commit()
    finally:
        conn.close()
    with TestClient(_app()) as client:
        response = client.get("/api/v1/archive/transcripts/1/export/verified")
    assert response.status_code == 413
    body = response.json()
    assert body["result_status"] == "cannot_determine"
    assert body["result"] is None
    assert str(VERIFY_BEFORE_SEND_MAX_BYTES) in body["unevaluated"][0]["reason"]
    assert body["meta"]["stream_href"].endswith("/transcripts/1/export")


@pytest.mark.parametrize("path", [
    "/api/v1/archive/transcripts/9999/export",
    "/api/v1/archive/transcripts/9999/export/verified",
])
def test_export_of_a_missing_transcript_is_404_with_an_envelope(archive, path):
    """Not an empty file, which a client would happily save."""
    with TestClient(_app()) as client:
        response = client.get(path)
    assert response.status_code == 404
    body = response.json()
    assert body["result_status"] == "not_found"
    assert body["scope_status"] == "not_found"


# --- Could-not-evaluate is not an empty list ------------------------------


@pytest.mark.parametrize("path", ENVELOPE_ROUTES)
def test_an_unopenable_datastore_is_cannot_determine_not_an_empty_ok(
    tmp_path, monkeypatch, path,
):
    """The single most important distinction in this API, on every route."""
    from src.config import settings

    empty = tmp_path / "no-such-state"
    empty.mkdir()
    monkeypatch.setattr(type(settings), "get_state_dir", lambda self: empty)
    with TestClient(_app()) as client:
        response = client.get(path)
    assert response.status_code == 200, (path, response.text[:300])
    body = response.json()
    assert body["result_status"] == "cannot_determine", path
    assert body["scope_status"] == "cannot_determine", path
    # NOT an empty list. A client iterating result must not render a
    # confident empty state over a question nobody answered.
    assert body["result"] is None, path
    assert body["unevaluated"], path
