"""Boot must hold EVERY surviving session, not just the last one.

WHAT WAS MEASURED, and why a suite exists for it. On the owner's box the
socket carried 21 live ``cloude_*`` sessions and a restart held ZERO. The
boot path was written for one session and its docstring still said so
("persist/rehydrate at most ONE session across restarts"), while
concurrent sessions had long since become a runtime feature.

The 20 unheld sessions were not merely missing from a list. ``GET
/sessions/list`` reads in-memory state, so each one vanished from it and
got painted from ``/sessions/attachable`` instead, where the client's
``actionsFor(undefined)`` offers close and nothing else. The hook
endpoint answered 410 Gone for ids it had never registered - 29 dropped
events on one boot, unretryable from the agent's side. And opening one
ran the ADOPT path, minting a fresh ``adopted:`` id with a new
``created_at`` and a hook token the running agent can never read.

The single-session path was ALSO broken, separately and for a different
reason: ``build_backend`` was handed the persisted ``id`` and no
``session_name``, so it re-derived ``cloude_<slug(id)>``. For an adopted
session already named ``adopted:cloude_Media_Compression`` that produced
``cloude_adopted_cloude_Media_Compression`` - a name no socket has ever
carried. The live log's ``session_metadata_slug_not_in_backend`` followed
by ``stale_session_metadata_deleted`` is exactly that.

SIX CLAIMS, one per failure mode:

  1. POSITIVE - every session with a matching row comes back, under the
     STORED id and attached to the STORED tmux name.
  2. NEGATIVE - a ``cloude_*`` session with no row is NOT claimed. A pass
     that holds everything it can see is worse than one that holds
     nothing, because it silently takes over a stranger's session.
  3. FAILURE - a listing that did not run holds nothing, changes no
     ownership record, and the attachable route says 503 with a reason.
     Not zero. No answer.
  4. REGRESSION - the metadata's own ``tmux_session`` is what gets
     attached, not a re-derivation of it.
  5. IDEMPOTENCE - running the pass twice holds each session once.
  6. THE HOOK PATH - an event for a re-adopted session raises a toast
     rather than 410, which is the user-visible point of all of this.

SAFETY. Hermetic. Every backend is a double, every path is under
``tmp_path``, and nothing here opens a tmux socket - the conftest guard
would fail the run if it tried.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_bra_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_bra_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.config import settings
from src.core.db import connect, db_path_for
from src.core.db_models import SESSION_ORIGIN_ADOPTED, SESSION_ORIGIN_CREATED
from src.core.session_boot_readopt import (
    ID_SOURCE_DERIVED,
    ID_SOURCE_HOOK_TOKEN,
    READOPT_CANNOT_DETERMINE,
    READOPT_RAN,
    SKIP_ALREADY_HELD,
    SKIP_NO_ROW,
    readopt_surviving_sessions,
)
from src.core.session_identity import record_instance
from src.core.session_manager import SessionManager
from src.core.tmux_backend import SESSION_PREFIX, _slugify
from src.core.tmux_listing import TmuxListing
from src.models import Session
from tests.s7_helpers import migrated_connection

EPOCH_A = 1786913001
EPOCH_B = 1786913002
EPOCH_C = 1786913003


# --------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------- #


class FakeBackend:
    """A tmux backend double that records what it was asked to attach.

    Description: mirrors ``TmuxBackend``'s NAMING rule exactly - an
      explicit ``session_name`` wins, otherwise ``cloude_<slug(id)>`` is
      derived. That fidelity is the whole point: the double-prefix
      regression is a naming bug, and a double that always used the name
      it was handed could not detect it.
    Inputs (constructor): the ``build_backend`` keyword shape.
    Output: a FakeBackend.
    """

    def __init__(
        self,
        session_id,
        working_dir,
        on_output=None,
        socket_name="cloudetest",
        scrollback_lines=3000,
        session_name=None,
    ):
        self.session_id = session_id
        self.working_dir = working_dir
        self.socket_name = socket_name
        if session_name is not None:
            self.tmux_session = session_name
        else:
            self.tmux_session = f"{SESSION_PREFIX}{_slugify(session_id)}"
        self.attach_calls = 0
        self.needs_pipe_setup = None
        self.rows = []
        self.discover = TmuxListing.answered([])
        self.attachable = TmuxListing.answered([])

    def discover_existing(self):
        """The name-only boot listing. Inputs: none. Output: TmuxListing."""
        return self.discover

    def list_attachable_sessions(self, owned_names=None, owned_instances=None):
        """The epoch-bearing listing. Inputs: ignored. Output: TmuxListing."""
        return self.attachable

    def is_alive(self):
        """Inputs: none. Output: bool - always True for a double."""
        return True

    async def attach_existing(self, needs_pipe_setup=False):
        """Record the attach. Inputs: needs_pipe_setup (bool). Output: None."""
        self.attach_calls += 1
        self.needs_pipe_setup = needs_pipe_setup


def install_backends(monkeypatch, *, discover, attachable):
    """Point every ``build_backend`` call site at FakeBackend.

    Description: ``session_manager`` imports the factory at module load
      and ``session_boot_readopt`` imports it inside the function, so
      BOTH bindings have to be replaced or half the code under test
      still reaches for real tmux.
    Inputs: monkeypatch. discover (TmuxListing) - what
      ``discover_existing`` answers. attachable (TmuxListing) - what the
      epoch-bearing probe answers.
    Output: list[FakeBackend] - every instance built, in order.
    """
    built = []

    def factory(settings_obj, session_id, working_dir, on_output=None,
                session_name=None):
        backend = FakeBackend(
            session_id, working_dir, on_output, session_name=session_name
        )
        backend.discover = discover
        backend.attachable = attachable
        built.append(backend)
        return backend

    monkeypatch.setattr("src.core.session_manager.build_backend", factory)
    monkeypatch.setattr("src.core.session_backend.build_backend", factory)
    return built


def listing_rows(*specs):
    """Build epoch-bearing listing rows.

    Inputs: specs (tuple[str, int]) - ``(name, epoch)`` pairs.
    Output: TmuxListing with ok=True.
    Example: listing_rows(('cloude_a', 1))
    """
    return TmuxListing.answered(
        [
            {"name": name, "created_at_epoch": epoch, "window_count": 1,
             "created_by_cloude": False, "working_dir": None}
            for name, epoch in specs
        ]
    )


# --------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A wired-up state directory holding a migrated cloude.db.

    Inputs: tmp_path, monkeypatch.
    Output: Path - the state directory ``settings`` now resolves to.
    """
    log_dir = tmp_path / "logs"
    state = tmp_path / "state"
    log_dir.mkdir()
    state.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(log_dir))
    monkeypatch.setattr(settings, "state_dir_override", str(state))
    with closing(migrated_connection(state)):
        pass
    return state


