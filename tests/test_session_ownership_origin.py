"""The created_by_cloude cutover: sessions.origin is now the source of truth.

WHAT CHANGED. Ownership used to be membership of the in-memory
``owned_tmux_sessions`` set, rebuilt from a live listing on every start,
which is why an adoption could not survive a restart. It is now
``sessions.origin``, a stored column anchored on the tmux INSTANCE triple
``(socket, name, creation epoch)``.

WHY THE EPOCH IS IN EVERY ASSERTION HERE. The badge decides whether the
user is told a session is HIS. A name-keyed lookup gets that wrong the
moment a name is reused, and a session the user never touched shows up
badged as his. So the tests below always check the reused-name case, not
just the happy one.

WHAT DID NOT CHANGE, DELIBERATELY. ``owned_tmux_sessions`` is still
maintained and still consulted, and the adopt ROUTE still does not write
``origin='adopted'`` - persisting adoption is build step S7. Cutting that
over here would flip the badge for adopted sessions and break
scripts/verify_session_ownership_badge.py, which must pass unchanged
across this commit.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_backend import TmuxBackend

SESSION_MANAGER_PATH = ROOT / "src" / "core" / "session_manager.py"
TMUX_BACKEND_PATH = ROOT / "src" / "core" / "tmux_backend.py"


class _FakeProbeBackend:
    """A TmuxBackend whose listing rows are supplied, not shelled out for.

    Description: exercises the real resolution logic in
      ``list_attachable_sessions`` without running tmux, so the test can
      state an exact (name, epoch) pair. Never touches a socket.
    Inputs (constructor): rows (list[tuple[str, int]]) - name and epoch.
    Output: an object with ``list_attachable_sessions``.
    """

    def __init__(self, rows):
        self._rows = rows

    def _run_listing(self, *_args):
        """Return the canned tmux stdout for the supplied rows.

        Description: emits LISTING_FORMAT's field ORDER - session id,
          created, windows, then the NAME LAST - because the name is the
          only caller-controlled field and putting it last is what makes
          the bounded split unambiguous. A synthetic ``$<n>`` id is
          assigned per row so the rows validate.
        Inputs: *_args - ignored.
        Output: tuple[None, str] - (no failure, formatted stdout).
        """
        text = "\n".join(
            f"${index}|{epoch}|1|{name}"
            for index, (name, epoch) in enumerate(self._rows)
        )
        return None, text

    list_attachable_sessions = TmuxBackend.list_attachable_sessions


def _badges(rows, **kwargs):
    """Resolve created_by_cloude for each row through the real backend code.

    Inputs: rows (list[tuple[str, int]]). **kwargs - owned_names and/or
      owned_instances, forwarded verbatim.
    Output: dict[str, bool] - tmux name -> created_by_cloude.
    """
    listing = _FakeProbeBackend(rows).list_attachable_sessions(**kwargs)
    assert listing.ok
    return {row["name"]: row["created_by_cloude"] for row in listing.sessions}


# --- the instance tier ------------------------------------------------------


def test_ownership_resolves_on_the_INSTANCE_not_the_name():
    """A reused name must not inherit the previous instance's badge.

    The scenario, concretely: the app owned a session called ``foo``. It
    died. The user made a brand new, unrelated ``foo``. Only the epoch
    tells them apart, and getting this wrong badges a stranger's process
    as the user's own.
    """
    badges = _badges(
        [("foo", 2000), ("bar", 3000)],
        owned_instances={("foo", 1000)},
    )
    assert badges["foo"] is False, (
        "the reused name inherited the dead instance's ownership badge"
    )
    assert badges["bar"] is False


def test_the_matching_instance_does_badge_as_ours():
    """The other half: same name AND same epoch is the same session."""
    badges = _badges([("foo", 1000)], owned_instances={("foo", 1000)})
    assert badges["foo"] is True


def test_a_None_epoch_is_NOT_a_wildcard_and_never_badges_a_stranger():
    """SUPERSEDES the old "(name, None) matches by name" guarantee.

    That guarantee was the defect. ``owned_tmux_instances()`` used to fold
    the legacy in-memory name set in as ``(name, None)`` and the backend
    read a None epoch as a NAME-ONLY WILDCARD - which disabled the epoch
    tier for every session the app had created since the last restart,
    i.e. exactly the population the epoch was added to protect. A dead
    ``foo`` replaced by the user's own unrelated ``foo`` badged as ours,
    which is verbatim the failure the epoch was introduced to close.

    A None epoch is now inert: it matches nothing. Legacy names still
    reach the backend, but through the separate ``owned_names`` argument
    and at their own explicitly name-only tier, where they cannot
    override a stored epoch.
    """
    badges = _badges([("foo", 9999)], owned_instances={("foo", None)})
    assert badges["foo"] is False


def test_a_stored_epoch_for_a_name_OVERRIDES_the_legacy_name_tier():
    """Tier 2: a specific DB opinion beats the lossy name set. THE D3 FIX.

    The datastore holds ``foo`` at epoch 1000. The live ``foo`` is a
    different instance at 9999, and the legacy in-memory set also holds
    the bare name ``foo`` because the app created the dead one this boot.
    The name tier must NOT rescue it: the DB has an epoch-keyed opinion
    about this name and this instance is not it.
    """
    badges = _badges(
        [("foo", 9999)],
        owned_instances={("foo", 1000)},
        owned_names={"foo"},
    )
    assert badges["foo"] is False


def test_the_legacy_name_tier_applies_when_the_DB_is_SILENT_on_that_name():
    """Tier 3 survives: no DB opinion at all means fall through to the name.

    A session the app created since the last restart has no row yet. The
    DB knows nothing about that NAME, so the degraded name-only tier is
    the best evidence available and is used. This is what keeps the fix
    from regressing the badge for freshly-created sessions.
    """
    badges = _badges(
        [("fresh", 9999)],
        owned_instances={("other", 1000)},
        owned_names={"fresh"},
    )
    assert badges["fresh"] is True


def test_owned_instances_takes_precedence_over_owned_names():
    """Most specific tier wins, or the lossy answer would mask the exact one."""
    badges = _badges(
        [("foo", 2000)],
        owned_names={"foo"},
        owned_instances={("foo", 1000)},
    )
    assert badges["foo"] is False


def test_the_name_tier_still_works_when_no_instances_are_supplied():
    """Back-compat: a caller with only names behaves exactly as before."""
    badges = _badges([("foo", 2000), ("bar", 1)], owned_names={"foo"})
    assert badges == {"foo": True, "bar": False}


def test_an_empty_instance_set_means_nothing_is_owned_not_fall_back():
    """An EMPTY set is an answer; None is the absence of one.

    Collapsing the two would make "the DB says this app owns nothing"
    silently reopen the spoofable prefix heuristic.
    """
    badges = _badges([("cloude_x", 1)], owned_instances=set())
    assert badges["cloude_x"] is False


def test_with_neither_source_the_prefix_heuristic_still_applies():
    """Unchanged fallback for callers outside the live app path."""
    badges = _badges([("cloude_x", 1), ("other", 2)])
    assert badges == {"cloude_x": True, "other": False}


# --- the structural guarantees ---------------------------------------------


def test_the_ownership_decision_lives_in_ONE_place_in_session_manager():
    """No call site may re-derive ownership from the raw legacy set.

    The original bug survived because three call sites answered this
    question three different ways, so the badge could be right on one
    screen and wrong on another. Every read now funnels through
    ``is_owned_tmux_name`` / ``owned_tmux_instances``, and the raw
    ``in self.owned_tmux_sessions`` membership test is allowed ONLY
    inside those helpers.
    """
    source = SESSION_MANAGER_PATH.read_text()
    tree = ast.parse(source)

    # The resolver itself, plus the three sites that legitimately touch
    # the legacy set for something that is NOT a badge. Each is named with
    # its reason so widening this list is a deliberate act:
    #
    #   rename_session          name-COLLISION avoidance, and re-keying the
    #                           set itself. Both are set maintenance, not
    #                           "is this session ours".
    #   destroy_external_session discards a name from the set. Maintenance.
    #
    # These disappear with the set itself in the follow-up commit.
    allowed = {
        "is_owned_tmux_name",
        "owned_tmux_instances",
        "rename_session",
        "destroy_external_session",
    }

    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func.name in allowed:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in node.ops):
                continue
            for comparator in node.comparators:
                if getattr(comparator, "attr", None) == "owned_tmux_sessions":
                    offenders.append((func.name, node.lineno))

    assert offenders == [], (
        "these functions test membership of owned_tmux_sessions directly "
        f"instead of going through the shared resolver: {offenders}. That "
        "is exactly how the badge came to disagree with itself before"
    )


def test_owned_tmux_sessions_is_still_alive_this_commit():
    """S4 must NOT delete the legacy set - that is a separate follow-up.

    It is removed only after scripts/verify_session_ownership_badge.py has
    passed against the DB as the source of truth. Deleting it in the same
    commit that introduces its replacement leaves no way to tell a
    cutover bug from a removal bug.
    """
    source = SESSION_MANAGER_PATH.read_text()
    assert "self.owned_tmux_sessions: set[str] = set()" in source
    assert 'payload["owned_tmux_sessions"] = sorted(self.owned_tmux_sessions)' in source


def test_the_adopt_path_now_persists_origin():
    """S7 INVERTS the S4 guard that used to live here. Read this before editing.

    The retired assertion was ``"adopt_instance" not in source`` - S4
    built the UPDATE and deliberately left it unwired, because wiring it
    flips the badge for adopted sessions and the shipped verifier
    asserted it stayed external. S7 is that wiring, and
    scripts/verify_session_ownership_badge.py moved with it.

    WHY THIS IS NOT A SOURCE GREP. The old form would have passed
    unchanged against the real S7 code, because session_manager reaches
    adoption through ``persist_adoption`` and never types the string
    ``adopt_instance`` at all. A guard that a change satisfies by
    accident is not a guard. Two rounds of adversarial review on S4 found
    the same shape twice: a proof constraining one module while the hole
    sat one layer up in the caller. So this asserts BEHAVIOUR at the
    layer that enforces it - the manager method the route calls - against
    a real database.
    """
    from contextlib import closing

    from src.core.db_models import SESSION_ORIGIN_ADOPTED
    from src.core.session_adopt_persist import persist_adoption
    from src.core.session_manager import SessionManager
    from tests.s7_helpers import (
        TEST_SOCKET,
        listing_of,
        listing_row,
        migrated_connection,
        session_row,
    )

    assert hasattr(SessionManager, "persist_adoption"), (
        "SessionManager.persist_adoption is the S7 wire between "
        "POST /sessions/adopt and sessions.origin. Without it an "
        "adoption is recorded nowhere and does not survive a restart"
    )
    # And the write it reaches must be the one that flips origin.
    import tempfile

    with tempfile.TemporaryDirectory() as state_dir:
        with closing(migrated_connection(Path(state_dir))) as conn:
            listing = listing_of([listing_row("ext", 4242)])
            result = persist_adoption(
                conn, socket=TEST_SOCKET, name="ext", listing=listing
            )
            assert result.persisted, result.detail
            stored = session_row(conn, "ext")
            assert stored["origin"] == SESSION_ORIGIN_ADOPTED
            assert stored["adopted_at"] is not None


def test_the_backend_resolution_order_is_documented_in_code():
    """The four tiers must be visible where the decision is made.

    The decision moved out of ``tmux_backend.list_attachable_sessions``
    and into ``tmux_listing_parse.resolve_ownership`` so it could be
    unit-tested against a hostile input without shelling out to tmux.
    This asserts the tiers are spelled there, including the NEGATIVE tier
    that closes the wildcard hole.
    """
    source = (ROOT / "src" / "core" / "tmux_listing_parse.py").read_text()
    assert "if (name, created_at_epoch) in owned_instances:" in source
    assert "return name in owned_names" in source
    assert "return bool(prefix) and name.startswith(prefix)" in source
    # tier 2: a stored epoch for this name is a specific NEGATIVE opinion
    assert "owned_name == name and owned_epoch is not None" in source
    # and the backend must not have grown a second, divergent copy
    backend = TMUX_BACKEND_PATH.read_text()
    assert "created_by_cloude = resolve_ownership(" in backend


def test_no_em_or_en_dashes_in_the_files_this_step_authored():
    """House style, checked by codepoint rather than by grep.

    grep here is a shell function that behaves like -I and silently skips
    files it deems binary, so an empty grep is not evidence of absence.
    """
    authored = [
        ROOT / "src" / "core" / "session_store.py",
        ROOT / "src" / "core" / "session_import.py",
        Path(__file__),
        ROOT / "tests" / "test_session_store.py",
        ROOT / "tests" / "test_session_import.py",
        ROOT / "src" / "core" / "tmux_listing.py",
        ROOT / "src" / "core" / "tmux_listing_parse.py",
        ROOT / "src" / "core" / "session_identity.py",
        ROOT / "tests" / "test_s4_adversarial.py",
        ROOT / "tests" / "test_tmux_listing_parse.py",
        ROOT / "tests" / "test_s4_regressions.py",
    ]
    for path in authored:
        text = path.read_text(encoding="utf-8")
        assert text.count(chr(8212)) == 0, f"em-dash in {path.name}"
        assert text.count(chr(8211)) == 0, f"en-dash in {path.name}"
