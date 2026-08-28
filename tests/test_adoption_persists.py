"""S7 - adoption is a fact on disk, not a set that dies with the process.

WHAT THIS SUITE HAS TO PROVE, and why each claim needs its own test.

Before S7 an adoption was recorded NOWHERE. The tmux name was
deliberately kept out of ``SessionManager.owned_tmux_sessions``, and that
set is in memory anyway, rebuilt from a live listing on every start. So
an adopted session was permanently EXTERNAL and the user's claim did not
survive a page reload, let alone a restart.

The decision S7 implements, which is not re-litigated here: ADOPTING AN
EXTERNAL SESSION MAKES IT OURS, PERMANENTLY. ``created`` and ``adopted``
both badge as ours; ``observed`` is the only external value; the
distinction stays in the column and is shown on the detail surface.

THE FOUR CLAIMS:

  1. DURABILITY ACROSS A PROCESS. Adopt, then throw the connection away
     and rebuild from the file. If the badge is still ours, it is on
     disk. This is the claim the in-memory set could never satisfy.
  2. DURABILITY ACROSS THE MANAGER. Adopt, drop the in-memory manager
     entirely, rebuild the ownership answer from the datastore alone,
     and the instance is still owned.
  3. ZERO ROWS IS NOT SUCCESS. An instance triple nothing carries yields
     a NAMED outcome, distinct from the corpse case and distinct from
     the datastore-unavailable case, and no row is marked adopted.
  4. FIRST-WRITE-WINS. ``adopted_at`` records the moment of the FIRST
     claim and does not move on re-entry. The UI re-opens sessions
     through the adopt path routinely, so a moving timestamp would
     rewrite history on every reload.

WHY THE ASSERTIONS RUN AGAINST A REAL MIGRATED DATABASE rather than a
hand-built table: a schema change must break these, not leave them
passing against a shape the product no longer has.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import (  # noqa: E402
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_adopt_persist import persist_adoption  # noqa: E402
from src.core.session_identity import (  # noqa: E402
    ADOPT_CLAIMED,
    claim_instance,
    record_instance,
)
from src.core.session_store import (  # noqa: E402
    is_owned_origin,
    owned_instances,
)
from tests.s7_helpers import (  # noqa: E402
    TEST_SOCKET,
    listing_of,
    listing_row,
    migrated_connection,
    session_row,
)

EPOCH = 1786913176
NAME = "cloudes7_ext"


@pytest.fixture()
def state_dir(tmp_path):
    """A per-test state directory holding a migrated cloude.db.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: Path - the state directory.
    """
    with closing(migrated_connection(tmp_path)):
        pass
    return tmp_path


@pytest.fixture()
def conn(state_dir):
    """An open connection to the per-test database.

    Inputs: state_dir (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    with closing(connect(db_path_for(state_dir))) as connection:
        yield connection


# --- claim 1: the badge survives a new process reading the same file -------


def test_adoption_survives_reopening_the_database_from_disk(state_dir):
    """Adopt, close every connection, reopen the FILE, and it is still ours.

    This is the claim the in-memory ``owned_tmux_sessions`` set could
    never make. The second connection shares nothing with the first
    except the bytes on disk, so a badge that reads owned here can only
    have come from a stored column.
    """
    with closing(connect(db_path_for(state_dir))) as first:
        result = persist_adoption(
            first,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH)]),
        )
        assert result.persisted, result.detail
        first.commit()

    # A genuinely separate connection. Nothing in memory carries over.
    with closing(connect(db_path_for(state_dir))) as second:
        stored = session_row(second, NAME)
        assert stored["origin"] == SESSION_ORIGIN_ADOPTED
        assert is_owned_origin(stored["origin"]) is True
        assert (NAME, EPOCH) in owned_instances(second, socket=TEST_SOCKET)


def test_the_row_is_observed_before_adoption_and_adopted_after(conn):
    """The flip happens at the claim, exactly once, and only then.

    Guards the specific regression that a sighting alone marks a session
    as ours. Recording that we have SEEN an instance and recording that
    the user has CLAIMED it are two different facts.
    """
    record_instance(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_OBSERVED,
    )
    assert session_row(conn, NAME)["origin"] == SESSION_ORIGIN_OBSERVED
    assert owned_instances(conn, socket=TEST_SOCKET) == set()

    assert claim_instance(
        conn, socket=TEST_SOCKET, name=NAME, epoch=EPOCH
    ).claimed
    assert session_row(conn, NAME)["origin"] == SESSION_ORIGIN_ADOPTED
    assert (NAME, EPOCH) in owned_instances(conn, socket=TEST_SOCKET)


def test_a_later_observed_sighting_does_not_demote_an_adopted_session(conn):
    """A poll seeing the session again must not un-adopt it.

    ``origin`` is written once and never recomputed. A merge refreshes
    liveness columns and touches nothing else, which is what makes the
    badge survive every subsequent listing.
    """
    persist_adoption(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        listing=listing_of([listing_row(NAME, EPOCH)]),
    )
    record_instance(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_OBSERVED,
    )
    assert session_row(conn, NAME)["origin"] == SESSION_ORIGIN_ADOPTED