def seed_row(state_dir, manager, *, name, epoch, origin=SESSION_ORIGIN_CREATED,
             working_dir="/tmp", agent_type=None, legacy_session_id=None):
    """Write one sessions row keyed on the instance triple.

    Description: goes through ``record_instance``, the production write,
      so a schema change breaks this instead of leaving it green against
      a shape the product no longer has.
    Inputs: state_dir (Path). manager (SessionManager) - names the
      socket. name (str). epoch (int). origin (str). working_dir (str).
      agent_type (str | None). legacy_session_id (str | None).
    Output: None.
    """
    fields = {"working_dir": working_dir}
    if agent_type:
        fields["agent_type"] = agent_type
    if legacy_session_id:
        fields["legacy_session_id"] = legacy_session_id
    with closing(connect(db_path_for(state_dir))) as conn:
        record_instance(
            conn,
            socket=manager._tmux_socket_name(),
            name=name,
            epoch=epoch,
            origin=origin,
            **fields,
        )
        conn.commit()


async def _noop(*_args, **_kwargs):
    """Inputs: anything. Output: None."""
    return None


# --------------------------------------------------------------------- #
# 1. POSITIVE
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_session_with_a_row_is_held_under_its_stored_id(
    state_dir, monkeypatch
):
    """Three live sessions, three rows, three held - by stored id and name.

    THE OLD CODE HELD AT MOST ONE, and only the one named in
    ``session_metadata.json``. Two of these carry a hook-token record, so
    their ids are RECOVERED (the id the running agent presents on every
    hook); the third has none and gets the derived id.
    """
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "_sweep_orphan_uploads", _noop)

    for name, epoch in (("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B),
                        ("cloude_gamma", EPOCH_C)):
        seed_row(state_dir, mgr, name=name, epoch=epoch, agent_type="claude")

    # Two ids the pane environments already carry.
    mgr._hook_tmux_names = {
        "ses_alpha": "cloude_alpha",
        "ses_beta": "cloude_beta",
    }

    install_backends(
        monkeypatch,
        discover=TmuxListing.answered(
            ["cloude_alpha", "cloude_beta", "cloude_gamma"]
        ),
        attachable=listing_rows(
            ("cloude_alpha", EPOCH_A),
            ("cloude_beta", EPOCH_B),
            ("cloude_gamma", EPOCH_C),
        ),
    )

    await mgr.lifespan_startup()
    report = await mgr._boot_readopt_task

    assert report.outcome == READOPT_RAN
    assert set(mgr.sessions) == {"ses_alpha", "ses_beta", "adopted:cloude_gamma"}

    by_name = {
        backend.tmux_session: sid for sid, backend in mgr.backends.items()
    }
    assert by_name == {
        "cloude_alpha": "ses_alpha",
        "cloude_beta": "ses_beta",
        "cloude_gamma": "adopted:cloude_gamma",
    }
    # Every one was actually attached, with the dead-pane-refusing path.
    assert all(b.attach_calls == 1 for b in mgr.backends.values())
    assert all(b.needs_pipe_setup is True for b in mgr.backends.values())

    # The stored agent_type is carried over, NOT re-fingerprinted.
    assert mgr.sessions["ses_alpha"].agent_type == "claude"
    assert mgr.sessions["ses_alpha"].agent_type_via_fingerprint is False
    # created_at is the instance's tmux BIRTH, not "now". Naive UTC, the
    # convention every other Session.created_at in this codebase uses
    # (``default_factory=datetime.utcnow``), so it is compared against a
    # naive-UTC value rather than through ``.timestamp()`` - which would
    # read a naive datetime as LOCAL time and silently pass or fail by
    # the machine's offset.
    assert mgr.sessions["ses_alpha"].created_at == datetime.fromtimestamp(
        EPOCH_A, tz=timezone.utc
    ).replace(tzinfo=None)

    sources = {t.session_id: t.id_source for t in report.plan.targets}
    assert sources["ses_alpha"] == ID_SOURCE_HOOK_TOKEN
    assert sources["adopted:cloude_gamma"] == ID_SOURCE_DERIVED


@pytest.mark.asyncio
async def test_an_adopted_origin_row_is_held_too(state_dir, monkeypatch):
    """``adopted`` is ownership, same as ``created``. Both come back."""
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "_sweep_orphan_uploads", _noop)
    seed_row(
        state_dir, mgr, name="my_own_tmux", epoch=EPOCH_A,
        origin=SESSION_ORIGIN_ADOPTED, legacy_session_id="ses_legacy",
    )
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(("my_own_tmux", EPOCH_A)),
    )

    report = await readopt_surviving_sessions(mgr)

    assert report.held == ["ses_legacy"]
    assert mgr.backends["ses_legacy"].tmux_session == "my_own_tmux"


