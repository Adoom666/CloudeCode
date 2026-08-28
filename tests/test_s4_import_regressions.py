"""Regression proofs for D5 to D8 of the S4 adversarial review.

The IMPORT path: the origin it assigns, the two keys of its once-only
latch, the socket it records, and what it does with a latch record it
cannot read.

D1, D2 and D3 are proved in tests/test_tmux_listing_parse.py and
tests/test_s4_regressions.py. The AST half of D6 is proved in
tests/test_session_import.py, next to the check it replaces.

Each test names its defect and asserts the failure CANNOT RECUR. Where
the risk is a future EDIT rather than a future input, the proof is
structural: a behavioural test only fails on cases somebody thought of.
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
from src.core.db import connect, db_path_for, get_meta, set_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    CURRENT_SCHEMA_VERSION,
    META_IMPORTED_FROM_JSON_AT,
    META_IMPORTED_FROM_JSON_RESULT,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import record_instance
from src.core.session_import import (
    IMPORT_COMPLETED,
    ImportLatchUnreadable,
    RESULT_KEY_SESSIONS_STAGE,
    run_first_run_import,
    sessions_stage_done,
)
from src.core.session_store import count_sessions, list_sessions
from src.core.tmux_listing import TmuxListing


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _live(name, epoch, session_id=None):
    """Build one attachable-listing row.

    Inputs: name (str), epoch (int), session_id (str | None).
    Output: dict.
    """
    row = {"name": name, "created_at_epoch": epoch, "window_count": 1}
    if session_id is not None:
        row["tmux_session_id"] = session_id
    return row


# ===========================================================================
# D5 - the import must not badge the user's own session EXTERNAL
# ===========================================================================


def test_a_persisted_OWNED_session_imports_as_created_not_observed(conn):
    """D5: step 5 must consult owned_tmux_names, exactly as step 4 does.

    ``session_metadata.json`` holds exactly ONE session, the most recently
    active, which for an app user is almost always one the app created.
    Hardcoding ``observed`` badged his last session EXTERNAL on the very
    upgrade the import exists to protect.
    """
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([]),
        owned_tmux_names={"cloude_mine"},
        persisted_sessions=[
            {"tmux_session": "cloude_mine", "id": "sess-1",
             "agent_type": "claude", "tmux_created_epoch": 1755000000},
        ],
    )
    row = list_sessions(conn)[0]
    assert row["tmux_name"] == "cloude_mine"
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert row["origin"] == SESSION_ORIGIN_CREATED


def test_a_persisted_UNOWNED_session_still_imports_as_observed(conn):
    """D5, the other half: the fix must not badge everything as ours."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([]),
        owned_tmux_names={"something_else"},
        persisted_sessions=[
            {"tmux_session": "not_mine", "id": "sess-2"},
        ],
    )
    assert list_sessions(conn)[0]["origin"] == SESSION_ORIGIN_OBSERVED


