"""The import actually USES the evidence ladder, and the latch is versioned.

The pure-rule proofs live in tests/test_session_import_ladder.py. These
are the wiring proofs: what lands in the sessions table, what lands in
``meta.session_import_unattributed``, and what a version bump does to
rows the user has already answered for.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, get_meta
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    META_SESSION_IMPORT_UNATTRIBUTED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_import import (
    EVIDENCE_LADDER_VERSION,
    IMPORT_ALREADY_DONE,
    IMPORT_COMPLETED,
    IMPORT_RERUN_COMPLETED,
    RESULT_KEY_SESSIONS_EVIDENCE_VERSION,
    RESULT_KEY_SESSIONS_STAGE,
    run_first_run_import,
    sessions_stage_version,
)
from src.core.session_import_ladder import (
    REASON_COULD_NOT_EVALUATE,
    REASON_NO_EVIDENCE,
)
from src.core.session_import_promote import record_decline
from src.core.session_stage_a_boundary import read_boundary, record_boundary
from src.core.session_store import list_sessions
from src.core.tmux_listing import TmuxListing


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db at the current schema version."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _live(name, epoch):
    """One attachable-listing row."""
    return {"name": name, "created_at_epoch": epoch, "window_count": 1}


def _logdir(tmp_path, *filenames):
    """A throwaway LOG_DIRECTORY holding the given pipe files."""
    d = tmp_path / "logs"
    d.mkdir(parents=True, exist_ok=True)
    for fn in filenames:
        (d / fn).write_text("")
    return d


def _row(conn, name):
    """The one stored row with this tmux name."""
    return [r for r in list_sessions(conn) if r["tmux_name"] == name][0]


def _unattributed(conn):
    """The parsed session_import_unattributed meta value."""
    raw = get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED)
    return json.loads(raw) if raw else None


# ---- Stage B wiring -------------------------------------------------------

def test_a_created_pipe_imports_the_session_as_OURS(conn, tmp_path):
    """Tier 3 on the wire: the pipe file the app wrote about ITSELF."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_1a2b3c4d", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path, "tmux_ses_1a2b3c4d.pipe"),
    )
    row = _row(conn, "cloude_ses_1a2b3c4d")
    assert row["origin"] == SESSION_ORIGIN_CREATED
    assert row["lifecycle_source"] == "import:created_pipe"
    assert _unattributed(conn) == []