# --------------------------------------------------------------------- #
# 2. NEGATIVE - a stranger is never claimed
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_cloude_session_with_no_row_is_not_claimed(
    state_dir, monkeypatch
):
    """Seeing a session is not owning it.

    ``cloude_stranger`` is on our socket and looks exactly like ours. It
    has no row, so it stays observed and adoptable, and the reason is
    NAMED rather than swallowed by a bare ``continue``.
    """
    mgr = SessionManager()
    seed_row(state_dir, mgr, name="cloude_mine", epoch=EPOCH_A)
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(
            ("cloude_mine", EPOCH_A), ("cloude_stranger", EPOCH_B)
        ),
    )

    report = await readopt_surviving_sessions(mgr)

    assert report.plan.skipped_for(SKIP_NO_ROW) == ["cloude_stranger"]
    held_names = {b.tmux_session for b in mgr.backends.values()}
    assert held_names == {"cloude_mine"}
    # And nothing invented an ownership record for it either, which is
    # what the attachable listing reads to answer created_by_cloude.
    owned = mgr.owned_tmux_instances() or set()
    assert ("cloude_stranger", EPOCH_B) not in owned
    assert ("cloude_mine", EPOCH_A) in owned


# --------------------------------------------------------------------- #
# 3. FAILURE - a probe that did not run
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_listing_that_did_not_run_holds_nothing_and_disowns_nothing(
    state_dir, monkeypatch
):
    """No answer is not zero.

    The rows stay, the ownership record stays, nothing is held, and the
    outcome says ``cannot_determine`` rather than reporting a clean pass
    over an empty socket.
    """
    mgr = SessionManager()
    seed_row(state_dir, mgr, name="cloude_mine", epoch=EPOCH_A)
    before = mgr.owned_tmux_instances()

    install_backends(
        monkeypatch,
        discover=TmuxListing.unavailable("tmux_missing"),
        attachable=TmuxListing.unavailable("tmux_missing"),
    )

    report = await readopt_surviving_sessions(mgr)

    assert report.outcome == READOPT_CANNOT_DETERMINE
    assert report.held == []
    assert report.plan is None, "nothing was decided, so there is no plan"
    assert mgr.backends == {}
    assert mgr.owned_tmux_instances() == before


