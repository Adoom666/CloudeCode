"""WHICH surviving sessions boot may hold, and what to call each one.

The pure half of the boot re-adopt: no tmux, no sqlite, no event loop,
so the entire decision is testable without a socket or a server. The
pass that acts on it lives in ``src/core/session_boot_readopt.py``.

THE DEFECT THIS CLOSES. ``SessionManager._lifespan_tmux_reconcile`` was
written when one live session was the whole product, and its docstring
still says so: "persist/rehydrate at most ONE session across restarts".
Concurrent sessions became a runtime feature and the boot path never
followed. Measured on the owner's box: 21 live ``cloude_*`` sessions on
the socket, ZERO held after a restart.

WHAT ZERO HELD ACTUALLY COSTS, because "the list is shorter" undersells
it. ``GET /sessions/list`` reads in-memory state only, so an unheld
session is absent from it entirely. The launchpad and sidebar fall back
to painting it from ``GET /sessions/attachable``, whose rows carry no
``status`` at the wrapper level, so the client's ``actionsFor(undefined)``
offers close and nothing else: no activity dot, no unread badge, no
family or wrapper pill, no restart. Worse, the hook endpoint answers
**410 Gone** for a session id it has never heard of, and the agent
running inside that pane cannot retry - 29 hook events were dropped that
way on one boot. And opening one runs the ADOPT path, which mints a
fresh ``adopted:<name>`` id with a new ``created_at``, re-fingerprints
``agent_type`` off scrollback, and mints a hook token the already-running
agent will never read.

THE ID IS THE WHOLE PROBLEM, so it gets a ladder rather than a guess.
``get_env_for_spawn`` injects ``CLOUDECODE_SESSION_ID`` into the pane's
environment at ``new-session`` time, which means the running agent
carries the CREATE-TIME id for the rest of its life and presents it on
every hook. The durable record of that id is the hook-token store, whose
``tmux_names`` map (session_id -> tmux name) is restored at boot by
``SessionManager._load_hook_tokens``. Reversing that map is therefore not
a heuristic - it recovers the exact id the pane is already using, which
is what makes hooks work again with no 410 and no re-mint.

Four rungs, each NAMED in the result so a caller can tell a recovered id
from an invented one:

  ``hook_token``  - reversed out of the hook-token store. The id the
                    running agent actually presents. Preferred, and a
                    non-``adopted:`` id wins any tie because that is the
                    one the pane's environment carries.
  ``legacy_row``  - the row's own ``legacy_session_id``, written by the
                    JSON import path.
  ``derived``     - ``adopted:<name>``. Not recovered, DERIVED, and said
                    out loud. It is deterministic across restarts and
                    identical to what the adopt path would mint, so a
                    later user click re-enters the same session instead
                    of registering a second copy of it.

WHAT IS NEVER CLAIMED. A ``cloude_*`` session with no row stays observed
and adoptable - claiming a stranger silently is the failure mode this
module must not have. A row whose ``tmux_created_epoch`` is NULL keys no
instance (see ``session_store.get_instance``) and is skipped by name. An
origin outside ``created`` / ``adopted`` is left alone.

EVERY LIVE SESSION LANDS SOMEWHERE. A name is either a target or a
NAMED skip; there is no silent ``continue``, because a session that
quietly went missing from a plan is indistinguishable from one the plan
deliberately left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.db_models import (
    SESSION_FAMILY_SOURCE_FINGERPRINT,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
)
from src.models import Session, SessionStatus


#: Origins this module is willing to claim. Mirrors the ownership rule
#: ``session_store.is_owned_origin`` enforces; ``observed`` is deliberately
#: absent, because seeing a session is not owning it.
CLAIMABLE_ORIGINS: Tuple[str, ...] = (
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_ADOPTED,
)

#: Where a re-adopted session's id came from. Never blended: a recovered
#: id keeps hooks working, a derived one only keeps the row addressable.
ID_SOURCE_HOOK_TOKEN = "hook_token"
ID_SOURCE_LEGACY_ROW = "legacy_row"
ID_SOURCE_DERIVED = "derived"

#: Why a live tmux session was left alone. Every one of these is a real
#: answer that belongs in a log, not a silent ``continue``.
SKIP_ALREADY_HELD = "already_held"
SKIP_NO_ROW = "no_row"
SKIP_NOT_CLAIMABLE_ORIGIN = "not_claimable_origin"
SKIP_NO_EPOCH = "no_epoch"

#: Outcome of one whole pass.
READOPT_RAN = "ran"
READOPT_CANNOT_DETERMINE = "cannot_determine"

#: The id shape the adopt path mints, reused here so the derived rung and
#: an actual adoption cannot disagree about what to call one session.
ADOPTED_ID_PREFIX = "adopted:"


def derived_id_for(name: str) -> str:
    """The deterministic fallback id for a tmux session name.

    Description: identical to what ``adopt_external_session`` mints, on
      purpose. A later user click on this session therefore re-enters the
      registration this module made instead of creating a second one.
    Inputs: name (str) - literal tmux session name.
    Output: str.
    Example: derived_id_for('cloude_alpha')  # 'adopted:cloude_alpha'
    """
    return f"{ADOPTED_ID_PREFIX}{name}"


@dataclass(frozen=True)
class ReadoptTarget:
    """One live tmux session that boot has decided to hold, and how.

    Description: everything the attach needs, resolved before any tmux
      call is made, so the concurrent phase does no thinking.
    Inputs (constructor): name (str) - literal tmux session name, used
      verbatim as the backend's ``session_name``. epoch (int) - the
      instance's ``#{session_created}``. session_id (str) - the id to
      register under. id_source (str) - one of the ``ID_SOURCE_*``
      values. working_dir (str). agent_type (str | None) - read off the
      row, never re-fingerprinted. via_fingerprint (bool) - whether that
      agent_type was originally a scrollback guess. origin (str).
    Output: a ReadoptTarget.
    """

    name: str
    epoch: int
    session_id: str
    id_source: str
    working_dir: str
    agent_type: Optional[str] = None
    via_fingerprint: bool = False
    origin: str = SESSION_ORIGIN_CREATED


@dataclass(frozen=True)
class ReadoptPlan:
    """What one pass intends to do, decided with no I/O of its own.

    Inputs (constructor): targets (list[ReadoptTarget]). skipped
      (list[tuple[str, str]]) - ``(tmux name, reason)`` pairs, one per
      live session left alone.
    Output: a ReadoptPlan.
    """

    targets: List[ReadoptTarget] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    def skipped_for(self, reason: str) -> List[str]:
        """Names skipped for one specific reason.

        Inputs: reason (str) - one of the ``SKIP_*`` values.
        Output: list[str] - tmux names, in plan order.
        """
        return [name for name, why in self.skipped if why == reason]


@dataclass(frozen=True)
class ReadoptReport:
    """What one pass actually did, including the failures.

    Description: ``outcome`` carries the third answer. ``ran`` means the
      datastore was read and the plan below is a measurement; on
      ``cannot_determine`` every list is empty because nothing was
      decided, which is NOT the same as deciding to hold nothing.
    Inputs (constructor): outcome (str) - ``READOPT_RAN`` or
      ``READOPT_CANNOT_DETERMINE``. held (list[str]) - session ids now
      registered. failed (list[tuple[str, str]]) - ``(tmux name, error)``
      for attaches that raised. plan (ReadoptPlan | None). detail
      (str | None) - why the pass could not run.
    Output: a ReadoptReport.
    """

    outcome: str
    held: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    plan: Optional[ReadoptPlan] = None
    detail: Optional[str] = None


def resolve_session_id(
    name: str,
    row: Dict[str, Any],
    hook_names: Dict[str, str],
) -> Tuple[str, str]:
    """Recover the id this tmux session is already known by, or derive one.

    Description: the ladder in the module docstring. Rung 1 reverses the
      hook-token store, which is the only durable record of the id
      injected into the pane's own environment - so it is a RECOVERY, not
      a guess. When two ids map to one name (a session created by the app
      and later re-adopted mints a token under each), the non-``adopted:``
      id wins, because that is the create-time id the running agent
      carries. Ties beyond that are broken lexicographically so two boots
      over unchanged state cannot disagree.
    Inputs: name (str) - tmux session name. row (dict) - the sessions
      row for this instance. hook_names (dict[str, str]) - session_id ->
      tmux name, as restored by ``_load_hook_tokens``.
    Output: tuple[str, str] - ``(session_id, id_source)``.
    Example: resolve_session_id('cloude_a', row, {'ses_1': 'cloude_a'})
             # ('ses_1', 'hook_token')
    """
    candidates = sorted(
        sid for sid, mapped in hook_names.items() if mapped == name and sid
    )
    if candidates:
        preferred = [
            sid for sid in candidates if not sid.startswith(ADOPTED_ID_PREFIX)
        ]
        return (preferred or candidates)[0], ID_SOURCE_HOOK_TOKEN

    legacy = row.get("legacy_session_id")
    if legacy:
        return str(legacy), ID_SOURCE_LEGACY_ROW

    return derived_id_for(name), ID_SOURCE_DERIVED


def plan_readopt(
    *,
    listing_rows: Sequence[Dict[str, Any]],
    row_lookup: Callable[[str, Optional[int]], Optional[Dict[str, Any]]],
    hook_names: Dict[str, str],
    held_ids: Sequence[str],
    held_names: Sequence[str],
    default_working_dir: str,
) -> ReadoptPlan:
    """Decide which live sessions to hold, without touching tmux or disk.

    Description: pure, so the whole decision is testable without a socket
      or a server. Every live row lands in exactly one of ``targets`` or
      ``skipped``; nothing is dropped on the floor.
    Inputs: listing_rows (sequence[dict]) - rows from a SUCCESSFUL tmux
      listing, each with ``name`` and ``created_at_epoch``. row_lookup
      (callable) - ``(name, epoch) -> sessions row | None``; the exact
      instance-triple read, so a reused name cannot match a dead
      instance. hook_names (dict[str, str]). held_ids (sequence[str]) -
      session ids already registered this boot. held_names
      (sequence[str]) - tmux names already bound to a live backend.
      default_working_dir (str) - used when neither the row nor the
      listing names a directory.
    Output: ReadoptPlan.
    Example: plan_readopt(listing_rows=rows, row_lookup=f, hook_names={},
                          held_ids=[], held_names=[],
                          default_working_dir='/tmp').targets
    """
    held_id_set = set(held_ids)
    held_name_set = set(held_names)
    targets: List[ReadoptTarget] = []
    skipped: List[Tuple[str, str]] = []

    for listing_row in listing_rows:
        name = listing_row.get("name")
        if not name:
            continue
        if name in held_name_set:
            skipped.append((name, SKIP_ALREADY_HELD))
            continue

        raw_epoch = listing_row.get("created_at_epoch")
        try:
            epoch = int(raw_epoch)
        except (TypeError, ValueError):
            # No epoch means no instance triple, so no row can be shown
            # to describe THIS process. Named, not silently passed over.
            skipped.append((name, SKIP_NO_EPOCH))
            continue

        row = row_lookup(name, epoch)
        if row is None:
            skipped.append((name, SKIP_NO_ROW))
            continue

        origin = str(row.get("origin") or "")
        if origin not in CLAIMABLE_ORIGINS:
            skipped.append((name, SKIP_NOT_CLAIMABLE_ORIGIN))
            continue

        session_id, id_source = resolve_session_id(name, row, hook_names)
        if session_id in held_id_set:
            skipped.append((name, SKIP_ALREADY_HELD))
            continue

        working_dir = (
            row.get("working_dir")
            or listing_row.get("working_dir")
            or default_working_dir
        )
        targets.append(
            ReadoptTarget(
                name=name,
                epoch=epoch,
                session_id=session_id,
                id_source=id_source,
                working_dir=str(working_dir),
                agent_type=row.get("agent_type"),
                via_fingerprint=(
                    row.get("agent_family_source")
                    == SESSION_FAMILY_SOURCE_FINGERPRINT
                ),
                origin=origin,
            )
        )
        # Two live sessions cannot resolve to one id within a single pass.
        held_id_set.add(session_id)
        held_name_set.add(name)

    return ReadoptPlan(targets=targets, skipped=skipped)


def session_for(target: ReadoptTarget) -> Session:
    """Build the in-memory Session record for one re-adopt target.

    Description: every field comes from stored or measured state. In
      particular ``created_at`` is derived from tmux's own
      ``#{session_created}`` - the instance's actual birth - rather than
      minted at ``utcnow()``, which is what makes a re-adopted session
      keep its age across a restart instead of appearing brand new.
    Inputs: target (ReadoptTarget).
    Output: Session with status RUNNING.
    Example: session_for(target).created_at
    """
    born = datetime.fromtimestamp(target.epoch, tz=timezone.utc).replace(
        tzinfo=None
    )
    return Session(
        id=target.session_id,
        working_dir=target.working_dir,
        status=SessionStatus.RUNNING,
        created_at=born,
        last_activity=born,
        agent_type=target.agent_type,
        agent_type_via_fingerprint=target.via_fingerprint,
        tmux_session=target.name,
    )


