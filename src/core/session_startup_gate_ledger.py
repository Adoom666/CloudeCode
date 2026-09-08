"""Per-instance bookkeeping and the one tmux read the startup gate needs.

Split out of :mod:`src.core.session_startup_gate` so that module stays
PURE - a ladder that can be tested without a tmux server and without
mutable state. Everything here is one or the other: the ledger is state,
``capture_pane_tail`` is I/O. See that module's docstring for what the
gate is and for the measurements behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.session_startup_gate import STARTUP_TAIL_LINES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-instance bookkeeping: when the first hook landed, and whether this
# instance has already been toasted about.
# ---------------------------------------------------------------------------


@dataclass
class _InstanceRecord:
    """One tmux name's current instance and what has happened to it.

    ``epoch`` and ``pane_pid`` together are how a NEW instance under an
    OLD name is recognised - see ``StartupGateLedger.observe_instance``.
    """

    epoch: Optional[int] = None
    pane_pid: Optional[int] = None
    first_hook_at: Optional[datetime] = None
    toasted: bool = False


class StartupGateLedger:
    """First-hook time and toast-once state, per tmux instance.

    WHY NOT REUSE ``SessionActivityTracker.hooks_seen``. That tracker is
    keyed by cloudecode ``session_id`` and answers "has this session ever
    fired a hook this server run". Neither half survives what this gate
    needs to survive. ``POST /sessions/respawn`` with
    ``live_restart_confirmed`` kills the pane's PROCESS and respawns it in
    the SAME pane, so the session_id does not change and, measured on tmux
    3.7c (CLAUDE.md, 2026-09-07), neither does ``#{session_created}``. A
    session restarted onto a wrapper that now hits the trust dialog would
    therefore inherit the old process's ``hook_seen`` and read ``ready``
    forever.

    WHAT ACTUALLY MOVES ACROSS THAT RESTART IS THE PANE PID, and the same
    measurement is where that is recorded ("same epoch, same pane_id, new
    pane_pid"). So an instance here is the pair (epoch, pane_pid): the
    epoch catches a brand-new tmux session reusing a name, the pid catches
    a respawn inside an existing one. When either is observed to move,
    the record is dropped whole - first-hook time AND the toast claim -
    because the new process has proved nothing yet.

    Both fields are NULLABLE and a null never triggers a reset. The hook
    endpoint knows the tmux name and sometimes the epoch, and never the
    pid; the listing pass knows all three. So the pid is stamped by the
    first listing pass after the hook, and an unknown value is adopted
    rather than treated as a change. Treating "not measured" as "moved"
    would reset the ledger on every poll and re-toast forever, which is
    precisely the failure the toast-once claim exists to prevent.
    """

    def __init__(self) -> None:
        """Start with an empty ledger.

        Description: process-local state only, keyed by tmux name. There
            is nothing to load: a restart of this app has not restarted
            the panes, and a record re-earns itself on the next hook.
        Inputs: none.
        Output: None.
        Example: ledger = StartupGateLedger()
        """
        self._records: dict[str, _InstanceRecord] = {}

    def record_hook(
        self,
        tmux_name: Optional[str],
        *,
        epoch: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Note that a hook event landed for ``tmux_name``.

        Description: FIRST WRITE WINS - the field is "when did startup
            finish", so a later event must not push the timestamp
            forward. Idempotent by construction, which it has to be: hook
            events are duplicated and re-delivered (CLAUDE.md), and the
            same ``SessionStart`` arriving twice must leave the ledger
            byte-identical.
        Inputs:
            tmux_name: the tmux session name; a falsy name is a no-op
                (the hook endpoint cannot always resolve one).
            epoch: ``#{session_created}`` if the caller knows it. None is
                fine; the listing pass fills it in.
            now: injectable clock. Defaults to ``datetime.utcnow()``.
        Output: None.
        Example:
            >>> StartupGateLedger().record_hook("cloude_a")
        """
        if not tmux_name:
            return
        record = self._records.setdefault(tmux_name, _InstanceRecord())
        if epoch is not None:
            if record.epoch is not None and record.epoch != int(epoch):
                record = _InstanceRecord()
                self._records[tmux_name] = record
            record.epoch = int(epoch)
        if record.first_hook_at is None:
            record.first_hook_at = now or datetime.utcnow()

    def observe_instance(
        self,
        tmux_name: Optional[str],
        *,
        epoch: Optional[int],
        pane_pid: Optional[int],
    ) -> None:
        """Reconcile the ledger against the instance tmux reports NOW.

        Description: called once per listing pass with the live epoch and
            pane pid. Drops the whole record when either has been
            MEASURED to move (both the stored and the observed value
            known, and different) - that is a new process and it has
            fired no hook yet. Adopts an unknown stored value from the
            observation rather than treating it as a change.
        Inputs:
            tmux_name: tmux session name; falsy is a no-op.
            epoch: live ``#{session_created}``, or None if unreadable.
            pane_pid: live ``#{pane_pid}``, or None if unreadable.
        Output: None.
        Example:
            >>> led = StartupGateLedger()
            >>> led.record_hook("a")
            >>> led.observe_instance("a", epoch=7, pane_pid=100)
            >>> led.observe_instance("a", epoch=7, pane_pid=200)
            >>> led.first_hook_at("a") is None
            True
        """
        if not tmux_name:
            return
        record = self._records.get(tmux_name)
        if record is None:
            return
        moved = (
            epoch is not None
            and record.epoch is not None
            and int(epoch) != record.epoch
        ) or (
            pane_pid is not None
            and record.pane_pid is not None
            and int(pane_pid) != record.pane_pid
        )
        if moved:
            self._records[tmux_name] = _InstanceRecord(
                epoch=int(epoch) if epoch is not None else None,
                pane_pid=int(pane_pid) if pane_pid is not None else None,
            )
            return
        if epoch is not None:
            record.epoch = int(epoch)
        if pane_pid is not None:
            record.pane_pid = int(pane_pid)

    def first_hook_at(self, tmux_name: Optional[str]) -> Optional[datetime]:
        """When the first hook for the CURRENT instance landed, or None.

        Inputs: tmux_name (str | None).
        Output: datetime | None. None means no hook has been recorded for
            the instance currently under this name - which includes the
            case where a hook WAS recorded for a previous instance and
            ``observe_instance`` has since dropped it.
        """
        if not tmux_name:
            return None
        record = self._records.get(tmux_name)
        return record.first_hook_at if record else None

    def claim_toast(self, tmux_name: Optional[str]) -> bool:
        """Claim the one toast this instance is allowed, exactly once.

        Description: the idempotence guarantee behind "toast once per
            instance". Detection runs on every listing poll and will
            keep answering ``awaiting_startup_prompt`` for as long as the
            user leaves the prompt unanswered, so the toast has to be
            claimed rather than fired. Returns True for the FIRST caller
            and False for every caller after, until ``observe_instance``
            sees the instance move or ``forget`` is called.
        Inputs: tmux_name (str | None). A falsy name never claims.
        Output: bool - True exactly once per instance.
        Example:
            >>> led = StartupGateLedger()
            >>> led.claim_toast("a"), led.claim_toast("a")
            (True, False)
        """
        if not tmux_name:
            return False
        record = self._records.setdefault(tmux_name, _InstanceRecord())
        if record.toasted:
            return False
        record.toasted = True
        return True

    def forget(self, tmux_name: Optional[str]) -> None:
        """Drop all state for ``tmux_name``. Idempotent.

        Inputs: tmux_name (str | None).
        Output: None.
        """
        if tmux_name:
            self._records.pop(tmux_name, None)


# ---------------------------------------------------------------------------
# The one tmux read this module needs, and the copy that goes on screen.
# ---------------------------------------------------------------------------


def capture_pane_tail(
    *, socket: str, name: str, lines: int = STARTUP_TAIL_LINES
) -> Optional[str]:
    """Read a pane's recent scrollback as text, or None if it could not be.

    Description: one ``tmux capture-pane`` through a bare
        ``TmuxBackend.for_external`` - no attach, no pipe-pane, nothing
        kept afterwards. This is the same cheap probe
        ``SessionManager._detect_agent_type_from_pane`` uses, extracted
        here so the capture and the thing that reads it are not written
        twice.

        NEVER RAISES, and returns None rather than "" on failure. A
        listing poll must not fail because one pane could not be read,
        and "" would be handed to ``detect_startup_prompt`` as text that
        was successfully read and found empty - which would answer
        ``ready`` for a pane nobody actually managed to look at.
    Inputs:
        socket: tmux socket name (always this app's own socket).
        name: tmux session name.
        lines: how many lines of scrollback to ask for.
    Output:
        str | None - decoded pane text, or None when the capture failed
        or produced nothing.
    Example:
        capture_pane_tail(socket='cloude', name='cloude_a')
    """
    from src.core.tmux_backend import TmuxBackend

    try:
        probe = TmuxBackend.for_external(
            session_name=name,
            working_dir=Path.home(),
            socket_name=socket,
        )
        raw = probe.capture_scrollback(lines=lines)
    # DELIBERATELY BROAD, and inherited verbatim from the fingerprint
    # probe this replaced. A pane read is a best-effort side question
    # asked while building the home screen: a dead pane, a tmux server
    # that went away, an unsafe name, a decode fault - none of them is a
    # reason to fail the session list. Narrowing it to the two errors
    # currently known to escape would let the next new one take the
    # listing down. The swallow is logged with context and answers None,
    # which the callers already treat as "could not determine".
    except Exception as exc:  # noqa: BLE001 - a probe must never crash listing
        logger.debug(
            "startup_gate_capture_failed",
            session=name,
            socket=socket,
            error=str(exc),
        )
        return None
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace")
