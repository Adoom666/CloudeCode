"""Regressions for the imported-session identity fields (findings V2, V3
and V8 of the second adversarial round).

THE COMMON SUBJECT is the first-run import's STEP 5 - persisted sessions
from session_metadata.json that have no live tmux row. It is not an edge
case: the live install this import exists to protect carries exactly such
a file, so this path runs on the user's real machine on first upgraded
start. Both defects were therefore production behaviour, not hypotheses.

  V2  The step-5 ``record_instance`` call did not pass ``session_id`` at
      all, so every row it wrote carried a NULL discriminator whether or
      not the entry knew one. A NULL can never cause a refusal, so the
      instance-mismatch guard was silently unarmed for those rows.
  V3  ``_stopped_epoch`` returned a synthesized ``0`` when an entry
      recorded no epoch - which is the normal case, since the persisted
      format has no epoch field. With ``origin='created'`` that row
      entered ``owned_instances``, and the resolver's tier 2 then issued
      a CONFIDENT NEGATIVE against the name forever.
  V8  Nothing anywhere deletes a sessions row, so a wrong owned row is a
      permanent negative opinion with no repair path.

The V3 assertions are deliberately made at TWO layers: the mapping that
chooses the value, and ``owned_instances``, which is the layer that
actually decides whether a row can become an ownership opinion. Asserting
only the mapping would leave the guarantee resting on a caller.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.session_identity import RECORD_INSERTED, record_instance
from src.core.session_import import run_first_run_import
from src.core.session_import_mapping import _persisted_session_id, _stopped_epoch
from src.core.session_store import get_instance, owned_instances
from src.core.tmux_listing import TmuxListing
from src.core.tmux_listing_parse import resolve_ownership

SOCKET = "cloude"
REAL_EPOCH = 1755000000
LATER_EPOCH = 1787000000


@pytest.fixture()
def migrated_conn(tmp_path: Path):
    """A connection to a fresh state dir migrated to the current schema.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection - closed on teardown.
    """
    ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0-test")
    conn = connect(db_path_for(tmp_path))
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# V3  a synthesized epoch must not enter the identity key
# ---------------------------------------------------------------------------


def test_stopped_epoch_is_none_when_the_entry_records_none() -> None:
    """The mapping layer: absent means None, never a manufactured 0.

    The persisted session_metadata.json on the live install has no epoch
    field at all, so this IS the production path.
    """
    assert _stopped_epoch({}) is None
    assert _stopped_epoch({"session_id": "abc"}) is None
    assert _stopped_epoch({"tmux_created_epoch": None}) is None
    assert _stopped_epoch({"tmux_created_epoch": "not-an-int"}) is None


def test_stopped_epoch_still_uses_a_recorded_epoch() -> None:
    """A real recorded epoch must still be honoured, in both key spellings."""
    assert _stopped_epoch({"tmux_created_epoch": REAL_EPOCH}) == REAL_EPOCH
    assert _stopped_epoch({"created_at_epoch": REAL_EPOCH}) == REAL_EPOCH
    assert _stopped_epoch({"tmux_created_epoch": "1755000000"}) == REAL_EPOCH


def test_null_epoch_row_never_becomes_an_ownership_opinion(migrated_conn) -> None:
    """THE LAYER THAT ENFORCES IT. A NULL-epoch row is not an opinion.

    Written with ``origin='created'`` - the origin that DOES qualify for
    ownership - so this proves the exclusion comes from the NULL epoch
    and not from the origin filter.
    """
    with migrated_conn:
        result = record_instance(
            migrated_conn,
            socket=SOCKET,
            name="cloude_work",
            epoch=None,
            origin="created",
            lifecycle="stopped",
        )
    assert result.outcome == RECORD_INSERTED

    stored = migrated_conn.execute(
        "SELECT tmux_created_epoch FROM sessions WHERE tmux_name = ?",
        ("cloude_work",),
    ).fetchone()
    assert stored[0] is None, "the epoch must be stored as SQL NULL"

    assert owned_instances(migrated_conn, socket=SOCKET) == set(), (
        "a row with no recorded epoch must not reach the ownership "
        "resolver at all"
    )


def test_null_epoch_row_does_not_disown_a_later_real_session(
    migrated_conn,
) -> None:
    """THE OUTCOME THE USER WOULD HAVE SEEN. No confident negative.

    Under the old synthesized 0, this row landed in owned_instances as
    ``('cloude_work', 0)``, tier 2 fired on the name, and the user's real
    later session badged owned=False permanently.
    """
    with migrated_conn:
        record_instance(
            migrated_conn,
            socket=SOCKET,
            name="cloude_work",
            epoch=None,
            origin="created",
            lifecycle="stopped",
        )

    owned = owned_instances(migrated_conn, socket=SOCKET)
    verdict = resolve_ownership(
        "cloude_work", LATER_EPOCH, owned, {"cloude_work"}, prefix="cloude_"
    )
    assert verdict is True, (
        "with no epoch-keyed opinion stored, the legacy name tier must be "
        "reachable; a fabricated epoch used to pre-empt it with a False"
    )


def test_a_real_stored_epoch_still_produces_the_negative(migrated_conn) -> None:
    """Tier 2 must keep working where there IS evidence.

    The fix must narrow the negative to rows that earned it, not remove
    it - otherwise V3's cure reopens the wildcard hole tier 2 closed.
    """
    with migrated_conn:
        record_instance(
            migrated_conn,
            socket=SOCKET,
            name="cloude_work",
            epoch=REAL_EPOCH,
            origin="created",
            lifecycle="stopped",
        )

    owned = owned_instances(migrated_conn, socket=SOCKET)
    assert owned == {("cloude_work", REAL_EPOCH)}
    assert resolve_ownership(
        "cloude_work", LATER_EPOCH, owned, {"cloude_work"}, prefix="cloude_"
    ) is False


def test_get_instance_with_a_none_epoch_matches_nothing(migrated_conn) -> None:
    """An unknown epoch identifies no instance, so it must never MERGE."""
    with migrated_conn:
        record_instance(
            migrated_conn,
            socket=SOCKET,
            name="cloude_work",
            epoch=REAL_EPOCH,
            origin="created",
        )
    assert get_instance(
        migrated_conn, socket=SOCKET, name="cloude_work", epoch=None
    ) is None


def test_two_null_epoch_rows_do_not_collide(migrated_conn) -> None:
    """The documented cost of the fix, asserted so it is not a surprise.

    The partial unique index excludes NULL epochs, so two entries for the
    same name with no epoch produce two rows. Duplicate history is the
    accepted trade against a fabricated identity.
    """
    with migrated_conn:
        first = record_instance(
            migrated_conn, socket=SOCKET, name="cloude_work", epoch=None,
            origin="observed", lifecycle="stopped",
        )
        second = record_instance(
            migrated_conn, socket=SOCKET, name="cloude_work", epoch=None,
            origin="observed", lifecycle="stopped",
        )
    assert first.outcome == RECORD_INSERTED
    assert second.outcome == RECORD_INSERTED
    assert first.session_uuid != second.session_uuid


# ---------------------------------------------------------------------------
# V2  the discriminator is passed when known, and NULL only when measured
# ---------------------------------------------------------------------------


def test_persisted_session_id_reads_only_the_tmux_namespace() -> None:
    """The app's own session id must NEVER be written as tmux's.

    session_metadata.json's ``session_id`` is the APP's identifier and is
    mapped to ``legacy_session_id``. Writing it into tmux_session_id
    would manufacture a discriminator mismatch against every real row.
    """
    assert _persisted_session_id({"tmux_session_id": "$3"}) == "$3"
    assert _persisted_session_id({"session_id": "abc"}) is None
    assert _persisted_session_id({"id": "abc"}) is None
    assert _persisted_session_id({}) is None
    assert _persisted_session_id({"tmux_session_id": "   "}) is None


def test_step_five_records_a_known_discriminator(migrated_conn) -> None:
    """V2's fix: where the entry knows a tmux id, the row must carry it."""
    listing = TmuxListing.answered([], reason=None, detail="no live sessions")
    with migrated_conn:
        migrated_conn.execute("BEGIN IMMEDIATE")
        run_first_run_import(
            migrated_conn,
            listing=listing,
            owned_tmux_names={"cloude_known"},
            persisted_sessions=[
                {
                    "tmux_session": "cloude_known",
                    "session_id": "app-side-id",
                    "tmux_session_id": "$12",
                    "tmux_created_epoch": REAL_EPOCH,
                },
            ],
            socket=SOCKET,
        )
        migrated_conn.execute("COMMIT")

    row = migrated_conn.execute(
        "SELECT tmux_session_id, legacy_session_id, tmux_created_epoch "
        "FROM sessions WHERE tmux_name = ?",
        ("cloude_known",),
    ).fetchone()
    assert row[0] == "$12", "the tmux discriminator must be persisted"
    assert row[1] == "app-side-id", "the app id belongs in legacy_session_id"
    assert row[2] == REAL_EPOCH


