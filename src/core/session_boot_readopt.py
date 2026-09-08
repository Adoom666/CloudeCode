"""Running the boot re-adopt: read the rows, attach the panes, hold them.

The impure half. ``src/core/session_boot_readopt_plan.py`` decides WHICH
live sessions are ours and what each one is called; this module does the
I/O that decision implies, and nothing else. Its vocabulary
(``ID_SOURCE_*``, ``SKIP_*``, ``ReadoptTarget``) is re-exported here so a
caller has ONE import.

TWO PROPERTIES THIS PASS MUST HAVE, and both are easy to lose.

IT NEVER RUNS ON AN UNANSWERED PROBE. ``listing.ok`` is checked upstream
in ``SessionManager._lifespan_tmux_reconcile``, which stashes its listing
only past that gate, and this pass gates its OWN listing again. A listing
that did not run carries no names, and reading that as an empty socket
would disown every session on the box.

WHY IT TAKES ITS OWN LISTING RATHER THAN REUSING BOOT'S, which is the
non-obvious part and cost real time to find. ``discover_existing()`` asks
tmux for ``#{session_name}`` and NOTHING ELSE, so its rows are bare
strings with no ``#{session_created}``. An epoch is not a nice-to-have
here: with no epoch there is no instance triple, ``session_store.
get_instance`` returns None BY CONTRACT, and every single live session
would be skipped as "no row" while the pass reported a clean run.
``list_attachable_sessions`` is the listing that carries the epoch, and
paying for one extra tmux call is free on this path precisely because
the path is off the critical one.

IT IS NEVER AWAITED BY BOOT. uvicorn binds the listening socket only once
the lifespan startup coroutine reaches its ``yield``, so an awaited pass
is paid before the port answers at all. See ``schedule_boot_readopt``.

IT WRITES NOTHING DURABLE. Rows are read; registration is in memory. No
column is updated and no hook token is minted - a token minted here could
never reach the already-running agent, so it would be noise dressed as a
fix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List, Optional, Tuple

import structlog

from src.core.session_boot_readopt_plan import (
    ADOPTED_ID_PREFIX,
    CLAIMABLE_ORIGINS,
    ID_SOURCE_DERIVED,
    ID_SOURCE_HOOK_TOKEN,
    ID_SOURCE_LEGACY_ROW,
    READOPT_CANNOT_DETERMINE,
    READOPT_RAN,
    SKIP_ALREADY_HELD,
    SKIP_NO_EPOCH,
    SKIP_NO_ROW,
    SKIP_NOT_CLAIMABLE_ORIGIN,
    ReadoptPlan,
    ReadoptReport,
    ReadoptTarget,
    derived_id_for,
    plan_readopt,
    resolve_session_id,
    session_for,
)

logger = structlog.get_logger()

__all__ = [
    "ADOPTED_ID_PREFIX",
    "CLAIMABLE_ORIGINS",
    "ID_SOURCE_DERIVED",
    "ID_SOURCE_HOOK_TOKEN",
    "ID_SOURCE_LEGACY_ROW",
    "READOPT_CANNOT_DETERMINE",
    "READOPT_RAN",
    "SKIP_ALREADY_HELD",
    "SKIP_NO_EPOCH",
    "SKIP_NO_ROW",
    "SKIP_NOT_CLAIMABLE_ORIGIN",
    "ReadoptPlan",
    "ReadoptReport",
    "ReadoptTarget",
    "derived_id_for",
    "plan_readopt",
    "readopt_surviving_sessions",
    "resolve_session_id",
    "schedule_boot_readopt",
    "session_for",
]


def probe_instances(manager: Any) -> Any:
    """Take a listing that carries each session's creation epoch.

    Description: the identity-bearing probe. ``discover_existing`` names
      sessions and stops there, and a name is not an identity - see this
      module's docstring for what reusing it would silently do. One
      ``list-sessions`` call against a fresh probe backend, the same way
      ``SessionManager.list_attachable_sessions`` does it. No ownership
      hints are passed because this pass answers ownership from the row's
      ``origin``, not from the listing's badge.
    Inputs: manager (SessionManager) - unused except to keep the call
      shape uniform and mockable.
    Output: TmuxListing - ``ok=False`` propagated verbatim.
    Example: probe_instances(mgr).ok
    """
    from src.config import settings
    from src.core.session_backend import build_backend
    from src.core.tmux_listing import coerce_listing

    probe = build_backend(
        settings,
        session_id="__readopt_probe__",
        working_dir=Path.home(),
        on_output=None,
    )
    lister = getattr(probe, "list_attachable_sessions", None)
    if lister is None:
        # A PTY backend has no sessions to enumerate. Not an error and
        # not zero-with-confidence: there is nothing here to re-adopt.
        from src.core.tmux_listing import TmuxListing

        return TmuxListing.answered([])
    return coerce_listing(lister())


def schedule_boot_readopt(
    manager: Any, listing: Any = None
) -> Optional["asyncio.Task"]:
    """Start the re-adopt pass WITHOUT making boot wait for it.

    Description: uvicorn binds the listening socket only once the lifespan
      startup coroutine reaches its ``yield``, so anything AWAITED during
      startup is paid before the port answers at all. Twenty-one attaches
      at three or four tmux round trips each is one to three seconds of
      dead port, and ``asyncio.gather`` alone does not fix that - a
      gather awaited inside startup still blocks the bind, just for less
      wall time. Handing the work to a task is what moves it off the
      critical path: startup returns, the port binds, and the attaches
      interleave with the rest of boot and with the first requests.

      The task reference is HELD on the manager. A bare ``create_task``
      whose result nobody keeps can be garbage collected mid-flight, and
      the sessions would then be silently half-attached.
    Inputs: manager (SessionManager). listing (TmuxListing | None) - an
      epoch-bearing listing to use instead of probing for one; None (the
      normal case) makes the task take its own.
    Output: asyncio.Task | None - None when there is no running loop to
      schedule on, which is the case in a synchronous unit test and is a
      reason to skip, not to fail.
    Example: task = schedule_boot_readopt(mgr); await task
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("boot_readopt_not_scheduled", reason="no_running_loop")
        return None

    task = loop.create_task(readopt_surviving_sessions(manager, listing))
    manager._boot_readopt_task = task
    return task