@pytest.mark.asyncio
async def test_boot_does_not_even_schedule_the_pass_on_a_failed_probe(
    state_dir, monkeypatch
):
    """The gate is structural: no listing past the gate, no task at all."""
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "_sweep_orphan_uploads", _noop)
    install_backends(
        monkeypatch,
        discover=TmuxListing.unavailable("timeout"),
        attachable=listing_rows(("cloude_mine", EPOCH_A)),
    )

    await mgr.lifespan_startup()

    assert mgr._boot_listing is None
    assert mgr._boot_readopt_task is None
    assert mgr.sessions == {}


@pytest.mark.asyncio
async def test_the_attachable_route_answers_503_with_a_reason(
    state_dir, monkeypatch
):
    """The user-facing half of "no answer": 503 carrying ``listing_reason``.

    A 200 with an empty list would tell the client every session is gone,
    which is what the three-outcome rule exists to prevent.
    """
    from fastapi import HTTPException

    from src.api.routes import list_attachable_sessions

    mgr = SessionManager()
    install_backends(
        monkeypatch,
        discover=TmuxListing.unavailable("tmux_missing"),
        attachable=TmuxListing.unavailable("tmux_missing"),
    )

    class _App:
        class state:  # noqa: N801 - mimics starlette's attribute bag
            session_manager = mgr

    class _Request:
        app = _App

    with pytest.raises(HTTPException) as excinfo:
        await list_attachable_sessions(_Request)

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["listing_reason"] == "tmux_missing"


# --------------------------------------------------------------------- #
# 4. REGRESSION - the stored tmux name is what gets attached
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_metadata_tmux_name_is_what_gets_attached(
    state_dir, monkeypatch
):
    """The double-prefix bug, reproduced through the real reconcile path.

    An adopted session persists as ``id='adopted:cloude_Media_Compression'``
    with ``tmux_session='cloude_Media_Compression'``. Deriving the name
    from the id yields ``cloude_adopted_cloude_Media_Compression``, which
    is in no listing, so the session is dropped and its pointer deleted.
    """
    import json

    name = "cloude_Media_Compression"
    sid = f"adopted:{name}"
    payload = Session(
        id=sid, working_dir="/tmp/wd", tmux_session=name, agent_type="claude"
    ).model_dump()
    payload["owned_tmux_sessions"] = [name]
    (state_dir / "session_metadata.json").write_text(
        json.dumps(payload, indent=2, default=str)
    )

    mgr = SessionManager()
    monkeypatch.setattr(mgr, "_sweep_orphan_uploads", _noop)
    assert mgr.current_session() is not None, "metadata did not load"

    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([name]),
        attachable=listing_rows((name, EPOCH_A)),
    )

    await mgr.lifespan_startup()
    if mgr._boot_readopt_task is not None:
        await mgr._boot_readopt_task

    assert sid in mgr.sessions, (
        "the persisted session was dropped; the backend was almost "
        "certainly built for cloude_adopted_cloude_Media_Compression"
    )
    assert mgr.backends[sid].tmux_session == name
    # And the metadata file survived, rather than being cleared as stale.
    assert (state_dir / "session_metadata.json").exists()