def test_step_five_null_discriminator_is_measured_not_forgotten(
    migrated_conn,
) -> None:
    """The honest NULL: the live install records no tmux id, so NULL it is.

    This asserts the shape of the user's REAL session_metadata.json - a
    record with an app ``id`` and no tmux id and no epoch - and pins both
    honest NULLs together.
    """
    listing = TmuxListing.answered([], reason=None, detail="no live sessions")
    with migrated_conn:
        migrated_conn.execute("BEGIN IMMEDIATE")
        run_first_run_import(
            migrated_conn,
            listing=listing,
            owned_tmux_names={"cloude_work"},
            persisted_sessions=[
                {"tmux_session": "cloude_work", "session_id": "abc"},
            ],
            socket=SOCKET,
        )
        migrated_conn.execute("COMMIT")

    row = migrated_conn.execute(
        "SELECT tmux_session_id, tmux_created_epoch, legacy_session_id "
        "FROM sessions WHERE tmux_name = ?",
        ("cloude_work",),
    ).fetchone()
    assert row[0] is None, "no tmux id was recorded, so NULL is the truth"
    assert row[1] is None, "no epoch was recorded, so NULL - never a 0"
    assert row[2] == "abc"

    assert owned_instances(migrated_conn, socket=SOCKET) == set(), (
        "the row must not become an epoch-keyed ownership opinion"
    )


