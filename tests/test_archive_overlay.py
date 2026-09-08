"""The presentation overlay: identity, three outcomes, and immutability.

THE CENTRAL ASSERTION OF THIS FILE IS A HASH, NOT AN ARGUMENT. Every test
that performs an overlay operation brackets it with
:func:`archive_fingerprint`, which sha256s the FULL CONTENTS of every
archive table the feature is forbidden to write. A comment saying "this
does not touch the archive" is a claim; a matching digest either side of
a rename, a grouping and a hide is a measurement. The table list comes
from ``ARCHIVE_TABLES_NEVER_WRITTEN`` rather than being retyped here, so
the check and the rule cannot drift.

The second theme is that a test must be able to FAIL. Several assertions
here look tautological until you know what they are pointed at - the
overlay-status test, for instance, would pass on a build that returned a
constant 'none', so it also asserts the 'applied' case in the same run
against the same data. Each test in this file was deliberately broken and
observed red before being restored; the mutations are recorded in the
task report.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ovl_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ovl_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.archive_overlay_routes import router as overlay_router
from src.api.auth import require_auth
from src.config import settings
from src.core.archive_overlay import (
    OVERLAY_STATUS_APPLIED,
    OVERLAY_STATUS_NONE,
    apply_overlay,
    identity_key,
    key_for_node,
    load_overlay,
)
from src.core.archive_overlay_ddl import (
    ARCHIVE_TABLES_NEVER_WRITTEN,
    DDL_V19,
    OVERLAY_TABLE,
)
from src.core.archive_overlay_write import (
    OverlayWriteError,
    normalise_label,
    open_read_write,
    set_display_name,
    set_group,
    set_hidden,
    write_one,
)
from src.core.archive_project_names import merge_projects
from src.core.archive_read import open_read_only
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from tests.archive_fixture import make_state_dir

#: A project on both machines, one on one machine, and one with a NULL
#: observed_cwd - the three identity shapes the key has to survive.
CWD_SHARED = "/Users/j/Development/Shared"
CWD_SOLO = "/Users/j/Development/Solo"


def _seed(state_dir: Path) -> None:
    """Populate two hosts, three corpora and four project rows.

    Description: the shape that makes identity testable rather than a
      convenient one. CWD_SHARED appears on BOTH hosts, so a merge that
      keyed on the project id would show it twice and a rename would
      only take on one of them. One project has a NULL observed_cwd, so
      the project_id fallback key is exercised rather than assumed.
    Inputs: state_dir (Path) - a migrated state directory.
    Output: None.
    Example: _seed(make_state_dir(tmp_path))
    """
    with closing(sqlite3.connect(str(state_dir / "cloude.db"))) as conn:
        conn.executescript(
            f"""
            INSERT INTO message_hosts
              (id, machine_id, machine_id_scheme, display_name, first_seen_at)
            VALUES (1, 'm-1', 'declared', 'Host-A', '2026-09-01T00:00:00Z'),
                   (2, 'm-2', 'declared', 'Host-B', '2026-09-01T00:00:00Z');
            INSERT INTO message_corpora
              (id, host_id, corpus_key, root_path, collected_at)
            VALUES (1, 1, 'k1', '/r1', '2026-09-01T00:00:00Z'),
                   (2, 2, 'k2', '/r2', '2026-09-01T00:00:00Z');
            INSERT INTO message_projects
              (id, corpus_id, slug, observed_cwd, first_seen_at)
            VALUES (1, 1, '-Users-j-Development-Shared', '{CWD_SHARED}',
                    '2026-09-01T00:00:00Z'),
                   (2, 2, '-Users-j-Development-Shared', '{CWD_SHARED}',
                    '2026-09-01T00:00:00Z'),
                   (3, 1, '-Users-j-Development-Solo', '{CWD_SOLO}',
                    '2026-09-01T00:00:00Z'),
                   (4, 1, '-Users-j-Nowhere', NULL, '2026-09-01T00:00:00Z');
            -- Transcripts exist so the SESSION COUNT is exercised through
            -- the overlay route rather than only through the raw one. The
            -- shared project gets one uuid transcript on EACH host, so a
            -- count that reported a single member's figure would read 1
            -- and the merged truth is 2. Solo gets an agent sidechain
            -- ONLY, which is a measured zero - the case the live corpus
            -- does not contain and therefore never exercises.
            INSERT INTO message_transcripts
              (id, corpus_id, host_id, project_id, source_path, source_ref,
               session_ref, session_ref_scheme, line_ending,
               has_trailing_newline, line_count, content_sha256,
               raw_byte_length, ingested_at)
            VALUES
              (1, 1, 1, 1, '/r1/a.jsonl', 'r1:a', 'u-1', 'uuid', 'LF', 1, 1,
               'a1', 10, '2026-09-01T00:00:00Z'),
              (2, 2, 2, 2, '/r2/b.jsonl', 'r2:b', 'u-2', 'uuid', 'LF', 1, 1,
               'a2', 10, '2026-09-01T00:00:00Z'),
              (3, 1, 1, 3, '/r1/c.jsonl', 'r1:c', 'ag-1', 'agent', 'LF', 1, 1,
               'a3', 10, '2026-09-01T00:00:00Z');
            """
        )
        conn.commit()


def archive_fingerprint(state_dir: Path) -> Dict[str, str]:
    """sha256 every row of every archive table the overlay may not write.

    Description: the measurement this whole file rests on. It hashes the
      FULL CONTENTS, in rowid order, of each table in
      ``ARCHIVE_TABLES_NEVER_WRITTEN`` - not a row count, which would be
      unchanged by an UPDATE, and not a table digest SQLite provides,
      because it does not provide one. A table that does not exist on
      this fixture hashes to the literal 'absent', which is a stable
      value that still changes if the table is later created.
    Inputs: state_dir (Path).
    Output: dict - table name -> hex digest.
    Example: archive_fingerprint(sd)['message_projects']
    """
    digests: Dict[str, str] = {}
    with closing(sqlite3.connect(str(state_dir / "cloude.db"))) as conn:
        for table in ARCHIVE_TABLES_NEVER_WRITTEN:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if present is None:
                digests[table] = "absent"
                continue
            hasher = hashlib.sha256()
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                hasher.update(repr(tuple(row)).encode("utf-8"))
                hasher.update(b"\x1e")
            digests[table] = hasher.hexdigest()
    return digests


def _app() -> FastAPI:
    """Build a test app mounting only the overlay router, authed.

    Inputs: none. Output: FastAPI.
    Example: TestClient(_app()).get('/api/v1/archive/overlay/projects')
    """
    app = FastAPI()
    app.include_router(overlay_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A seeded, migrated archive with the app pointed at it.

    Inputs: tmp_path (Path), monkeypatch. Output: Path - the state dir.
    Example: def test_x(archive): ...
    """
    state_dir = make_state_dir(tmp_path)
    _seed(state_dir)
    monkeypatch.setenv("CLOUDE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(type(settings), "get_state_dir", lambda self: state_dir)
    return state_dir


def _nodes(client: TestClient, path: str = "/api/v1/archive/overlay/projects") -> List[Dict[str, Any]]:
    """GET a project list and return its result, asserting it was ok.

    Inputs: client (TestClient), path (str). Output: list[dict].
    Example: _nodes(client)[0]['display_name']
    """
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result_status"] == "ok", body
    return body["result"]


def _named(nodes: List[Dict[str, Any]], cwd: str) -> Dict[str, Any]:
    """Find the node for one observed_cwd.

    Inputs: nodes (list[dict]), cwd (str). Output: dict.
    Raises: AssertionError - not present.
    Example: _named(nodes, CWD_SOLO)['display_name'] -> 'Solo'
    """
    for node in nodes:
        if node.get("observed_cwd") == cwd:
            return node
    raise AssertionError(f"no node for {cwd}; got {[n.get('observed_cwd') for n in nodes]}")


# --- Identity --------------------------------------------------------------


def test_the_identity_key_is_the_same_key_the_merge_buckets_on(archive: Path):
    """A key per merged node, and no two nodes share one.

    The overlay attaches to the LOGICAL project, so the key has to
    partition project rows exactly as merge_projects does. Two
    declarations of one rule have to be audited, not trusted.
    """
    with closing(open_read_only(archive)) as conn:
        rows = [
            {
                "project_id": r["id"], "slug": r["slug"],
                "observed_cwd": r["observed_cwd"], "corpus_id": r["corpus_id"],
                "host_id": 1, "host_display_name": "H", "transcript_count": 0,
            }
            for r in conn.execute(
                "SELECT id, slug, observed_cwd, corpus_id FROM message_projects"
            )
        ]
    nodes = merge_projects(rows)
    keys = [key_for_node(n)[0] for n in nodes]
    assert len(keys) == len(set(keys)), f"two merged nodes share one key: {keys}"
    # 4 project rows, 3 logical projects: the cross-machine pair merged.
    assert len(nodes) == 3, [n["observed_cwd"] for n in nodes]
    assert f"cwd:{CWD_SHARED}" in keys
    assert "pid:4" in keys, keys


def test_a_project_with_no_cwd_falls_back_to_a_local_id_key():
    """The two key kinds are distinguishable, and the fallback is labelled."""
    assert identity_key("/a/b", 7) == ("cwd:/a/b", "cwd")
    assert identity_key(None, 7) == ("pid:7", "project_id")
    assert identity_key("   ", 7) == ("pid:7", "project_id")
    with pytest.raises(ValueError):
        identity_key(None, None)


# --- The overlay applied ---------------------------------------------------


def test_an_overlay_renames_a_project_and_keeps_the_archive_name(archive: Path):
    """A rename is presentation: both names come back, archive untouched."""
    before = archive_fingerprint(archive)
    client = TestClient(_app())
    key = f"cwd:{CWD_SOLO}"
    response = client.post(
        "/api/v1/archive/overlay/name",
        json={"identity_key": key, "display_name": "Renamed Solo"},
    )
    assert response.status_code == 200, response.text

    node = _named(_nodes(client), CWD_SOLO)
    assert node["display_name"] == "Renamed Solo"
    assert node["archive_display_name"] == "Solo"
    assert node["overlay"]["status"] == OVERLAY_STATUS_APPLIED
    assert node["overlay"]["applied"] == ["display_name"]
    assert archive_fingerprint(archive) == before


def test_a_project_with_no_overlay_row_is_untouched_and_says_so(archive: Path):
    """'none' is a measurement, not an inference from an unchanged name."""
    client = TestClient(_app())
    client.post(
        "/api/v1/archive/overlay/name",
        json={"identity_key": f"cwd:{CWD_SOLO}", "display_name": "Renamed Solo"},
    )
    nodes = _nodes(client)
    untouched = _named(nodes, CWD_SHARED)
    assert untouched["display_name"] == "Shared"
    assert untouched["archive_display_name"] == "Shared"
    assert untouched["overlay"]["status"] == OVERLAY_STATUS_NONE
    assert untouched["overlay"]["applied"] == []
    # And the OTHER node in the same response IS applied, so a build that
    # returned a constant 'none' could not pass this test.
    assert _named(nodes, CWD_SOLO)["overlay"]["status"] == OVERLAY_STATUS_APPLIED


def test_a_rename_takes_on_both_machines_at_once(archive: Path):
    """The cross-machine project is ONE node, so one rename covers it."""
    client = TestClient(_app())
    client.post(
        "/api/v1/archive/overlay/name",
        json={"identity_key": f"cwd:{CWD_SHARED}", "display_name": "One Name"},
    )
    node = _named(_nodes(client), CWD_SHARED)
    assert node["display_name"] == "One Name"
    assert len(node["members"]) == 2, node["members"]
    assert node["host_count"] == 2


# --- Hidden: excluded, listed, restorable ----------------------------------


def test_hidden_projects_leave_the_default_list_but_stay_retrievable(archive: Path):
    """Soft delete: filtered from one list, present in two others."""
    before = archive_fingerprint(archive)
    client = TestClient(_app())
    key = f"cwd:{CWD_SOLO}"
    assert client.post(
        "/api/v1/archive/overlay/hidden", json={"identity_key": key, "hidden": True}
    ).status_code == 200

    default = _nodes(client)
    assert CWD_SOLO not in [n.get("observed_cwd") for n in default]

    with_hidden = _nodes(client, "/api/v1/archive/overlay/projects?include_hidden=true")
    assert _named(with_hidden, CWD_SOLO)["overlay"]["hidden"] is True

    hidden_list = _nodes(client, "/api/v1/archive/overlay/hidden")
    assert [n["observed_cwd"] for n in hidden_list] == [CWD_SOLO]
    assert archive_fingerprint(archive) == before


def test_unhide_restores_the_project_and_its_name(archive: Path):
    """Reversible in practice: hide keeps the name, unhide brings it back."""
    client = TestClient(_app())
    key = f"cwd:{CWD_SOLO}"
    client.post("/api/v1/archive/overlay/name",
                json={"identity_key": key, "display_name": "Kept"})
    client.post("/api/v1/archive/overlay/hidden",
                json={"identity_key": key, "hidden": True})
    assert CWD_SOLO not in [n.get("observed_cwd") for n in _nodes(client)]

    client.post("/api/v1/archive/overlay/hidden",
                json={"identity_key": key, "hidden": False})
    node = _named(_nodes(client), CWD_SOLO)
    assert node["overlay"]["hidden"] is False
    assert node["display_name"] == "Kept", "unhide must not discard the rename"


def test_unhiding_a_project_with_nothing_else_said_prunes_the_row(archive: Path):
    """An overlay row that says nothing is removed, so 'none' stays honest."""
    client = TestClient(_app())
    key = f"cwd:{CWD_SOLO}"
    client.post("/api/v1/archive/overlay/hidden",
                json={"identity_key": key, "hidden": True})
    response = client.post("/api/v1/archive/overlay/hidden",
                           json={"identity_key": key, "hidden": False})
    assert response.json()["meta"]["overlay"]["outcome"] == "pruned"
    assert response.json()["result"] is None
    assert _named(_nodes(client), CWD_SOLO)["overlay"]["status"] == OVERLAY_STATUS_NONE


# --- Grouping --------------------------------------------------------------


def test_grouping_covers_a_group_of_one_and_a_project_in_no_group(archive: Path):
    """Both edges in one response: a one-member group, and an ungrouped project."""
    before = archive_fingerprint(archive)
    client = TestClient(_app())
    assert client.post(
        "/api/v1/archive/overlay/group",
        json={"identity_key": f"cwd:{CWD_SOLO}", "group": "Client work"},
    ).status_code == 200

    nodes = _nodes(client)
    assert _named(nodes, CWD_SOLO)["overlay"]["group"] == "Client work"
    assert _named(nodes, CWD_SHARED)["overlay"]["group"] is None

    groups = client.get("/api/v1/archive/overlay/groups").json()
    assert groups["result"] == [{"group": "Client work", "project_count": 1}]

    client.post("/api/v1/archive/overlay/group",
                json={"identity_key": f"cwd:{CWD_SHARED}", "group": "Client work"})
    groups = client.get("/api/v1/archive/overlay/groups").json()
    assert groups["result"] == [{"group": "Client work", "project_count": 2}]
    assert archive_fingerprint(archive) == before


def test_clearing_a_group_removes_it_when_its_last_member_leaves(archive: Path):
    """A group exists exactly as long as a project names it."""
    client = TestClient(_app())
    key = f"cwd:{CWD_SOLO}"
    client.post("/api/v1/archive/overlay/group",
                json={"identity_key": key, "group": "Temp"})
    client.post("/api/v1/archive/overlay/group",
                json={"identity_key": key, "group": None})
    assert client.get("/api/v1/archive/overlay/groups").json()["result"] == []


# --- Orphans ---------------------------------------------------------------


def test_an_overlay_row_for_an_absent_project_is_named_not_dropped(archive: Path):
    """It must not vanish silently, and it must not become a phantom node."""
    client = TestClient(_app())
    ghost = "cwd:/Users/j/Development/Deleted"
    client.post("/api/v1/archive/overlay/name",
                json={"identity_key": ghost, "display_name": "Ghost"})

    body = client.get("/api/v1/archive/overlay/projects").json()
    assert ghost not in [n["overlay"]["identity_key"] for n in body["result"]], (
        "an orphan overlay row was rendered as a project node"
    )
    orphans = body["meta"]["overlay"]["orphans"]
    assert [o["identity_key"] for o in orphans] == [ghost], orphans
    assert orphans[0]["display_name"] == "Ghost"

    rows = client.get("/api/v1/archive/overlay/rows").json()["result"]
    assert ghost in [r["identity_key"] for r in rows], "the row was deleted"


def test_an_orphan_reattaches_when_its_project_appears(archive: Path):
    """Keying on identity rather than a row id is what makes this work."""
    client = TestClient(_app())
    future = "cwd:/Users/j/Development/Later"
    client.post("/api/v1/archive/overlay/name",
                json={"identity_key": future, "display_name": "Arrived"})
    assert client.get(
        "/api/v1/archive/overlay/projects"
    ).json()["meta"]["overlay"]["orphans"]

    with closing(sqlite3.connect(str(archive / "cloude.db"))) as conn:
        conn.execute(
            "INSERT INTO message_projects (id, corpus_id, slug, observed_cwd, "
            "first_seen_at) VALUES (9, 1, '-later', ?, '2026-09-01T00:00:00Z')",
            ("/Users/j/Development/Later",),
        )
        conn.commit()

    body = client.get("/api/v1/archive/overlay/projects").json()
    assert body["meta"]["overlay"]["orphans"] == []
    assert _named(body["result"], "/Users/j/Development/Later")["display_name"] == "Arrived"


# --- The third outcome -----------------------------------------------------


def test_a_failed_overlay_read_is_cannot_determine_not_the_archive_names(
    archive: Path,
):
    """The non-negotiable: no silent fallback to un-overlaid names."""
    with closing(sqlite3.connect(str(archive / "cloude.db"))) as conn:
        conn.execute(f"DROP TABLE {OVERLAY_TABLE}")
        conn.commit()

    client = TestClient(_app())
    body = client.get("/api/v1/archive/overlay/projects").json()
    assert body["result_status"] == "cannot_determine", body
    assert body["result"] is None, "cannot_determine must never carry a list"
    subjects = [entry["subject"] for entry in body["unevaluated"]]
    assert "archive:overlay" in subjects, body["unevaluated"]


def test_apply_overlay_refuses_to_render_over_a_failed_load(archive: Path):
    """The core function has no degrade path for the caller to reach."""
    with closing(sqlite3.connect(str(archive / "cloude.db"))) as conn:
        conn.execute(f"DROP TABLE {OVERLAY_TABLE}")
        conn.commit()
    with closing(open_read_only(archive)) as conn:
        load = load_overlay(conn)
    assert load.ok is False
    assert load.rows is None, "a failed load must not present as an empty map"
    with pytest.raises(ValueError):
        apply_overlay([], load)


# --- Immutability ----------------------------------------------------------


def test_every_overlay_operation_leaves_the_archive_byte_identical(archive: Path):
    """Rename, group, hide, unhide, clear - one fingerprint either side."""
    before = archive_fingerprint(archive)
    client = TestClient(_app())
    key = f"cwd:{CWD_SHARED}"
    for path, payload in [
        ("name", {"identity_key": key, "display_name": "A"}),
        ("group", {"identity_key": key, "group": "G"}),
        ("hidden", {"identity_key": key, "hidden": True}),
        ("hidden", {"identity_key": key, "hidden": False}),
        ("group", {"identity_key": key, "group": None}),
        ("name", {"identity_key": key, "display_name": None}),
    ]:
        assert client.post(
            f"/api/v1/archive/overlay/{path}", json=payload
        ).status_code == 200
        assert archive_fingerprint(archive) == before, (
            f"POST /archive/overlay/{path} with {payload} mutated the archive"
        )
    # And the fingerprint is capable of CHANGING, so a constant digest
    # could not have produced the six passes above.
    with closing(sqlite3.connect(str(archive / "cloude.db"))) as conn:
        conn.execute("UPDATE message_projects SET slug = 'moved' WHERE id = 3")
        conn.commit()
    assert archive_fingerprint(archive) != before


# --- Writes are refused on a read-only connection --------------------------


@pytest.mark.parametrize(
    "writer,value",
    [(set_display_name, "X"), (set_group, "G"), (set_hidden, True)],
)
def test_every_write_is_refused_on_a_read_only_connection(archive: Path, writer, value):
    """query_only is not a convention here; SQLite enforces it."""
    with closing(open_read_only(archive)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            writer(conn, f"cwd:{CWD_SOLO}", "cwd", value)


def test_open_read_write_hands_out_a_connection_that_is_not_query_only(archive: Path):
    """The mirror of open_read_only's read-back: measured, not assumed."""
    with closing(open_read_write(archive)) as conn:
        assert int(conn.execute("PRAGMA query_only").fetchone()[0]) == 0


def test_the_read_module_cannot_import_a_writer():
    """A GET handler has no writing function in scope, structurally."""
    import src.core.archive_overlay as read_module

    import ast

    tree = ast.parse(Path(read_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("archive_overlay_write" in name for name in imported), (
        f"the read module imports the write module; a read path could then "
        f"reach a writer: {sorted(imported)}"
    )


# --- Labels ----------------------------------------------------------------


def test_a_blank_label_is_refused_rather_than_silently_clearing():
    """Clearing must be an explicit null, not a typo that looks like one."""
    assert normalise_label("  Work  ", field="group") == "Work"
    assert normalise_label(None, field="group") is None
    with pytest.raises(OverlayWriteError):
        normalise_label("   ", field="group")
    with pytest.raises(OverlayWriteError):
        normalise_label("x" * 201, field="display_name")


def test_a_refused_label_is_cannot_determine_not_a_500(archive: Path):
    """The server evaluated it and answered no; that is not a crash."""
    client = TestClient(_app())
    body = client.post(
        "/api/v1/archive/overlay/name",
        json={"identity_key": f"cwd:{CWD_SOLO}", "display_name": "   "},
    ).json()
    assert body["result_status"] == "cannot_determine"
    assert body["result"] is None
    assert body["unevaluated"][0]["subject"] == "overlay:display_name"


# --- Migration -------------------------------------------------------------


def test_the_v19_step_is_idempotent_across_repeated_runs(tmp_path: Path):
    """Re-running it after an interrupted attempt is a no-op, not an error."""
    path = tmp_path / "cloude.db"
    with closing(sqlite3.connect(str(path))) as conn:
        with conn:
            run_chain(conn, 0, CURRENT_SCHEMA_VERSION)
        first = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (OVERLAY_TABLE,)
        ).fetchone()
        assert first is not None, "v19 did not create the overlay table"
        conn.execute(
            f"INSERT INTO {OVERLAY_TABLE} (identity_key, identity_kind, "
            f"display_name, created_at, updated_at) "
            f"VALUES ('cwd:/x', 'cwd', 'Keep', 'now', 'now')"
        )
        conn.commit()
        for _ in range(3):
            for statement in DDL_V19:
                conn.execute(statement)
        assert conn.execute(
            f"SELECT display_name FROM {OVERLAY_TABLE} WHERE identity_key='cwd:/x'"
        ).fetchone()[0] == "Keep", "a re-run rewrote existing overlay data"


def test_the_v19_step_does_not_rewrite_any_existing_table(tmp_path: Path):
    """Additive only: v18's tables are byte-identical after v19 runs."""
    path = tmp_path / "cloude.db"
    with closing(sqlite3.connect(str(path))) as conn:
        with conn:
            run_chain(conn, 0, 18)
        before = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
        }
        with conn:
            run_chain(conn, 18, 19)
        after = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
        }
    for name, sql in before.items():
        assert after.get(name) == sql, f"v19 altered the definition of {name}"
    assert OVERLAY_TABLE in after and OVERLAY_TABLE not in before


# --- Route contract --------------------------------------------------------


def test_every_overlay_route_declares_response_model_none():
    """A response_model would silently DELETE unevaluated and meta."""
    offenders = [
        route.path
        for route in overlay_router.routes
        if getattr(route, "response_model", None) is not None
    ]
    assert not offenders, offenders


def test_every_overlay_route_carries_auth():
    """A write route with no auth is a worse bug than a read route with none."""
    for route in overlay_router.routes:
        assert getattr(route, "dependencies", None), (
            f"{route.path} carries no dependency, so it is not behind auth"
        )


def test_no_get_route_writes(archive: Path):
    """Every GET runs on a connection SQLite would refuse a write on."""
    before = archive_fingerprint(archive)
    client = TestClient(_app())
    get_paths = sorted({
        route.path for route in overlay_router.routes if "GET" in (route.methods or set())
    })
    assert len(get_paths) == 4, get_paths
    for path in get_paths:
        assert client.get(path.replace("/archive", "/api/v1/archive")).status_code == 200
    with closing(open_read_only(archive)) as conn:
        rows = conn.execute(f"SELECT COUNT(*) FROM {OVERLAY_TABLE}").fetchone()[0]
    assert rows == 0, "a GET route created an overlay row"
    assert archive_fingerprint(archive) == before


# --- The write seam --------------------------------------------------------


def test_write_one_reports_pruned_and_ok_as_different_outcomes(archive: Path):
    """An idempotent no-op is a real outcome, not a change nobody made."""
    key = f"cwd:{CWD_SOLO}"
    assert write_one(archive, key, "cwd", field="group", value="G")["outcome"] == "ok"
    result = write_one(archive, key, "cwd", field="group", value=None)
    assert result["outcome"] == "pruned"
    assert result["row"] is None
    with pytest.raises(OverlayWriteError):
        write_one(archive, key, "cwd", field="nonsense", value=1)


# ---------------------------------------------------------------------------
# The SESSION COUNT through the overlay route
#
# The rail calls THIS route, not /archive/projects, so the count has to
# survive the overlay. It is a passthrough by construction (_apply_to_node
# copies the node) which is exactly why it is worth an assertion: a
# passthrough that stops passing something through does not error.
# ---------------------------------------------------------------------------


def test_the_overlay_route_carries_the_session_count(archive: Path):
    """The route the rail actually calls must answer the card's question."""
    client = TestClient(_app())
    shared = _named(_nodes(client), CWD_SHARED)
    assert shared["session_counted"] is True
    assert shared["session_count"] == 2, (
        "one uuid transcript on each host; a node reporting a single "
        "member's count would read 1"
    )
    assert shared["transcript_count"] == 2


def test_the_overlay_route_reports_a_measured_zero_as_zero(archive: Path):
    """Solo holds an agent sidechain only. That is 0, not not-known."""
    solo = _named(_nodes(client := TestClient(_app())), CWD_SOLO)
    assert solo["session_count"] == 0
    assert solo["session_counted"] is True
    assert solo["transcript_count"] == 1
    del client


def test_a_rename_does_not_disturb_the_session_count(archive: Path):
    """The overlay decides the NAME. It has no opinion about the count."""
    client = TestClient(_app())
    before = _named(_nodes(client), CWD_SOLO)
    assert client.post(
        "/api/v1/archive/overlay/name",
        json={"identity_key": before["overlay"]["identity_key"],
              "display_name": "Renamed Solo"},
    ).status_code == 200
    after = _named(_nodes(client), CWD_SOLO)
    assert after["display_name"] == "Renamed Solo"
    assert after["session_count"] == before["session_count"]
    assert after["session_counted"] == before["session_counted"]
    assert after["transcript_count"] == before["transcript_count"]


def test_both_routes_report_the_same_counts(archive: Path):
    """The overlay presents; it does not recount.

    A divergence here would mean two screens showing different numbers
    for one project, with nothing on either screen saying why. The raw
    router is mounted alongside the overlay one for this test ONLY -
    every other test in this file mounts the overlay router alone, so
    that isolation is not weakened for them.
    """
    from src.api.archive_routes import router as archive_router

    app = FastAPI()
    app.include_router(archive_router, prefix="/api/v1")
    app.include_router(overlay_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    client = TestClient(app)

    raw = {n["full_path"]: n for n in _nodes(client, "/api/v1/archive/projects")}
    over = {n["full_path"]: n for n in _nodes(client)}
    assert set(raw) == set(over)
    assert len(raw) == 3
    for key, node in raw.items():
        assert node["session_count"] == over[key]["session_count"], key
        assert node["session_counted"] == over[key]["session_counted"], key
        assert node["transcript_count"] == over[key]["transcript_count"], key
    # And the counts are not all identical to each other, so a build that
    # returned one constant from both routes could not pass this.
    assert len({n["session_count"] for n in raw.values()}) > 1