def test_the_import_NEVER_invents_an_adoption_on_either_step(conn):
    """D5: past adoptions were persisted nowhere, so importing one is invention."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_live", 1000)]),
        owned_tmux_names={"cloude_live", "cloude_dead"},
        persisted_sessions=[{"tmux_session": "cloude_dead", "id": "s"}],
    )
    origins = {row["origin"] for row in list_sessions(conn)}
    assert SESSION_ORIGIN_ADOPTED not in origins
    assert origins == {SESSION_ORIGIN_CREATED}


def test_BOTH_import_steps_use_the_SAME_origin_resolver():
    """D5, STRUCTURAL: the two steps must not drift apart again.

    The defect was that step 4 called ``observed_origin_for`` and step 5
    hardcoded a constant. A behavioural test catches today's divergence;
    this catches the reintroduction of ANY hardcoded origin on either
    write path.

    STAGE B WIDENED WHAT IS ALLOWED, BY EXACTLY ONE VALUE, AND THIS TEST
    NOW PINS THE WIDENING RATHER THAN BEING RELAXED FOR IT. Step 4 no
    longer passes ``observed_origin_for(...)`` inline, because the
    evidence ladder can now PROVE a session is ours from the created
    pipe or an epoch-gated marker, not only from the legacy owned set.
    So a NAME may be passed - but every assignment to that name is
    enumerated here and must be one of exactly two forms:

      * ``observed_origin_for(...)``  - the shared resolver, unchanged;
      * ``SESSION_ORIGIN_CREATED``    - and ONLY inside a branch guarded
        on the ladder verdict being LADDER_OURS.

    ``SESSION_ORIGIN_OBSERVED`` is deliberately NOT on that list. A path
    that hardcodes external is the original defect, and it stays
    unrepresentable here.
    """
    import ast

    source = (ROOT / "src" / "core" / "session_import.py").read_text()
    tree = ast.parse(source)

    def _is_shared_resolver(value_node):
        """True when the expression is a call to the shared origin resolver."""
        return (
            isinstance(value_node, ast.Call)
            and getattr(value_node.func, "id", None) == "observed_origin_for"
        )

    hardcoded = []
    indirect_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "record_instance":
            continue
        for keyword in node.keywords:
            if keyword.arg != "origin":
                continue
            if _is_shared_resolver(keyword.value):
                continue
            if isinstance(keyword.value, ast.Name):
                indirect_names.add(keyword.value.id)
                continue
            hardcoded.append(node.lineno)

    assert hardcoded == [], (
        "record_instance is called with an origin that is neither "
        "observed_origin_for(...) nor a name this test can audit, at "
        f"line(s) {hardcoded}. Every import write path must resolve "
        "origin the same way, or the user's own session gets badged "
        "external on one of them"
    )

    # Every assignment to an indirect origin name must be one of the two
    # allowed forms, and the CREATED form must sit inside a branch tested
    # on the ladder verdict. That is what stops
    # ``origin = SESSION_ORIGIN_CREATED`` being hoisted out of its guard.
    ladder_guarded = set()
    for outer in ast.walk(tree):
        if isinstance(outer, ast.If) and "LADDER_OURS" in ast.dump(outer.test):
            for inner in ast.walk(outer):
                ladder_guarded.add(id(inner))

    bad_assignments = []
    for name in sorted(indirect_names):
        seen = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                continue
            seen += 1
            value = node.value
            if _is_shared_resolver(value):
                continue
            if (
                isinstance(value, ast.Name)
                and value.id == "SESSION_ORIGIN_CREATED"
                and id(node) in ladder_guarded
            ):
                continue
            bad_assignments.append((name, node.lineno, ast.dump(value)[:80]))
        assert seen, f"origin name {name!r} is never assigned in this module"

    assert bad_assignments == [], (
        "an import write path resolves origin from something that is "
        "neither the shared resolver nor a ladder-proved 'created': "
        f"{bad_assignments}"
    )


# ===========================================================================
# D6 - the operative latch key and the reported one must agree
# ===========================================================================


def test_the_two_latch_keys_are_written_TOGETHER_or_not_at_all(conn):
    """D6: a completed import sets BOTH; a pending one sets NEITHER.

    The guard that actually runs reads ``imported_from_json_result``. The
    route reports ``imported_from_json_at``. Writing only the first
    latched the import shut while every reader said it had never
    completed, forever.
    """
    with transaction(conn):
        pending = run_first_run_import(
            conn, listing=TmuxListing.unavailable("timeout")
        )
    assert pending.pending is True
    assert sessions_stage_done(conn) is False
    assert get_meta(conn, META_IMPORTED_FROM_JSON_AT) is None

    with transaction(conn):
        done = run_first_run_import(
            conn, listing=TmuxListing.answered([])
        )
    assert done.outcome == IMPORT_COMPLETED
    assert sessions_stage_done(conn) is True
    assert get_meta(conn, META_IMPORTED_FROM_JSON_AT) is not None


def test_the_latch_helper_writes_BOTH_keys():
    """D6, STRUCTURAL: neither key may be dropped from the helper.

    If a future edit removes one write from ``_latch_sessions_stage``,
    the two keys silently disagree again and the AST proof, which only
    checks WHERE writes happen, would still pass.
    """
    import ast

    source = (ROOT / "src" / "core" / "session_import.py").read_text()
    tree = ast.parse(source)
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_latch_sessions_stage"
    )
    names = {
        getattr(sub, "id", None) for sub in ast.walk(helper)
    } | {
        getattr(sub, "attr", None) for sub in ast.walk(helper)
    }
    assert "META_IMPORTED_FROM_JSON_AT" in names, (
        "the helper no longer writes the key GET /sessions/import-status "
        "reports"
    )
    assert "RESULT_KEY_SESSIONS_STAGE" in names, (
        "the helper no longer writes the key sessions_stage_done() reads"
    )


# ===========================================================================
# D7 - the import must record the socket the probe actually ran against
# ===========================================================================


def test_the_import_records_the_socket_it_was_GIVEN(conn):
    """D7: rows must key on the probed socket, not the module default."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("a", 1000)]),
        socket="mysock",
    )
    assert list_sessions(conn)[0]["tmux_socket"] == "mysock"