def test_step_five_passes_session_id_structurally() -> None:
    """AST proof at the CALL SITE, which is the layer that forgets.

    A behavioural test only catches the entry shapes someone thought to
    write. The defect was a missing keyword argument, so the proof is
    that the keyword is present on that specific call.
    """
    source = (ROOT / "src" / "core" / "session_import.py").read_text()
    tree = ast.parse(source)

    calls_without_session_id: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "record_instance":
            continue
        if not any(kw.arg == "session_id" for kw in node.keywords):
            calls_without_session_id.append(node.lineno)

    assert calls_without_session_id == [], (
        "every record_instance call in the import must pass session_id "
        "explicitly, so a NULL discriminator is a measured absence rather "
        f"than a forgotten argument. Missing at lines: "
        f"{calls_without_session_id}"
    )


# ---------------------------------------------------------------------------
# V8  there is no repair path, and that must stay visible
# ---------------------------------------------------------------------------


def test_no_source_file_deletes_a_sessions_row() -> None:
    """Pins the KNOWN GAP so the docstring describing it cannot go stale.

    Verified by parsing, not by grep, for two independent reasons. The
    shell's grep here behaves like ``-I`` and exits 1 with no output on
    files it deems binary, so an empty grep is not evidence of absence.
    And a text scan cannot tell EXECUTABLE SQL from PROSE ABOUT SQL - the
    session_store docstring that documents this very gap contains the
    phrase, and a naive scan reports the documentation as the defect.

    So the scan walks the AST and inspects only string literals that are
    NOT docstrings, which is where real SQL lives.

    This test is expected to FAIL THE DAY A REAPER IS BUILT. That is the
    point - whoever builds it must come here, read the intended design
    recorded in session_store's module docstring (reap only against a
    listing with ok=True), and update both together.

    ONE EXEMPTION, AND IT IS DELIBERATELY NARROW. Schema v9 merges the
    row pairs the tmux-rename defect produced, and a merge ends by
    removing the discarded corpse. That is a ONE-SHOT, VERSIONED,
    inside-a-transaction repair with no runtime path to it - a
    completely different risk from a live reaper deciding a session is
    gone. The exemption is a single FILE, checked further by
    ``test_the_only_delete_lives_in_the_v9_merge`` below, which pins it
    to a single FUNCTION. Widening it to a second file or a second
    function fails one of the two tests, which is the property that
    stops "one migration needed it" becoming a general licence.
    """
    #: The only source file permitted to carry a sessions DELETE. See
    #: this function's docstring for why, and the test below for the
    #: second half of the guard.
    migration_steps = "core/db_steps.py"

    offenders: List[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if str(path.relative_to(ROOT / "src")) == migration_steps:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))

        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_nodes.add(id(body[0].value))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str) or id(node) in docstring_nodes:
                continue
            if "delete from sessions" in node.value.lower():
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                )

    assert offenders == [], (
        "a sessions reaper now exists. Update the KNOWN GAP section of "
        "src/core/session_store.py's module docstring, and make sure the "
        "reaper is gated on TmuxListing.ok being True - reaping against "
        f"ok=False would delete the user's whole history. Found in: "
        f"{offenders}"
    )


def test_session_store_documents_the_missing_repair_path() -> None:
    """The gap must be NAMED in the module a maintainer actually opens."""
    import src.core.session_store as session_store

    doc = session_store.__doc__ or ""
    assert "DELETE" in doc.upper()
    assert "reaper" in doc.lower(), (
        "the intended repair path must be described, not merely the gap"
    )
    assert "ok=True" in doc, (
        "the load-bearing constraint on any future reaper must be stated"
    )


def test_the_only_delete_lives_in_the_v9_merge() -> None:
    """The second half of the exemption above: pin it to ONE function.

    ``test_no_source_file_deletes_a_sessions_row`` exempts one FILE.
    Left there, anything later added to that file could delete sessions
    rows unnoticed. This walks the same AST and asserts every sessions
    DELETE in it sits inside ``_v9_merge_rename_splits``, so the
    exemption cannot spread by accident.
    """
    path = ROOT / "src" / "core" / "db_steps.py"
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))

    allowed = "_v9_merge_rename_splits"
    stray: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == allowed:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and "delete from sessions" in inner.value.lower()
            ):
                stray.append(f"{node.name}:{inner.lineno}")

    assert stray == [], (
        "a sessions DELETE appeared outside the v9 merge. The file-level "
        "exemption in test_no_source_file_deletes_a_sessions_row covers "
        f"exactly one function, not the whole file. Found in: {stray}"
    )