# --- claim 2: rebuilt from disk with no manager in the picture -------------


def test_ownership_rebuilds_from_disk_with_no_in_memory_manager(state_dir):
    """Drop the manager entirely; the datastore alone still says adopted.

    ``owned_instances`` is the single source the ownership resolver
    reads. Asserting it directly, from a connection opened after
    everything else is gone, is the layer that actually enforces the
    guarantee - not the manager attribute that merely forwards it.
    """
    with closing(connect(db_path_for(state_dir))) as writer:
        persist_adoption(
            writer,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH)]),
        )
        writer.commit()

    from src.core.tmux_listing_parse import resolve_ownership

    with closing(connect(db_path_for(state_dir))) as reader:
        instances = owned_instances(reader, socket=TEST_SOCKET)

    # No manager, no legacy name set, nothing in memory. The badge is
    # answered from the stored instance triple alone.
    assert resolve_ownership(
        NAME, EPOCH, instances, set(), prefix="cloude_"
    ) is True


def test_a_different_instance_of_the_same_name_is_not_adopted(state_dir):
    """Name reuse must not inherit the adoption.

    The epoch is in the identity key precisely so that a session which
    died and had its name taken cannot be badged as the user's. This is
    the negative half of the previous test and it must fail closed.
    """
    with closing(connect(db_path_for(state_dir))) as writer:
        persist_adoption(
            writer,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH)]),
        )
        writer.commit()

    from src.core.tmux_listing_parse import resolve_ownership

    with closing(connect(db_path_for(state_dir))) as reader:
        instances = owned_instances(reader, socket=TEST_SOCKET)

    assert resolve_ownership(
        NAME, EPOCH + 1, instances, set(), prefix="cloude_"
    ) is False


# --- claim 4: adopted_at is first-write-wins -------------------------------


def test_adopted_at_keeps_its_first_value_across_repeat_adoptions(conn):
    """The UI re-opens sessions through the adopt path routinely.

    ``adopted_at`` answers "when did this become ours", and that moment
    is the FIRST claim. A second call must be a genuine no-op on that
    column while remaining free to refresh everything else.
    """
    first_stamp = "2026-01-01T00:00:00Z"
    second_stamp = "2026-06-01T00:00:00Z"
    record_instance(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_OBSERVED,
    )

    assert claim_instance(
        conn, socket=TEST_SOCKET, name=NAME, epoch=EPOCH, now=first_stamp
    ).claimed
    assert claim_instance(
        conn, socket=TEST_SOCKET, name=NAME, epoch=EPOCH, now=second_stamp
    ).claimed

    stored = session_row(conn, NAME)
    assert stored["adopted_at"] == first_stamp, (
        "adopted_at moved on re-adoption. The UI re-enters this path on "
        "every session re-open, so a moving stamp rewrites history "
        "constantly"
    )
    # The rest of the statement IS idempotent-by-overwrite.
    assert stored["updated_at"] == second_stamp
    assert stored["origin"] == SESSION_ORIGIN_ADOPTED


def test_repeat_adoption_through_the_full_path_also_holds_the_stamp(conn):
    """The same guarantee through persist_adoption, not just the store.

    The store function having COALESCE proves nothing if the orchestration
    above it deletes and re-inserts the row, which is exactly the kind of
    hole an assertion one layer down would miss.
    """
    listing = listing_of([listing_row(NAME, EPOCH)])
    first = persist_adoption(
        conn, socket=TEST_SOCKET, name=NAME, listing=listing,
        now="2026-02-02T00:00:00Z",
    )
    assert first.persisted
    stamp = session_row(conn, NAME)["adopted_at"]
    uuid_before = session_row(conn, NAME)["session_uuid"]

    second = persist_adoption(
        conn, socket=TEST_SOCKET, name=NAME, listing=listing,
        now="2026-09-09T00:00:00Z",
    )
    assert second.persisted
    after = session_row(conn, NAME)
    assert after["adopted_at"] == stamp
    assert after["session_uuid"] == uuid_before, (
        "re-adoption minted a new external identity, so every reference "
        "to the old one is now dangling"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE tmux_name = ?", (NAME,)
    ).fetchone()[0] == 1


def test_adopt_result_claimed_is_true_only_for_the_claimed_outcome():
    """The property every caller branches on, checked against all four.

    ``persisted``/``claimed`` returning True for anything but a real
    claim would make every failure above report as success at the call
    site, which is the whole reason these are named outcomes.
    """
    from src.core.session_identity import ADOPT_FAILURES, AdoptResult

    assert AdoptResult(outcome=ADOPT_CLAIMED).claimed is True
    for outcome in ADOPT_FAILURES:
        assert AdoptResult(outcome=outcome).claimed is False
    assert len(set(ADOPT_FAILURES)) == 3