def test_a_metadata_file_without_a_tmux_name_keeps_the_legacy_derivation():
    """The fallback must stay a fallback.

    Pre-``tmux_session`` metadata carries None there. If the fix made the
    stored name mandatory, every legacy install would lose its rehydrate
    - so the derivation has to remain reachable.
    """
    legacy = Session(id="ses_abc", working_dir="/tmp")
    assert legacy.tmux_session is None
    derived = FakeBackend("ses_abc", "/tmp", session_name=legacy.tmux_session or None)
    assert derived.tmux_session == f"{SESSION_PREFIX}{_slugify('ses_abc')}"


# --------------------------------------------------------------------- #
# 5. IDEMPOTENCE
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_running_the_pass_twice_holds_each_session_once(
    state_dir, monkeypatch
):
    """Hook events arrive twice; so may a reconcile. Neither may double up.

    The second pass must recognise its own work as already held - by name
    AND by id - and attach nothing again. A second attach would leak a
    second pipe-pane tailer onto one FIFO.
    """
    mgr = SessionManager()
    seed_row(state_dir, mgr, name="cloude_alpha", epoch=EPOCH_A)
    mgr._hook_tmux_names = {"ses_alpha": "cloude_alpha"}
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(("cloude_alpha", EPOCH_A)),
    )

    first = await readopt_surviving_sessions(mgr)
    second = await readopt_surviving_sessions(mgr)

    assert first.held == ["ses_alpha"]
    assert second.held == []
    assert second.plan.skipped_for(SKIP_ALREADY_HELD) == ["cloude_alpha"]
    assert list(mgr.sessions) == ["ses_alpha"]
    assert mgr.backends["ses_alpha"].attach_calls == 1


@pytest.mark.asyncio
async def test_one_refused_pane_does_not_take_the_others_down(
    state_dir, monkeypatch
):
    """A dead pane is refused by name; its neighbours are still held.

    ``gather`` runs these concurrently, so an exception that was not
    contained would cancel the siblings mid-attach.
    """
    mgr = SessionManager()
    for name, epoch in (("cloude_alpha", EPOCH_A), ("cloude_dead", EPOCH_B)):
        seed_row(state_dir, mgr, name=name, epoch=epoch)
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(
            ("cloude_alpha", EPOCH_A), ("cloude_dead", EPOCH_B)
        ),
    )

    original = FakeBackend.attach_existing

    async def refusing(self, needs_pipe_setup=False):
        if self.tmux_session == "cloude_dead":
            raise RuntimeError("cannot adopt cloude_dead: pane already dead")
        await original(self, needs_pipe_setup=needs_pipe_setup)

    monkeypatch.setattr(FakeBackend, "attach_existing", refusing)

    report = await readopt_surviving_sessions(mgr)

    assert report.held == ["adopted:cloude_alpha"]
    assert [name for name, _err in report.failed] == ["cloude_dead"]
    assert "adopted:cloude_dead" not in mgr.sessions