def test_MAIN_PASSES_THE_CONFIGURED_SOCKET_to_the_import():
    """D7, STRUCTURAL: the call site is the defect, so the call site is the proof.

    src/main.py omitted ``socket=``, so rows took the module default while
    ``SessionManager._tmux_socket_name()`` read the CONFIGURED value back.
    A user with a custom ``session.tmux_socket_name`` therefore got an
    empty ``owned_instances()`` for the entire install and fell back to
    the name-only ownership tier permanently. A behavioural test cannot
    see this - only reading the call site can.
    """
    import ast

    source = (ROOT / "src" / "main.py").read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "run_first_run_import"
    ]
    assert len(calls) == 1, f"expected one import call site, found {len(calls)}"
    kwargs = {keyword.arg for keyword in calls[0].keywords}
    assert "socket" in kwargs, (
        "src/main.py calls run_first_run_import without socket=, so every "
        "imported row takes the module default rather than the socket the "
        "probe ran against and the manager reads back"
    )
    # and it must be the manager's own accessor, not a literal
    socket_arg = next(
        keyword.value for keyword in calls[0].keywords if keyword.arg == "socket"
    )
    assert isinstance(socket_arg, ast.Call), (
        "socket= must be resolved from the session manager, not hardcoded"
    )
    assert getattr(socket_arg.func, "attr", None) == "tmux_socket_name"


def test_the_manager_exposes_the_socket_name_publicly():
    """D7: main.py must not have to reach into a private attribute."""
    from src.core.session_manager import SessionManager

    assert callable(getattr(SessionManager, "tmux_socket_name", None))


# ===========================================================================
# D8 - an unreadable latch record is a THIRD outcome, not "absent"
# ===========================================================================


@pytest.mark.parametrize("corrupt", ["{not json", "[]", '"a string"', "17"])
def test_an_unreadable_latch_record_RAISES_instead_of_re_running(conn, corrupt):
    """D8: could-not-evaluate must never be reported as "the stage never ran".

    Treating a garbled blob as absent re-ran a once-only import AND made
    ``_merge_result_blob`` overwrite the blob, discarding every key the
    corrupt value held - including other stages' records.
    """
    with transaction(conn):
        run_first_run_import(
            conn, listing=TmuxListing.answered([])
        )
    assert sessions_stage_done(conn) is True

    with transaction(conn):
        set_meta(conn, META_IMPORTED_FROM_JSON_RESULT, corrupt)

    with pytest.raises(ImportLatchUnreadable):
        sessions_stage_done(conn)

    with pytest.raises(ImportLatchUnreadable):
        with transaction(conn):
            run_first_run_import(
                conn, listing=TmuxListing.answered([])
            )

    # nothing was written, and the corrupt value was NOT clobbered
    assert count_sessions(conn) == 0
    assert get_meta(conn, META_IMPORTED_FROM_JSON_RESULT) == corrupt