async def readopt_surviving_sessions(
    manager: Any, listing: Any = None
) -> ReadoptReport:
    """Hold every surviving session this app owns, concurrently.

    Description: the impure half. Takes an epoch-bearing listing (see
      :func:`probe_instances`), reads the ``sessions`` table over ONE
      connection - one indexed exact-triple SELECT per live session, via
      the canonical ``session_store.get_instance``, so no second lookup
      shape is invented here - plans, then attaches with
      ``asyncio.gather`` so one dead pane cannot cancel the other
      twenty. Every attach failure is logged and counted, never raised.

      A listing that did not run yields ``cannot_determine`` and holds
      nothing. An unanswered probe carries no rows, and treating that as
      an empty socket is the bug the three-outcome gate exists to
      prevent.

      Idempotent: a second pass over unchanged state finds every session
      already held and holds nothing again.
    Inputs: manager (SessionManager). listing (TmuxListing | None) - an
      epoch-bearing listing, or None to take one.
    Output: ReadoptReport.
    Example: (await readopt_surviving_sessions(mgr)).held
    """
    from src.config import settings
    from src.core.session_backend import build_backend
    from src.core.session_store import get_instance, sessions_table_ready

    if listing is None:
        listing = probe_instances(manager)
    if not listing.ok:
        logger.warning(
            "boot_readopt_listing_unavailable",
            reason=getattr(listing, "reason", None),
            note=(
                "tmux could not be enumerated, so nothing is held and "
                "nothing is disowned - not zero sessions, no answer"
            ),
        )
        return ReadoptReport(
            outcome=READOPT_CANNOT_DETERMINE,
            detail=f"listing unavailable ({getattr(listing, 'reason', None)})",
        )

    conn = None
    try:
        conn = manager._datastore_connection()
        if conn is None or not sessions_table_ready(conn):
            # NOT AN EMPTY ANSWER. Nothing is held and nothing is
            # claimed, because we could not find out what is ours.
            logger.warning(
                "boot_readopt_cannot_determine",
                live_count=len(listing.names),
                note=(
                    "the sessions table could not be read, so no live "
                    "session could be shown to be ours - holding none "
                    "rather than claiming or disowning any"
                ),
            )
            return ReadoptReport(
                outcome=READOPT_CANNOT_DETERMINE,
                detail="sessions table unavailable",
            )

        socket = manager._tmux_socket_name()
        plan = plan_readopt(
            listing_rows=listing.sessions,
            row_lookup=lambda name, epoch: get_instance(
                conn, socket=socket, name=name, epoch=epoch
            ),
            hook_names=dict(getattr(manager, "_hook_tmux_names", {}) or {}),
            held_ids=list(manager.sessions.keys()),
            held_names=[
                getattr(backend, "tmux_session", None)
                for backend in manager.backends.values()
            ],
            default_working_dir=str(settings.get_working_dir()),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - a close failure is not a verdict
                pass

    if not plan.targets:
        logger.info(
            "boot_readopt_nothing_to_hold",
            live_count=len(listing.names),
            skipped=len(plan.skipped),
        )
        return ReadoptReport(outcome=READOPT_RAN, plan=plan)

    async def _attach(target: ReadoptTarget) -> None:
        """Attach one target and register it. Raises on tmux refusal."""
        backend = build_backend(
            settings,
            session_id=target.session_id,
            working_dir=Path(target.working_dir),
            on_output=manager._make_output_handler(target.session_id),
            # THE STORED NAME, VERBATIM. Deriving ``cloude_<slug(id)>``
            # here would rebuild the double-prefix bug this change fixes
            # on the single-session path.
            session_name=target.name,
        )
        # ``needs_pipe_setup=True`` buys three things at once: a dead
        # pane is REFUSED rather than held as a corpse, pipe-pane is
        # ensured without clobbering one already running, and
        # remain-on-exit is set so a child exiting cannot silently
        # collapse the pane out from under us.
        await backend.attach_existing(needs_pipe_setup=True)

        session = session_for(target)
        session.pinned_theme = manager.resolve_project_theme(
            Path(target.working_dir), target.name
        )
        manager._register_session(session, backend)
        # Scope any later hook write to the exact instance, the same way
        # the create and adopt paths do.
        manager._instance_epochs[target.session_id] = target.epoch
        # Make the id -> name association explicit even on the derived
        # rung, so a hook that does arrive can be resolved to a session.
        manager._hook_tmux_names.setdefault(target.session_id, target.name)

        # PUSH THE CURRENT CONTROL VARIABLES ONTO THE PANE'S SESSION
        # ENVIRONMENT. It does NOT reach the agent already running in
        # there - tmux copies the environment at spawn and a live process
        # keeps what it was born with - and that is precisely why it is
        # worth doing: the NEXT process in that pane (a restart, or a
        # claude the user launches by hand) is the first one that can be
        # handed a current ``CLOUDECODE_SESSION_ID`` and
        # ``CLOUDECODE_HOOK_TOKEN``, and without this it would inherit
        # whatever stale pair the pane has carried since it was born and
        # 403 on every hook it sends. Nothing is minted: this id was
        # RECOVERED, so ``get_env_for_spawn`` returns the token already
        # held for it.
        #
        # BEST EFFORT. A boot pass that failed over a set-environment
        # refusal would hold zero sessions for the sake of an
        # optimisation, which is the wrong trade in the direction this
        # module has already paid for once.
        try:
            for var, val in manager.get_env_for_spawn(
                target.session_id
            ).items():
                await backend._run_tmux(
                    "set-environment", "-t", target.name, var, val,
                    check=False,
                )
        except (OSError, AttributeError) as exc:
            logger.debug(
                "boot_readopt_set_environment_failed",
                session=target.name,
                error=str(exc),
            )

    # WHICH SESSION IS "CURRENT" MUST NOT BE DECIDED BY A RACE.
    # ``_register_session`` moves ``_last_session_id``, and these attaches
    # finish in whatever order tmux answers, so without this the session
    # the user was last in would be replaced by whichever pane happened to
    # come back last. The rehydrated metadata session (set before this
    # pass) keeps the pointer; with none set, the FIRST target in plan
    # order takes it, so two boots over unchanged state agree.
    prior_current = manager._last_session_id

    results = await asyncio.gather(
        *(_attach(target) for target in plan.targets),
        return_exceptions=True,
    )

    held: List[str] = []
    failed: List[Tuple[str, str]] = []
    for target, result in zip(plan.targets, results):
        if isinstance(result, BaseException):
            failed.append((target.name, str(result)))
            logger.warning(
                "boot_readopt_attach_failed",
                tmux_name=target.name,
                session_id=target.session_id,
                error=str(result),
            )
            continue
        held.append(target.session_id)

    # ONLY THIS PASS'S OWN ATTACHES MAY BE OVERRULED. This task is not
    # awaited by boot, so the port is already bound while the gather is in
    # flight and a user can create or enter a session inside that window.
    # That moves the pointer to something none of these attaches produced,
    # and it is a choice made by somebody who is here NOW - it outranks a
    # boot-time default. Re-pinning it regardless would take "current"
    # away from the session the user is looking at.
    if manager._last_session_id in set(held):
        if prior_current is not None and prior_current in manager.sessions:
            manager._last_session_id = prior_current
        elif held:
            manager._last_session_id = held[0]

    logger.info(
        "boot_readopt_complete",
        held=len(held),
        failed=len(failed),
        skipped=len(plan.skipped),
        live_count=len(listing.names),
        id_sources={
            source: sum(
                1
                for t in plan.targets
                if t.id_source == source and t.session_id in held
            )
            for source in (
                ID_SOURCE_HOOK_TOKEN,
                ID_SOURCE_LEGACY_ROW,
                ID_SOURCE_DERIVED,
            )
        },
        no_row=len(plan.skipped_for(SKIP_NO_ROW)),
    )
    return ReadoptReport(
        outcome=READOPT_RAN, held=held, failed=failed, plan=plan
    )
