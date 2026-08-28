"""ADVERSARIAL demonstrations against build step S4 (feat/sessions-table).

Every test in this file demonstrated a DEFECT. Each was written to assert
the CURRENT, WRONG behaviour and to name in its docstring what the
correct behaviour would be, "so that a fix flips the assertion rather
than deleting the test". This is that flip.

NOTHING HAS BEEN WEAKENED. Every scenario, every input and every measured
tmux string is unchanged. What changed is the assertion: each now states
the behaviour its own docstring already named as correct, and each
docstring records what the defect WAS so the history is not lost. Where
a demonstration could be made stronger without changing its subject it
was strengthened, and the strengthening is called out in the docstring.

Read the module as the list of ways the S4 guarantees COULD be defeated,
each now closed, with the proof sitting next to the description of the
hole it fills.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, get_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    META_IMPORTED_FROM_JSON_AT,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import (
    RECORD_REFUSED_INSTANCE_MISMATCH,
    adopt_instance,
    record_instance,
)
from src.core.session_import import (
    IMPORT_COMPLETED,
    IMPORT_PENDING_LISTING_UNAVAILABLE,
    ImportLatchUnreadable,
    run_first_run_import,
)
from src.core.session_store import count_sessions, list_sessions
from src.core.tmux_listing_parse import parse_listing_row, resolve_ownership
from src.core.tmux_listing import (
    TmuxListing,
    classify_listing_failure,
    looks_like_no_server,
)


@dataclass
class FakeProject:
    """Minimal stand-in for ProjectConfig.

    Inputs (constructor): name (str), path (str), agent_type (str).
    Output: a FakeProject instance.
    """

    name: str
    path: str
    description: Optional[str] = None
    agent_type: str = "claude"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _live(name, epoch, working_dir=None):
    """Build one attachable-listing row.

    Inputs: name (str), epoch (int), working_dir (str | None).
    Output: dict.
    """
    row = {"name": name, "created_at_epoch": epoch, "window_count": 1}
    if working_dir is not None:
        row["working_dir"] = working_dir
    return row


# ---------------------------------------------------------------------------
# DEFECT 1 - the latch IS stamped on a probe that could not answer.
#
# The AST test constrains src/core/session_import.py. It cannot constrain
# the CLASSIFIER that decides whether a failed tmux invocation is
# ok=False (no answer) or ok=True (a complete answer of zero).
#
# looks_like_no_server() matches the bare substring "error connecting to"
# and ignores the errno in the parentheses entirely. tmux emits that same
# prefix for Permission denied, Socket operation on non-socket, and File
# name too long - all measured against tmux 3.5a on this machine. Each of
# those is a probe that DID NOT ANSWER, and each is classified as ok=True
# with zero sessions, which walks straight through the gate and stamps
# the one-way latch.
# ---------------------------------------------------------------------------

REAL_TMUX_STDERR_THAT_IS_NOT_NO_SERVER = (
    "error connecting to /tmp/advt/noperm/sock2 (Permission denied)",
    "error connecting to /tmp/advt/noperm/sock (Socket operation on non-socket)",
    "error connecting to /very/long/path/sock (File name too long)",
)


@pytest.mark.parametrize("stderr_text", REAL_TMUX_STDERR_THAT_IS_NOT_NO_SERVER)
def test_defect_probe_failure_misclassified_as_zero_sessions(stderr_text):
    """A tmux connect ERROR is classified as a complete answer of zero.

    Description: WAS A DEFECT, now closed. These three stderr strings were
      produced by real tmux 3.5a invocations (re-measured byte-identical
      on 3.7b). None of them means "no server is running"; each means "I
      could not reach the socket", and each used to classify as ok=True
      with sessions=[] because the classifier matched the bare substring
      "error connecting to" and threw the errno away.

      The errno is now the whole decision, and only
      "(No such file or directory)" is an absent server. Everything else
      is ok=False.
    """
    assert looks_like_no_server(stderr_text) is False
    listing = classify_listing_failure(1, stderr_text)
    assert listing.ok is False
    assert listing.sessions == []
    assert listing.reason != "no_server"


def test_defect_latch_stamped_after_unreadable_socket(conn):
    """A permission-denied socket stamps the latch and loses history forever.

    Description: WAS A DEFECT, and it was the exact catastrophe
      session_import.py's docstring says cannot happen. The user has live
      tmux sessions; the probe cannot reach the socket; tmux says
      "error connecting to ... (Permission denied)"; the classifier used
      to call that a complete answer of zero; the gate passed; zero
      sessions were imported; imported_from_json_at was STAMPED; the
      import never ran again on that install.

      It is now a PENDING outcome with the latch unset, the reason named,
      and a home-screen notice - and the retry actually recovers the
      user's sessions, which is the half that proves the guard is not
      just a slower loss.
    """
    listing = classify_listing_failure(
        1, "error connecting to /tmp/x/sock (Permission denied)"
    )
    result = run_first_run_import(conn, listing=listing)
    assert result.outcome == IMPORT_PENDING_LISTING_UNAVAILABLE
    assert result.pending is True
    assert result.sessions_imported == 0
    assert count_sessions(conn) == 0
    assert get_meta(conn, META_IMPORTED_FROM_JSON_AT) is None
    notice = result.home_screen_notice()
    assert notice is not None and "PENDING" in notice

    # And it is NOT permanent: the next start recovers his sessions.
    second = run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_work", 1755000000)]),
        owned_tmux_names={"cloude_work"},
    )
    assert second.outcome == IMPORT_COMPLETED
    assert count_sessions(conn) == 1
    assert list_sessions(conn)[0]["tmux_name"] == "cloude_work"


# ---------------------------------------------------------------------------
# DEFECT 2 - the instance triple is FORGEABLE from a tmux session name.
#
# TmuxBackend.list_attachable_sessions asks tmux for
# "#{session_name}|#{session_created}|#{session_windows}" and splits the
# line on "|". tmux forbids only "." and ":" in a session name - "|" is
# legal, verified on tmux 3.5a. _sanitize_tmux_name replaces only "." and
# ":" too, so the app itself will happily mint a name containing pipes
# from a project called "a|b".
# ---------------------------------------------------------------------------


def _parse_listing_line(line, owned_instances=None, owned_names=None):
    """Run one tmux line through the REAL parser and the REAL badge resolver.

    Description: STRENGTHENED. This used to reproduce the backend's inline
      parser verbatim, because the parser lived inside a method that
      shells out to tmux and could not be called directly. Reproducing it
      meant the demonstration could drift away from the code it accused.
      The parser and the ownership resolver now live in
      src/core/tmux_listing_parse.py precisely so they can be called
      here, so this delegates to them instead of imitating them. Nothing
      about the scenarios below changed; they now run against the
      shipping code rather than a copy of it.
    Inputs: line (str) - one line of tmux -F output. owned_instances
      (set | None). owned_names (set | None) - the legacy in-memory set.
    Output: dict | None - the row the backend would emit, or None when
      the parser REFUSED the line.
    """
    row = parse_listing_row(line)
    if row is None:
        return None
    return {
        "name": row["name"],
        "created_at_epoch": row["created_at_epoch"],
        "owned": resolve_ownership(
            row["name"], row["created_at_epoch"], owned_instances, owned_names
        ),
    }


def test_defect_pipe_in_tmux_name_forges_the_instance_triple():
    """A session named "victim|1755000000|1" impersonates an owned instance.

    Description: WAS A DEFECT. Measured tmux output for a session created
      as `tmux new-session -s 'victim|1755000000|1'` is
      `victim|1755000000|1|1787070480|1`. The old parser took fields
      0, 1, 2, so the app believed it was looking at a session named
      "victim" created at epoch 1755000000 - an instance triple the
      ATTACKER chose. If the DB held an owned row for that triple, the
      unrelated process badged as OURS.

      Neither remedy the docstring offered was taken, because a third is
      stronger than both: the format now puts the caller-controlled NAME
      LAST and splits with a BOUNDED split, so the name cannot reach the
      parser as more than one field at all. The forgery is not detected,
      it is UNREPRESENTABLE - this line no longer parses, because its
      first field is not a tmux-generated `$<digits>` session id.

      Rejecting the delimiter outright was rejected as the primary fix
      because a project legitimately called "api|prod" must still work;
      see test_a_pipe_in_a_session_name_survives_VERBATIM.
    """
    real_tmux_line = "victim|1755000000|1|1787070480|1"
    owned_from_db = {("victim", 1755000000)}
    row = _parse_listing_line(real_tmux_line, owned_instances=owned_from_db)
    assert row is None, "the forged line still parses"

    # The same session, seen through the REAL format, reports its REAL
    # identity: the name keeps its pipes and the epoch is tmux's own.
    honest = _parse_listing_line(
        "$7|1787070480|1|victim|1755000000|1", owned_instances=owned_from_db
    )
    assert honest["name"] == "victim|1755000000|1"
    assert honest["created_at_epoch"] == 1787070480
    assert honest["owned"] is False


def test_defect_sanitize_tmux_name_permits_the_delimiter():
    """The app mints its own pipe-containing names, no attacker required.

    Description: WAS A DEFECT. _sanitize_tmux_name documented that tmux
      forbids only "." and ":" and preserved everything else verbatim, so
      a project named "api|prod" yielded a tmux name the app's own
      listing parser could not read back. No attacker required.

      The bounded split now reads such a name back correctly, so this is
      defence in depth rather than the primary guard - but a name the app
      MINTED ITSELF should never depend on a parser subtlety to be read,
      so the delimiter and the non-whitespace control characters are
      replaced. The whitespace controls are deliberately left to the
      pre-existing collapse rule.
    """
    from src.core.session_manager import _sanitize_tmux_name

    assert _sanitize_tmux_name("api|prod") == "api_prod"
    assert "|" not in _sanitize_tmux_name("a|b|c")


# ---------------------------------------------------------------------------
# DEFECT 3 - the legacy owned-name set is UNIONed in with a wildcard
# epoch, which defeats the epoch tier for exactly the sessions the epoch
# tier was added to protect.
# ---------------------------------------------------------------------------


def test_defect_legacy_wildcard_epoch_defeats_the_instance_key():
    """A reused tmux name still badges as ours, exactly as before S4.

    Description: WAS A DEFECT. SessionManager.owned_tmux_instances()
      returned `db_instances | {(name, None) for name in
      owned_tmux_sessions}`, and TmuxBackend matched `(name, None)` as a
      name-only WILDCARD. So for every name still in the legacy in-memory
      set - which is every session this app created since the last
      restart - the epoch was ignored and a NEW, unrelated tmux session
      that took the name badged as OURS. That was verbatim the failure
      the commit message said the epoch "closes entirely".

      The wildcard is gone in both halves: owned_tmux_instances() no
      longer fabricates a None epoch, and the resolver treats a None
      epoch as inert rather than as a match-anything. The legacy names
      still reach the resolver, but through the SEPARATE owned_names
      argument at their own explicitly name-only tier, where a stored
      epoch for the same name overrides them.
    """
    owned_from_db = {("cloude_work", 1755000000)}
    legacy_names = {"cloude_work"}

    # The old session died. A brand new, unrelated session took the name.
    # The legacy set STILL holds the bare name, which is the whole point.
    stranger = _parse_listing_line(
        "$9|1787070999|1|cloude_work",
        owned_instances=owned_from_db,
        owned_names=legacy_names,
    )
    assert stranger["created_at_epoch"] == 1787070999  # a different instance
    assert stranger["owned"] is False

    # A None epoch is inert even if one is somehow constructed.
    wildcard = _parse_listing_line(
        "$9|1787070999|1|cloude_work",
        owned_instances={("cloude_work", None)},
    )
    assert wildcard["owned"] is False

    # And the genuine instance still badges as ours.
    real = _parse_listing_line(
        "$2|1755000000|1|cloude_work", owned_instances=owned_from_db
    )
    assert real["owned"] is True


# ---------------------------------------------------------------------------
# DEFECT 4 - the same-second collision guard only fires on a STOPPED row,
# which is the rarer half of the case. The common half MERGES.
# ---------------------------------------------------------------------------


def test_defect_adoption_transfers_to_a_stranger_on_a_running_row(conn):
    """An adopted session's identity is no longer handed to another process.

    Description: WAS A DEFECT. record_instance REFUSED only when the
      stored row was already `stopped`. A row is marked stopped by a
      successful probe, and probes are periodic - so in the window
      between a session's death and the next probe, the stored row is
      still `running`. A new session that took the same name inside the
      same one-second epoch therefore MERGED into the adopted row,
      inheriting its session_uuid, its origin='adopted' and its
      adopted_at.

      THE FIX TOOK THE SECOND REMEDY THIS DOCSTRING NAMED: widen the
      identity beyond a one-second epoch using tmux's #{session_id}.
      It is carried as a DISCRIMINATOR on the row (schema v3), not as the
      stored key, because the id counter restarts at $0 when the tmux
      server does - so it is unique within a server lifetime (better than
      the epoch at separating two live sessions) but repeats across
      restarts (worse than the epoch as a durable key). It can therefore
      only ever cause a REFUSAL, never a match.

      STRENGTHENED: the scenario is unchanged but both sightings now
      carry the session id the real listing path always supplies, because
      parse_listing_row REFUSES any row without a valid `$<digits>` id.
      The no-evidence case is covered separately below.
    """
    with transaction(conn):
        record_instance(
            conn,
            socket="cloude",
            name="cloude_work",
            epoch=1755000000,
            origin=SESSION_ORIGIN_CREATED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            session_id="$3",
        )
        adopt_instance(
            conn, socket="cloude", name="cloude_work", epoch=1755000000,
            now="2026-08-18T00:00:00Z",
        )
    before = list_sessions(conn)[0]
    assert before["origin"] == SESSION_ORIGIN_ADOPTED

    # The session dies. NO probe has run yet, so the row is still running.
    # A completely unrelated process takes the name inside the same second.
    with transaction(conn):
        result = record_instance(
            conn,
            socket="cloude",
            name="cloude_work",
            epoch=1755000000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            session_id="$9",
        )
    assert result.outcome == RECORD_REFUSED_INSTANCE_MISMATCH
    assert result.refused is True
    after = list_sessions(conn)[0]
    assert after["session_uuid"] == before["session_uuid"]
    assert after["origin"] == SESSION_ORIGIN_ADOPTED
    assert after["adopted_at"] == "2026-08-18T00:00:00Z"


def test_the_live_listing_path_ALWAYS_supplies_the_discriminator():
    """The refusal above depends on evidence, so the evidence must be mandatory.

    Description: the honest limit of the D4 fix, asserted rather than
      left implicit. The mismatch refusal fires only when BOTH sides
      carry a session id, because a NULL id means "not recorded" and
      must never manufacture a refusal on an upgraded install. That would
      be a hole if the production listing could ever yield a row without
      one - so it cannot: parse_listing_row REFUSES any line whose first
      field is not a tmux-generated `$<digits>` id. Every row reaching
      record_instance from a live probe therefore carries one.
    """
    assert parse_listing_row("$0|1|1|a")["session_id"] == "$0"
    # no id, or a non-tmux id, and the row does not exist at all
    assert parse_listing_row("1|1|a") is None
    assert parse_listing_row("x|1|1|a") is None
    assert parse_listing_row("|1755000000|1|a") is None


def test_defect_adopt_instance_can_claim_a_dead_session(conn):
    """adopt_instance has no lifecycle guard, so a stale UI adopts a corpse.

    Description: WAS A DEFECT (lower severity). The UPDATE keyed on the
      triple alone, so a client holding a listing from before the session
      died could POST /sessions/adopt and permanently mark a stopped row
      as adopted, receiving True for a process that no longer exists.

      The first remedy was taken: the UPDATE now requires the row NOT be
      `stopped`. The second was taken as well - a refused claim logs the
      stored lifecycle by name, so the two ways of returning False stay
      distinguishable.
    """
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="dead", epoch=1755000000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_STOPPED,
        )
        claimed = adopt_instance(
            conn, socket="cloude", name="dead", epoch=1755000000
        )
    assert claimed is False
    row = list_sessions(conn)[0]
    assert row["origin"] == SESSION_ORIGIN_OBSERVED
    assert row["adopted_at"] is None


# ---------------------------------------------------------------------------
# DEFECT 5 - the import badges the user's own persisted session EXTERNAL.
# ---------------------------------------------------------------------------


def test_defect_persisted_owned_session_imports_as_observed(conn):
    """The one session RECENT is built from imports as OURS, not external.

    Description: WAS A DEFECT. session_import step 5 hardcoded
      `origin=SESSION_ORIGIN_OBSERVED` for a persisted session with no
      live tmux row, ignoring owned_tmux_names entirely - unlike step 4,
      which consults it. session_metadata.json holds exactly ONE session:
      the most recently active one, which for this user is almost always
      one the app created. So on the very upgrade this import exists to
      protect, his last session appeared in RECENT badged EXTERNAL.

      Step 5 now calls observed_origin_for(name, owned), the same
      resolver step 4 uses, and a structural test in
      tests/test_s4_regressions.py asserts NEITHER step can go back to a
      hardcoded origin.
    """
    result = run_first_run_import(
        conn,
        listing=TmuxListing.answered([]),
        owned_tmux_names={"cloude_mine"},
        persisted_sessions=[
            {"tmux_session": "cloude_mine", "id": "sess-1",
             "agent_type": "claude", "tmux_created_epoch": 1755000000},
        ],
    )
    assert result.outcome == IMPORT_COMPLETED
    row = list_sessions(conn)[0]
    assert row["tmux_name"] == "cloude_mine"
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert row["origin"] == SESSION_ORIGIN_CREATED


# ---------------------------------------------------------------------------
# DEFECT 6 - main.py never tells the import which socket it probed.
# ---------------------------------------------------------------------------


def test_defect_import_hardcodes_the_default_socket(conn):
    """Imported rows record the socket the probe ACTUALLY ran against.

    Description: WAS A DEFECT. src/main.py called run_first_run_import
      without `socket=`, so every imported row took DEFAULT_TMUX_SOCKET.
      A user with `session.tmux_socket_name` set to anything else got
      rows asserting an instance that does not exist on that socket,
      while SessionManager._tmux_socket_name() read the CONFIGURED value
      back - so owned_instances() queried socket='mysock', found nothing,
      and the ownership badge silently fell back to the legacy name set
      for that whole install.

      main.py now passes session_manager.tmux_socket_name(), a public
      accessor added for the purpose, so the write and the read cannot
      disagree. The adversary's own resolution of the build dispute
      stands: build_backend DOES thread socket_name correctly
      (src/core/session_backend.py), the create path was never broken,
      and design doc section 1.6 was the stale claim.
    """
    run_first_run_import(
        conn,
        listing=TmuxListing.answered([_live("cloude_a", 1755000000)]),
        owned_tmux_names={"cloude_a"},
        socket="mysock",
    )
    import inspect

    import src.main as main_mod
    from src.core.session_store import owned_instances

    # main.py passes the socket the manager reads back, not a default.
    main_src = inspect.getsource(main_mod)
    call = main_src[main_src.index("run_first_run_import("):]
    call = call[: call.index("\n                    )")]
    assert "socket=" in call
    assert "tmux_socket_name()" in call

    # The row records the socket it was GIVEN, and that socket is the one
    # a matching owned_instances() query finds.
    assert list_sessions(conn)[0]["tmux_socket"] == "mysock"
    assert owned_instances(conn, socket="mysock") == {("cloude_a", 1755000000)}
    assert owned_instances(conn, socket="some-other-socket") == set()


# ---------------------------------------------------------------------------
# DEFECT 7 - an unparseable result blob silently un-does the once-only
# guard, and clobbers the other stages' records.
# ---------------------------------------------------------------------------


def test_defect_corrupt_result_blob_reruns_the_import(conn):
    """A garbled meta blob is CANNOT DETERMINE, and refuses to proceed.

    Description: WAS A DEFECT (contained, but it was a third outcome
      collapsed into a pass). sessions_stage_done() read
      meta.imported_from_json_result and treated an unparseable value as
      ABSENT - i.e. as proof the stage had not run - which is exactly the
      "could not evaluate reported as a verdict" the repo rule forbids.
      The re-run did not double rows (the unique index and the refusal
      hold), but _merge_result_blob then wrote a fresh blob and DISCARDED
      every key the corrupt one held.

      An unreadable latch record now RAISES ImportLatchUnreadable and
      says so, per the docstring's own prescription. The corrupt value is
      left on disk untouched rather than clobbered, so an operator can
      inspect it and clear it deliberately.
    """
    from src.core.db import set_meta
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT
    from src.core.session_import import sessions_stage_done

    with transaction(conn):
        run_first_run_import(
            conn, listing=TmuxListing.answered([]),
        )
    assert sessions_stage_done(conn) is True

    with transaction(conn):
        set_meta(conn, META_IMPORTED_FROM_JSON_RESULT, "{not json")

    with pytest.raises(ImportLatchUnreadable):
        sessions_stage_done(conn)

    with pytest.raises(ImportLatchUnreadable):
        with transaction(conn):
            run_first_run_import(
                conn, listing=TmuxListing.answered([]),
            )

    # the unreadable value is preserved, not overwritten
    assert get_meta(conn, META_IMPORTED_FROM_JSON_RESULT) == "{not json"


# ---------------------------------------------------------------------------
# DEFECT 8 - the AST proof guards the DECORATIVE latch, not the OPERATIVE one.
#
# The once-only guard that actually runs is sessions_stage_done(), which
# reads RESULT_KEY_SESSIONS_STAGE out of meta.imported_from_json_result
# and is written by _merge_result_blob(). META_IMPORTED_FROM_JSON_AT - the
# only thing the AST test constrains - is never read by the guard at all.
# So the mutation the AST test exists to catch is catchable in the wrong
# variable, and its twin walks straight past.
# ---------------------------------------------------------------------------


def test_defect_ast_proof_misses_the_guard_that_actually_latches(tmp_path):
    """The AST proof now constrains EVERY write that can latch the import.

    Description: WAS A DEFECT. The once-only guard that actually runs is
      sessions_stage_done(), which reads RESULT_KEY_SESSIONS_STAGE out of
      meta.imported_from_json_result and is written by
      _merge_result_blob(). META_IMPORTED_FROM_JSON_AT - the only thing
      the old AST test constrained - is never read by that guard at all.
      So hoisting the _merge_result_blob stamp above the `if not
      listing.ok` gate latched the import shut permanently while
      GET /sessions/import-status reported it had never completed, and
      BOTH structural assertions still passed on the mutant.

      Both remedies the docstring named are now in place. Both writes
      live in ONE helper, _latch_sessions_stage, so the operative guard
      and the asserted latch are written together or not at all; and the
      structural proof constrains every write of EITHER key, asserts the
      helper has exactly one call site, and asserts that call site is
      after the gate.

      This test keeps the ORIGINAL mutation and asserts it is now
      REJECTED, plus the original behavioural half, now asserting the two
      keys cannot disagree.
    """
    import ast

    src = (ROOT / "src" / "core" / "session_import.py").read_text()

    def _structural_ok(source: str) -> bool:
        """Evaluate the CURRENT structural proof's core fact on a source.

        Description: fact 1 of the rewritten proof in
          tests/test_session_import.py - every write of either latch key
          must sit inside _latch_sessions_stage. Reproduced here so this
          demonstration can be run against a mutant.
        Inputs: source (str) - python source text.
        Output: bool - True when no latch write escapes the helper.
        """
        tree = ast.parse(source)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name == "_latch_sessions_stage":
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name == "_merge_result_blob":
                    for sub in ast.walk(node):
                        if getattr(sub, "id", None) == "RESULT_KEY_SESSIONS_STAGE":
                            return False
                if name == "set_meta":
                    for arg in node.args:
                        if getattr(arg, "id", None) == "META_IMPORTED_FROM_JSON_AT":
                            return False
        return True

    assert _structural_ok(src) is True  # the real module passes

    # THE MUTATION: stamp the operative guard key BEFORE the gate.
    hoist = (
        "    # --- step 3: THE GATE ---"
        "-----------------------------------------\n"
        "    _merge_result_blob(conn, {RESULT_KEY_SESSIONS_STAGE: stamp})\n"
    )
    marker = "    # --- step 3: THE GATE ------------------------------------------------\n"
    assert marker in src
    mutant = src.replace(marker, hoist, 1)

    assert _structural_ok(mutant) is False, (
        "the hoisted-blob mutation is STILL invisible to the structural "
        "proof; this is the mutation that latched the import shut "
        "permanently while every reader said it had never completed"
    )

    # BEHAVIOURAL HALF: the two keys can no longer disagree. Writing only
    # the guard key by hand still latches - meta is a key/value store and
    # nothing can stop a direct write - but the module itself has no path
    # that produces that state, and a real completed import sets BOTH.
    import json

    from src.core.db import set_meta
    from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT
    from src.core.session_import import (
        RESULT_KEY_SESSIONS_STAGE,
        sessions_stage_done,
    )

    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as c:
        with transaction(c):
            blocked = run_first_run_import(
                c,
                listing=TmuxListing.answered([_live("cloude_work", 1755000000)]),
                owned_tmux_names={"cloude_work"},
            )
        assert blocked.outcome == IMPORT_COMPLETED
        assert count_sessions(c) == 1

        # BOTH keys were written by the one helper, so the guard and the
        # route agree. Previously only one of them was constrained.
        assert sessions_stage_done(c) is True
        assert get_meta(c, META_IMPORTED_FROM_JSON_AT) is not None
        blob = json.loads(get_meta(c, META_IMPORTED_FROM_JSON_RESULT))
        assert blob[RESULT_KEY_SESSIONS_STAGE] == get_meta(
            c, META_IMPORTED_FROM_JSON_AT
        ), "the operative latch key and the reported one disagree"