# --------------------------------------------------------------------- #
# 6. THE HOOK PATH - a toast, not a 410
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_hook_for_a_re_adopted_session_raises_a_toast(
    state_dir, monkeypatch
):
    """The whole user-visible point.

    ``record_toast`` raises ValueError for an unknown id and the hook
    route turns that into 410 Gone, which the agent cannot retry - 29
    events were dropped that way on one boot. Re-adopting under the id
    the pane's environment already carries is what makes the id known
    again.
    """
    mgr = SessionManager()
    seed_row(state_dir, mgr, name="cloude_alpha", epoch=EPOCH_A)
    mgr._hook_tmux_names = {"ses_alpha": "cloude_alpha"}
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(("cloude_alpha", EPOCH_A)),
    )

    # Before: the id is unknown, and the hook path is a 410.
    with pytest.raises(ValueError):
        mgr.record_toast(
            session_id="ses_alpha", kind="Stop", title="t", body="b"
        )

    await readopt_surviving_sessions(mgr)

    toast = mgr.record_toast(
        session_id="ses_alpha", kind="Stop", title="t", body="b"
    )
    assert toast is not None
    # The instance epoch is cached too, so a hook-time activity write can
    # be scoped to the exact instance instead of refusing.
    assert mgr._instance_epochs["ses_alpha"] == EPOCH_A


@pytest.mark.asyncio
async def test_the_current_session_pointer_is_not_decided_by_a_race(
    state_dir, monkeypatch
):
    """Re-adopting N sessions must not steal "current" from the last one.

    ``_register_session`` moves ``_last_session_id``, and these attaches
    finish in whatever order tmux answers. Without the guard, the session
    the user was last in is replaced by whichever pane came back last -
    a different answer on every boot, over identical state.
    """
    mgr = SessionManager()
    for name, epoch in (("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)):
        seed_row(state_dir, mgr, name=name, epoch=epoch)
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(
            ("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)
        ),
    )

    # Stand in for the single-session rehydrate that ran just before us.
    mgr._register_session(
        Session(id="ses_rehydrated", working_dir="/tmp",
                tmux_session="cloude_rehydrated"),
        None,
    )
    assert mgr.current_session().id == "ses_rehydrated"

    await readopt_surviving_sessions(mgr)

    assert mgr.current_session().id == "ses_rehydrated"
    assert len(mgr.sessions) == 3


@pytest.mark.asyncio
async def test_with_no_prior_current_the_pointer_is_deterministic(
    state_dir, monkeypatch
):
    """No rehydrated session means the FIRST target in plan order wins.

    Plan order follows the listing, not attach-completion order, so two
    boots over unchanged state name the same current session.
    """
    mgr = SessionManager()
    for name, epoch in (("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)):
        seed_row(state_dir, mgr, name=name, epoch=epoch)
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(
            ("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)
        ),
    )

    await readopt_surviving_sessions(mgr)

    assert mgr.current_session().id == "adopted:cloude_alpha"


@pytest.mark.asyncio
async def test_a_session_entered_during_the_pass_keeps_the_pointer(
    state_dir, monkeypatch
):
    """A concurrent create outranks the boot-time default.

    This task is NOT awaited by boot: uvicorn has already bound the port
    by the time it runs, so a user can create or enter a session while
    the gather is still in flight. That moves ``_last_session_id`` to a
    session none of these attaches produced. Re-pinning the boot default
    over it would take "current" away from the session the user is
    actually looking at, so the pass only overrules a pointer its OWN
    attaches moved.
    """
    mgr = SessionManager()
    for name, epoch in (("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)):
        seed_row(state_dir, mgr, name=name, epoch=epoch)
    install_backends(
        monkeypatch,
        discover=TmuxListing.answered([]),
        attachable=listing_rows(
            ("cloude_alpha", EPOCH_A), ("cloude_beta", EPOCH_B)
        ),
    )

    # The user's create lands in the window between the LAST attach
    # registering and the pointer being restored below it. Registering
    # the session is exactly what the create path does, and it moves
    # ``_last_session_id``.
    register = mgr._register_session
    registered = []

    def register_then_maybe_user_creates(session, backend):
        register(session, backend)
        registered.append(session.id)
        if len(registered) == 2:
            register(
                Session(
                    id="ses_user_made",
                    working_dir="/tmp",
                    tmux_session="cloude_user_made",
                ),
                None,
            )

    monkeypatch.setattr(
        mgr, "_register_session", register_then_maybe_user_creates
    )

    await readopt_surviving_sessions(mgr)

    assert mgr.current_session().id == "ses_user_made"