def test_a_GENUINELY_ABSENT_record_still_means_the_stage_never_ran(conn):
    """D8, the other half: absent and unreadable must stay distinguishable."""
    assert sessions_stage_done(conn) is False
    with transaction(conn):
        set_meta(conn, META_IMPORTED_FROM_JSON_RESULT, "")
    assert sessions_stage_done(conn) is False
    with transaction(conn):
        set_meta(conn, META_IMPORTED_FROM_JSON_RESULT, "{}")
    assert sessions_stage_done(conn) is False


def test_another_stages_keys_SURVIVE_a_sessions_import(conn):
    """D8: the blob is shared, so a merge must never be a replace."""
    with transaction(conn):
        set_meta(
            conn,
            META_IMPORTED_FROM_JSON_RESULT,
            json.dumps({"projects_imported_at": "2026-01-01T00:00:00Z"}),
        )
    with transaction(conn):
        run_first_run_import(
            conn, listing=TmuxListing.answered([])
        )
    blob = json.loads(get_meta(conn, META_IMPORTED_FROM_JSON_RESULT))
    assert blob["projects_imported_at"] == "2026-01-01T00:00:00Z"
    assert blob[RESULT_KEY_SESSIONS_STAGE]


# ===========================================================================
# schema: the v3 step that carries the discriminator
# ===========================================================================


def test_schema_v3_adds_the_column_ADDITIVELY(tmp_path):
    """The migration must add a column and change nothing else.

    The identity index is deliberately UNCHANGED: tmux's session id
    restarts at $0 when the server does, so it is a worse durable key
    than the creation epoch and is stored as a discriminator instead.
    Changing a unique index would also violate this schema's
    additive-only rule, which the whole rollback design depends on.
    """
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.schema_version == CURRENT_SCHEMA_VERSION >= 3
    with closing(connect(db_path_for(tmp_path))) as c:
        columns = {
            str(row[1]) for row in c.execute("PRAGMA table_info(sessions)")
        }
        assert "tmux_session_id" in columns
        index = c.execute(
            "SELECT sql FROM sqlite_master WHERE name = "
            "'ux_sessions_tmux_instance'"
        ).fetchone()[0]
        assert "tmux_socket, tmux_name, tmux_created_epoch" in index
        assert "tmux_session_id" not in index


def test_the_v3_step_is_IDEMPOTENT(tmp_path):
    """ALTER TABLE ADD COLUMN has no IF NOT EXISTS, so the step inspects first.

    Without the PRAGMA check a retry after an INTERRUPTED trail entry
    raises "duplicate column name" and the database can never advance.
    """
    from src.core.db_steps import _step_v2_to_v3

    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        with transaction(c):
            _step_v2_to_v3(c)
            _step_v2_to_v3(c)
        columns = [
            str(row[1]) for row in c.execute("PRAGMA table_info(sessions)")
        ]
        assert columns.count("tmux_session_id") == 1


def test_the_listing_carries_the_session_id_into_the_import(conn):
    """End to end: a parsed row's id reaches the stored row."""
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("a", 1000, session_id="$5")]),
    )
    assert list_sessions(conn)[0]["tmux_session_id"] == "$5"


def test_no_em_or_en_dashes_in_the_files_this_step_authored():
    """House style, checked by codepoint rather than by grep.

    grep here is a shell function that behaves like -I and silently skips
    files it deems binary, so an empty grep is not evidence of absence.
    """
    authored = [
        ROOT / "src" / "core" / "tmux_listing.py",
        ROOT / "src" / "core" / "tmux_listing_parse.py",
        ROOT / "src" / "core" / "session_identity.py",
        ROOT / "src" / "core" / "session_import.py",
        ROOT / "src" / "core" / "db_steps.py",
        Path(__file__),
        ROOT / "tests" / "test_tmux_listing_parse.py",
        ROOT / "tests" / "test_s4_regressions.py",
        ROOT / "src" / "core" / "session_reconcile.py",
    ]
    for path in authored:
        text = path.read_text(encoding="utf-8")
        assert text.count(chr(8212)) == 0, f"em-dash in {path.name}"
        assert text.count(chr(8211)) == 0, f"en-dash in {path.name}"