def test_an_ext_pipe_ALONE_leaves_the_session_unattributed(conn, tmp_path):
    """THE REGRESSION GUARD, on the wire. This is the live install's exact
    shape: five ext_ pipes, five rows, no created pipe anywhere. They must
    land UNKNOWN and be asked about, never written external on the
    strength of a verdict the bug produced."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_scrolltest", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path, "tmux_ext_cloude_scrolltest.pipe"),
    )
    row = _row(conn, "cloude_scrolltest")
    assert row["origin"] == SESSION_ORIGIN_OBSERVED
    assert row["lifecycle_source"] == "import"
    records = _unattributed(conn)
    assert [r["tmux_name"] for r in records] == ["cloude_scrolltest"]
    assert records[0]["reason"] == REASON_NO_EVIDENCE


def test_both_pipes_is_OURS_by_the_created_pipe(conn, tmp_path):
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_ec5bf2a3", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(
            tmp_path,
            "tmux_ses_ec5bf2a3.pipe",
            "tmux_ext_cloude_ses_ec5bf2a3.pipe",
        ),
    )
    assert _row(conn, "cloude_ses_ec5bf2a3")["origin"] == SESSION_ORIGIN_CREATED


def test_an_unreadable_log_directory_is_could_not_evaluate_not_no_evidence(
    conn, tmp_path
):
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_a", 1000)]),
        owned_tmux_names=set(),
        log_dir=tmp_path / "does-not-exist",
    )
    records = _unattributed(conn)
    assert records[0]["reason"] == REASON_COULD_NOT_EVALUATE


def test_the_unattributed_record_shape_is_exactly_what_the_prompt_renders(
    conn, tmp_path
):
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_deadbeef", 4242)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
    )
    rec = _unattributed(conn)[0]
    assert set(rec) == {"tmux_name", "epoch", "hints", "reason"}
    assert rec["epoch"] == 4242
    assert any("auto-generated" in h for h in rec["hints"])


def test_an_empty_list_and_an_absent_key_are_different_facts(conn, tmp_path):
    assert get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED) is None
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
    )
    assert _unattributed(conn) == []


# ---- Stage A boundary -----------------------------------------------------

def test_the_boundary_is_stamped_once_and_never_moves(conn):
    first = record_boundary(conn, now_epoch=1000)
    second = record_boundary(conn, now_epoch=9999)
    assert first == 1000
    assert second == 1000
    assert read_boundary(conn) == 1000


def test_an_absent_boundary_reads_as_CANNOT_DETERMINE_not_zero(conn):
    assert read_boundary(conn) is None


def test_an_unparseable_boundary_reads_as_CANNOT_DETERMINE(conn):
    from src.core.db import set_meta
    from src.core.db_models import META_STAGE_A_BOUNDARY_EPOCH

    set_meta(conn, META_STAGE_A_BOUNDARY_EPOCH, "not-a-number")
    assert read_boundary(conn) is None


def test_a_marker_on_a_session_older_than_the_boundary_is_ignored(conn, tmp_path):
    """Tier 4's epoch gate, on the wire. The session predates the write
    site, so nothing this app runs could have stamped it."""
    record_boundary(conn, now_epoch=2000)
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_old", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
        origin_probe=lambda name: "created",
    )
    assert _row(conn, "cloude_old")["origin"] == SESSION_ORIGIN_OBSERVED
    assert _unattributed(conn)[0]["reason"] == REASON_NO_EVIDENCE


def test_a_marker_after_the_boundary_imports_as_OURS(conn, tmp_path):
    record_boundary(conn, now_epoch=2000)
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_new", 3000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
        origin_probe=lambda name: "created",
    )
    row = _row(conn, "cloude_new")
    assert row["origin"] == SESSION_ORIGIN_CREATED
    assert row["lifecycle_source"] == "import:origin_marker"


def test_a_marker_with_NO_boundary_is_inadmissible_not_assumed_valid(
    conn, tmp_path
):
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_x", 3000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
        origin_probe=lambda name: "created",
    )
    assert _row(conn, "cloude_x")["origin"] == SESSION_ORIGIN_OBSERVED
    assert _unattributed(conn)[0]["reason"] == REASON_COULD_NOT_EVALUATE


# ---- Stage D: the versioned latch ----------------------------------------

def _first_pass(conn, tmp_path, names, log_files=()):
    """Run the import once and then pretend an OLDER ladder produced it."""
    result = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live(n, e) for n, e in names]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path, *log_files),
    )
    assert result.outcome == IMPORT_COMPLETED
    _downgrade_stamped_version(conn)
    return result


def _downgrade_stamped_version(conn):
    """Rewrite the stamped evidence version to one below the current."""
    from src.core.db import get_meta as _g
    from src.core.db import set_meta as _s
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT

    blob = json.loads(_g(conn, META_IMPORTED_FROM_JSON_RESULT))
    blob[RESULT_KEY_SESSIONS_EVIDENCE_VERSION] = EVIDENCE_LADDER_VERSION - 1
    _s(conn, META_IMPORTED_FROM_JSON_RESULT, json.dumps(blob, sort_keys=True))


def test_a_stamp_with_NO_version_key_reads_as_version_zero(conn, tmp_path):
    """The install this work exists to rescue. Reading its latch as
    current would lock it out of every future improvement."""
    from src.core.db import get_meta as _g
    from src.core.db import set_meta as _s
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT

    _s(
        conn,
        META_IMPORTED_FROM_JSON_RESULT,
        json.dumps({RESULT_KEY_SESSIONS_STAGE: "2026-08-18T18:41:13Z"}),
    )
    assert sessions_stage_version(conn) == 0


def test_a_second_run_at_the_SAME_version_is_already_done(conn, tmp_path):
    _first_pass(conn, tmp_path, [("cloude_a", 1000)])
    # restore the current version, then re-run
    from src.core.db import get_meta as _g
    from src.core.db import set_meta as _s
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT

    blob = json.loads(_g(conn, META_IMPORTED_FROM_JSON_RESULT))
    blob[RESULT_KEY_SESSIONS_EVIDENCE_VERSION] = EVIDENCE_LADDER_VERSION
    _s(conn, META_IMPORTED_FROM_JSON_RESULT, json.dumps(blob))
    out = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_a", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
    )
    assert out.outcome == IMPORT_ALREADY_DONE


def test_a_version_bump_PROMOTES_an_observed_row_the_new_ladder_can_prove(
    conn, tmp_path
):
    _first_pass(conn, tmp_path, [("cloude_ses_1a2b3c4d", 1000)])
    assert _row(conn, "cloude_ses_1a2b3c4d")["origin"] == SESSION_ORIGIN_OBSERVED

    out = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_1a2b3c4d", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path, "tmux_ses_1a2b3c4d.pipe"),
    )
    assert out.outcome == IMPORT_RERUN_COMPLETED
    assert out.promoted == 1
    row = _row(conn, "cloude_ses_1a2b3c4d")
    assert row["origin"] == SESSION_ORIGIN_CREATED
    assert row["lifecycle_source"] == "import:rerun:created_pipe"
    assert _unattributed(conn) == []


def test_a_DECLINED_row_survives_a_latch_version_bump(conn, tmp_path):
    """THE REGRESSION GUARD. "Leave as external" is an answer, and an
    answer that gets re-asked on every boot is not one."""
    _first_pass(conn, tmp_path, [("cloude_ses_1a2b3c4d", 1000)])
    assert (
        record_decline(
            conn, name="cloude_ses_1a2b3c4d", epoch=1000, now="2026-08-23T00:00:00Z"
        )
        == "applied"
    )

    out = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_1a2b3c4d", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path, "tmux_ses_1a2b3c4d.pipe"),
    )
    assert out.outcome == IMPORT_RERUN_COMPLETED
    assert out.promoted == 0
    row = _row(conn, "cloude_ses_1a2b3c4d")
    assert row["origin"] == SESSION_ORIGIN_OBSERVED
    assert row["user_declined_at"] == "2026-08-23T00:00:00Z"
    assert _unattributed(conn) == []


def test_an_OURS_row_is_NEVER_demoted_by_a_re_run(conn, tmp_path):
    """THE REGRESSION GUARD. A re-run with worse evidence must leave a
    proved row exactly as it found it."""
    _first_pass(
        conn,
        tmp_path,
        [("cloude_ses_1a2b3c4d", 1000)],
        log_files=("tmux_ses_1a2b3c4d.pipe",),
    )
    assert _row(conn, "cloude_ses_1a2b3c4d")["origin"] == SESSION_ORIGIN_CREATED

    # The evidence is GONE on the re-run: no pipe file at all.
    out = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_ses_1a2b3c4d", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path / "empty"),
    )
    assert out.outcome == IMPORT_RERUN_COMPLETED
    assert _row(conn, "cloude_ses_1a2b3c4d")["origin"] == SESSION_ORIGIN_CREATED


def test_an_ADOPTED_row_is_never_touched_by_a_re_run(conn, tmp_path):
    from src.core.session_identity import record_instance

    record_instance(
        conn,
        socket="cloude",
        name="cloude_adopted",
        epoch=1000,
        origin=SESSION_ORIGIN_ADOPTED,
        now="2026-08-01T00:00:00Z",
    )
    from src.core.db import set_meta as _s
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT

    _s(
        conn,
        META_IMPORTED_FROM_JSON_RESULT,
        json.dumps({RESULT_KEY_SESSIONS_STAGE: "2026-08-18T00:00:00Z"}),
    )
    out = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_adopted", 1000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
    )
    assert out.outcome == IMPORT_RERUN_COMPLETED
    assert _row(conn, "cloude_adopted")["origin"] == SESSION_ORIGIN_ADOPTED
    assert _unattributed(conn) == []


def test_a_re_run_INSERTS_nothing(conn, tmp_path):
    """A re-run must not resurrect a session the user has since deleted."""
    _first_pass(conn, tmp_path, [("cloude_a", 1000)])
    before = {r["tmux_name"] for r in list_sessions(conn)}
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_brand_new", 2000)]),
        owned_tmux_names=set(),
        log_dir=_logdir(tmp_path),
    )
    assert {r["tmux_name"] for r in list_sessions(conn)} == before


def test_DEMOTION_IS_UNIMPLEMENTABLE_not_merely_unused():
    """STRUCTURAL. No write in the promote module may set origin to
    ``observed``.

    A behavioural test can only prove that today's code paths do not
    demote. This proves the module contains no way to, so a future edit
    that adds one fails here rather than shipping a re-run that can
    silently disown a session the user has already been told is his.
    """
    import ast

    path = ROOT / "src" / "core" / "session_import_promote.py"
    tree = ast.parse(path.read_text())

    origin_writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "execute":
            continue
        sql = " ".join(
            n.value
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        if "SET origin" not in sql:
            continue
        names = {
            n.id
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id.startswith("SESSION_ORIGIN_")
        }
        origin_writes.append((node.lineno, names))

    assert origin_writes, "found no origin write at all; has the module moved?"
    for lineno, names in origin_writes:
        assert "SESSION_ORIGIN_CREATED" in names, (
            f"the origin write at line {lineno} does not set "
            f"SESSION_ORIGIN_CREATED; it uses {sorted(names)}. Promote is "
            "the only direction this module may move a row"
        )


def test_NO_log_directory_at_all_is_could_not_evaluate(conn):
    """A caller that cannot say where the pipe files are has not told us
    they are absent. Tier 3 is UNEVALUATED, and the session is reported as
    a measurement we could not take rather than one that found nothing."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_a", 1000)]),
        owned_tmux_names=set(),
        log_dir=None,
    )
    assert _unattributed(conn)[0]["reason"] == REASON_COULD_NOT_EVALUATE
