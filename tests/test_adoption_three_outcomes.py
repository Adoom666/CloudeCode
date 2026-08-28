"""S7 - what an adoption that claims NOTHING must say, and to whom.

Split out of tests/test_adoption_persists.py, which keeps the
durability claims. The split follows the risk: that file proves an
adoption STICKS, this one proves a FAILED adoption is never mistaken for
a successful one, at each of the three layers a user's answer passes
through.

ADOPTION IS AN UPDATE, so it can match zero rows. When it does, the
honest answers are:

  no_such_instance     no row carries that instance triple. Adoption
                       updates; it never invents, because an invented row
                       would carry an origin nobody measured.
  not_running          the row exists and is stopped. The process it
                       described is gone, and claiming it would
                       permanently badge a corpse as the user's.
  no_datastore         we could not evaluate the claim at all. Nothing is
                       known to be wrong with the SESSION; we simply have
                       nowhere to record it. This is the only one of the
                       three that is not a measurement.

Plus two that happen before the UPDATE is even reachable:

  session_gone         the listing RAN and this instance is not in it, so
                       the session died between the client's list and its
                       click. NOT a 500, NOT a 200 - a named 409 with a
                       refresh.
  listing_unavailable  the probe could not run, so whether the session
                       exists is unknown. Never rendered as gone.

WHY THE SAME CLAIM IS ASSERTED AT THREE LAYERS. Two rounds of
adversarial review on S4 both found the same shape: a proof constraining
one module while the hole sat one layer up in the caller. A named
outcome in the store proves nothing about the status code the client
reads, and a route test that injects its own manager proves nothing
about the manager. So: the store returns the outcome, the MANAGER turns
the gone case into a distinct exception type rather than the bare
RuntimeError it already raises for genuine faults, and the ROUTE turns
that type into a 409 carrying a refresh instruction.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import (  # noqa: E402
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_CREATED,
)
from src.core.session_adopt_persist import (  # noqa: E402
    PERSIST_LISTING_UNAVAILABLE,
    PERSIST_SESSION_GONE,
    persist_adoption,
)
from src.core.session_identity import (  # noqa: E402
    ADOPT_CLAIMED,
    ADOPT_NO_DATASTORE,
    ADOPT_NO_SUCH_INSTANCE,
    ADOPT_NOT_RUNNING,
    claim_instance,
    record_instance,
)
from tests.s7_helpers import (  # noqa: E402
    TEST_SOCKET,
    listing_of,
    listing_row,
    listing_unavailable,
    migrated_connection,
    session_row,
)

EPOCH = 1786913176
NAME = "cloudes7_ext"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    with closing(migrated_connection(tmp_path)):
        pass
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


# --- claim 3: zero rows updated is a NAMED failure, never a success --------


def test_claiming_an_absent_instance_updates_zero_rows_and_says_so(conn):
    """No row carries that triple: a distinct, named negative.

    Adoption UPDATES; it never invents. An invented row would carry an
    ``origin`` nobody measured.
    """
    before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    result = claim_instance(
        conn, socket=TEST_SOCKET, name="never-existed", epoch=999
    )
    assert result.claimed is False
    assert result.outcome == ADOPT_NO_SUCH_INSTANCE
    assert result.determined is True
    after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert after == before, "a failed claim must not create a row"


def test_the_three_failure_outcomes_are_distinguishable_from_each_other(conn):
    """no_such_instance, not_running and no_datastore are three answers.

    A bare False collapses them, and the UI has to say something
    different for each: refresh the list, that session stopped, and we
    could not record it. Only the third is a could-not-evaluate.
    """
    record_instance(
        conn,
        socket=TEST_SOCKET,
        name="corpse",
        epoch=EPOCH,
        origin=SESSION_ORIGIN_CREATED,
        lifecycle=SESSION_LIFECYCLE_STOPPED,
    )
    stopped = claim_instance(
        conn, socket=TEST_SOCKET, name="corpse", epoch=EPOCH
    )
    absent = claim_instance(
        conn, socket=TEST_SOCKET, name="ghost", epoch=EPOCH
    )

    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    no_datastore = claim_instance(
        empty, socket=TEST_SOCKET, name="anything", epoch=EPOCH
    )
    empty.close()

    assert stopped.outcome == ADOPT_NOT_RUNNING
    assert absent.outcome == ADOPT_NO_SUCH_INSTANCE
    assert no_datastore.outcome == ADOPT_NO_DATASTORE
    assert len({stopped.outcome, absent.outcome, no_datastore.outcome}) == 3
    assert all(r.claimed is False for r in (stopped, absent, no_datastore))
    # The could-not-evaluate is the ONLY one that is not a measurement.
    assert stopped.determined is True
    assert absent.determined is True
    assert no_datastore.determined is False
    # And the corpse keeps the origin it had.
    assert session_row(conn, "corpse")["origin"] == SESSION_ORIGIN_CREATED


def test_a_session_that_died_between_listing_and_adopt_is_named_gone(conn):
    """The adopt path's zero-row case, at the layer the route calls.

    The listing RAN and does not contain the name, so the session died
    between the client's list and its click. That is neither success nor
    a server fault, and NO ROW IS MARKED ADOPTED.
    """
    result = persist_adoption(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        listing=listing_of([listing_row("someone-else", 55)]),
    )
    assert result.persisted is False
    assert result.outcome == PERSIST_SESSION_GONE
    assert "no longer there" in (result.detail or "")
    assert session_row(conn, NAME) is None


def test_an_unavailable_listing_is_not_reported_as_gone(conn):
    """A probe that could not run must never say the session died.

    ``ok=False`` carries no rows BY CONTRACT, so a missing name proves
    nothing. Collapsing this into ``session_gone`` would tell the user
    his live session had vanished because tmux timed out - the exact
    false verdict, in the other direction.
    """
    result = persist_adoption(
        conn,
        socket=TEST_SOCKET,
        name=NAME,
        listing=listing_unavailable("timeout"),
    )
    assert result.outcome == PERSIST_LISTING_UNAVAILABLE
    assert result.outcome != PERSIST_SESSION_GONE
    assert session_row(conn, NAME) is None, "nothing may be written"


def test_the_manager_raises_the_NAMED_gone_error_not_a_bare_runtime_error():
    """The layer BETWEEN the store and the route, which had no test.

    ``adopt_external_session`` already raises RuntimeError for a dead
    pane and a failed pipe-pane setup, and those are genuine server
    faults that SHOULD be 500s. A session that merely died between the
    client's listing and its click is not a fault at all, so it must
    raise the DISTINCT type the route catches. Raising a bare
    RuntimeError here would put it back in the 500 bucket while every
    store-level and route-level test still passed - the exact
    proof-one-layer-below-the-hole shape.
    """
    import asyncio

    from src.core.session_adopt_persist import (
        AdoptPersistResult,
        AdoptTargetGoneError,
    )
    from src.core.session_manager import SessionManager

    # A bare instance: adopt_external_session touches only ``backends``
    # before the persistence gate, so nothing else has to be built. That
    # is deliberate - constructing a full manager would drag in tmux.
    manager = object.__new__(SessionManager)
    manager.backends = {}
    manager.persist_adoption = lambda _name: AdoptPersistResult(
        outcome=PERSIST_SESSION_GONE, detail="that session is no longer there"
    )

    with pytest.raises(AdoptTargetGoneError) as caught:
        asyncio.run(manager.adopt_external_session(name=NAME))
    assert "no longer there" in str(caught.value)
    # And it must NOT be the type the error middleware turns into a 500
    # by way of the other RuntimeErrors this method already raises.
    assert type(caught.value) is not RuntimeError


def test_the_manager_does_not_abort_the_adoption_for_other_persist_failures():
    """A bookkeeping write that failed must not cost the user his session.

    Only the gone case refuses. Everything else logs and continues, so a
    momentarily unreadable datastore degrades the BADGE rather than the
    feature. Proved by getting past the gate to the next step, which
    fails for an unrelated reason.
    """
    import asyncio

    from src.core.session_adopt_persist import (
        PERSIST_LISTING_UNAVAILABLE,
        AdoptPersistResult,
        AdoptTargetGoneError,
    )
    from src.core.session_manager import SessionManager

    manager = object.__new__(SessionManager)
    manager.backends = {}
    manager.persist_adoption = lambda _name: AdoptPersistResult(
        outcome=PERSIST_LISTING_UNAVAILABLE, detail="datastore unreadable"
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(manager.adopt_external_session(name=NAME))
    assert not isinstance(caught.value, AdoptTargetGoneError), (
        "a failed bookkeeping write refused the adoption. The user must "
        "still get his session; only the badge degrades"
    )


def test_the_route_answers_409_session_gone_rather_than_500():
    """At the ROUTE layer, because that is where the user's client reads it.

    Asserting the store returns a named outcome proves nothing about the
    status code, and the brief's failure shape is precisely a proof one
    layer below the hole. This drives POST /sessions/adopt.
    """
    import asyncio

    import httpx

    from src.api.auth import require_auth
    from src.core.session_adopt_persist import AdoptTargetGoneError

    os.environ.setdefault("TOTP_SECRET", "s7secretnotreal")
    os.environ.setdefault("JWT_SECRET", "s7jwtnotreal")
    from src.main import app

    class _GoneManager:
        """A session manager whose adopt raises the gone error."""

        async def adopt_external_session(self, **_kwargs):
            """Always report the target as gone.

            Inputs: **_kwargs - ignored.
            Output: never returns.
            Raises: AdoptTargetGoneError.
            """
            raise AdoptTargetGoneError("that session is no longer there")

    previous = getattr(app.state, "session_manager", None)
    app.state.session_manager = _GoneManager()
    app.dependency_overrides[require_auth] = lambda: True

    async def _drive():
        """POST /sessions/adopt against the gone manager.

        Inputs: none.
        Output: httpx.Response.
        """
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://s7.local/api/v1"
        ) as client:
            return await client.post(
                "/sessions/adopt",
                json={"session_name": NAME, "confirm_detach": False},
            )

    try:
        response = asyncio.run(_drive())
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.state.session_manager = previous

    assert response.status_code == 409, response.text
    body = response.json()["detail"]
    assert body["error"] == "session_gone"
    assert body["refresh"] is True
    assert body["session_name"] == NAME
