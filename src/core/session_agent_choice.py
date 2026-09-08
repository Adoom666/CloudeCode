"""Picking a DIFFERENT wrapper when a session is restarted, and remembering it.

WHAT THIS EXISTS TO ELIMINATE. Moving a session onto another launch
wrapper - ``claude-chrome``, say - used to require a hand edit of
``sessions.agent_type`` in ``cloude.db``. That is the operation this
module makes a supported one: the restart request may carry an
``agent_type``, it is checked against the wrappers that are actually
configured, and once a restart with it has been VERIFIED running the
choice is written back so the next restart remembers it.

THE VALIDATION REFUSES; IT NEVER FALLS BACK. ``Settings.get_agent_command``
is deliberately forgiving - an unrecognised ``agent_type`` resolves to
the claude family's default wrapper, which is right for a launch path
that must not fail on a stale label. It is exactly wrong here. A user who
picks ``claude-chrome`` and silently gets ``claude-skip-permissions`` has
been lied to by a picker, which is worse than having no picker. So the id
is validated FIRST, against the same ``agents.wrappers`` list the picker
was drawn from, and an unknown id is refused rather than resolved.

THREE OUTCOMES, and the third is not the second.

  ``CHOICE_ACCEPTED``      the id names a configured wrapper. Its
                           resolved command is on the result.
  ``CHOICE_UNKNOWN``       the wrapper list was READ and this id is not
                           in it. A definite negative; the caller returns
                           400 and names the ids that do exist.
  ``CHOICE_CANNOT_DETERMINE``
                           the config could not be read, or resolving the
                           command raised. We do not know whether the id
                           is valid, so we do not say it is invalid and we
                           do not launch it. The caller refuses, and says
                           it could not check rather than that the choice
                           was wrong.

WHY PERSISTENCE WAITS FOR A VERIFIED START. ``persist_agent_type`` is
called only after the respawn came back with a process CONFIRMED alive in
the pane. A choice that was tried and died on arrival is not what the
session is running, and recording it would make the next restart
re-derive a command that has already been observed to fail, with nothing
on screen saying so. The user's second attempt re-picks, which costs one
click and keeps the column meaning what it says: this is what launched.

KEYED ON THE INSTANCE, NEVER ON THE NAME ALONE. tmux names are re-minted
by this app, so a name-only UPDATE can write onto a DEAD session's row.
The row is located through ``session_store.identity_for_live_name``,
which takes the newest instance for the name on this socket - correct
here by construction, because the session being restarted is the live
tmux session carrying that name (its pane died; the session did not).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional

import structlog

logger = structlog.get_logger()

#: The id names a configured wrapper and its command resolved.
CHOICE_ACCEPTED: str = "accepted"

#: The wrapper list was read and does not contain this id.
CHOICE_UNKNOWN: str = "unknown"

#: We could not check. Never reported as an invalid id.
CHOICE_CANNOT_DETERMINE: str = "cannot_determine"

#: Every verdict :func:`validate_agent_choice` can return.
ALL_CHOICE_VERDICTS: frozenset = frozenset(
    {CHOICE_ACCEPTED, CHOICE_UNKNOWN, CHOICE_CANNOT_DETERMINE}
)


@dataclass(frozen=True)
class AgentChoice:
    """The verdict on one requested ``agent_type``, and what it resolves to.

    Attributes:
        verdict: One of the ``CHOICE_*`` constants.
        agent_type: The id as requested, stripped. Carried on every
            verdict so a refusal can quote it back.
        command: Shell string the wrapper renders to. Only ever set on
            ``CHOICE_ACCEPTED``.
        available: Ids that ARE configured, for a refusal message. Empty
            on ``CHOICE_CANNOT_DETERMINE``, because on that path the list
            is precisely what could not be read.
        detail: One sentence fit to show the user verbatim.
    """

    verdict: str
    agent_type: str = ""
    command: Optional[str] = None
    available: tuple = ()
    detail: str = ""

    @property
    def accepted(self) -> bool:
        """True iff the caller may launch ``command``.

        Output:
            bool: True only for ``CHOICE_ACCEPTED``.
        """
        return self.verdict == CHOICE_ACCEPTED


def configured_wrapper_ids(settings_obj) -> Optional[List[str]]:
    """Ids of every launch wrapper this install has configured.

    Description: the ONE read of the wrapper list, so the picker, the
        validator and the preview cannot disagree about what exists. It
        goes through ``Settings.load_auth_config`` rather than reading
        ``config.json``, because that is the cached, in-memory view the
        running server actually launches from - a hand edit of the file
        is NOT visible to it (see HANDOFF section 5), and a second reader
        that saw the file would offer choices the launcher would not
        honour.

    Inputs:
        settings_obj: the app ``Settings`` instance.

    Output:
        Optional[List[str]]: ids in config order, or None when the config
            could not be read at all. An EMPTY LIST is a real answer
            ("this install has no wrappers"); None is the absence of one.

    Example:
        >>> configured_wrapper_ids(settings)
        ['claude-skip-permissions', 'claude-chrome', 'cldl']
    """
    try:
        agents = settings_obj.load_auth_config().agents
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("agent_choice_config_unreadable", error=str(exc))
        return None
    wrappers = getattr(agents, "wrappers", None)
    if wrappers is None:
        return None
    return [str(w.id) for w in wrappers]


def validate_agent_choice(
    settings_obj,
    agent_type: Optional[str],
    *,
    model: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> AgentChoice:
    """Check a requested wrapper id and resolve what it would run.

    Description: refuses an id that is not configured instead of letting
        ``get_agent_command`` fall back to the default wrapper. See the
        module docstring - the forgiving fallback is right for a launch
        and wrong for a choice.

    Inputs:
        settings_obj: the app ``Settings`` instance.
        agent_type: the wrapper id the user picked. Empty or None is a
            programming error here; callers must not call this when no
            choice was made.
        model: model id to render into the wrapper, when it takes one.
        extra_args: further arguments appended to the wrapped CLI, each
            shlex-quoted at every boundary by ``get_agent_command``. The
            restart path passes ``['--resume', '<uuid>']`` here so a
            picked wrapper comes back on the SAME conversation - see
            ``src/core/session_resume_target.py``. It must be the value
            ``resolve_wrapper_offers`` was given for the same session, or
            the preview and the action render different commands.

    Output:
        AgentChoice: verdict, the id as asked, the resolved command on
            acceptance, and a sentence.

    Example:
        >>> validate_agent_choice(settings, "claude-chrome").verdict
        'accepted'
    """
    asked = (agent_type or "").strip()
    if not asked:
        return AgentChoice(
            verdict=CHOICE_CANNOT_DETERMINE,
            detail="no agent was named, so there was nothing to check",
        )

    ids = configured_wrapper_ids(settings_obj)
    if ids is None:
        return AgentChoice(
            verdict=CHOICE_CANNOT_DETERMINE,
            agent_type=asked,
            detail=(
                "the launch wrapper list could not be read, so whether "
                f"'{asked}' is configured cannot be determined"
            ),
        )

    if asked not in ids:
        return AgentChoice(
            verdict=CHOICE_UNKNOWN,
            agent_type=asked,
            available=tuple(ids),
            detail=(
                f"'{asked}' is not a configured launch wrapper; this "
                f"install has {', '.join(ids) if ids else 'none'}"
            ),
        )

    try:
        command = settings_obj.get_agent_command(
            asked, model=model, extra_args=extra_args
        )
    except (ValueError, OSError, AttributeError) as exc:
        # A wrapper that needs a model and was given none raises here.
        # That is a real refusal with a usable sentence, but it is not
        # "unknown id" - we could not build the command, so we cannot say
        # what would run.
        logger.warning(
            "agent_choice_command_unresolved", agent_type=asked, error=str(exc)
        )
        return AgentChoice(
            verdict=CHOICE_CANNOT_DETERMINE,
            agent_type=asked,
            available=tuple(ids),
            detail=f"'{asked}' could not be turned into a command: {exc}",
        )

    if not (command or "").strip():
        return AgentChoice(
            verdict=CHOICE_CANNOT_DETERMINE,
            agent_type=asked,
            available=tuple(ids),
            detail=f"'{asked}' resolved to an empty command, so it was not run",
        )

    return AgentChoice(
        verdict=CHOICE_ACCEPTED,
        agent_type=asked,
        command=command,
        available=tuple(ids),
        detail=f"restarting with {asked}",
    )


def resolve_wrapper_offers(
    settings_obj,
    *,
    model: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
):
    """Every configured wrapper, resolved to the command it would run.

    Description: the picker's menu, built ONCE here so the preview and
        the validator read the same list from the same place. A wrapper
        that will not resolve right now (one that needs a model and was
        given none) comes back with ``command=None`` and a REASON rather
        than being dropped: a choice that vanishes from a picker reads as
        a wrapper the user never configured, which is a different and
        wrong statement.

    Inputs:
        settings_obj: the app ``Settings`` instance.
        model: model id to render into wrappers that take one.
        extra_args: further arguments appended to every offer's CLI,
            shlex-quoted by ``get_agent_command``. The restart preview
            passes the session's ``['--resume', '<uuid>']`` so each
            offer's command is byte-identical to what
            :func:`validate_agent_choice` will build when that offer is
            picked. Passing it to one and not the other is exactly how a
            badge and a button drift apart.

    Output:
        Optional[list[WrapperOffer]]: offers in config order, or None
            when the wrapper list could not be read at all. None is the
            absence of an answer and is reported by the caller as
            ``unavailable``; an EMPTY LIST is the real answer "this
            install configures no wrappers".

    Example:
        >>> resolve_wrapper_offers(settings)
        [WrapperOffer(agent_type='claude-chrome', ...)]
    """
    from src.core.session_restart_preview import WrapperOffer

    ids = configured_wrapper_ids(settings_obj)
    if ids is None:
        return None
    try:
        wrappers = settings_obj.load_auth_config().agents.wrappers
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("agent_choice_offers_unreadable", error=str(exc))
        return None

    offers = []
    for wrapper in wrappers:
        command = None
        reason = ""
        try:
            command = settings_obj.get_agent_command(
                wrapper.id, model=model, extra_args=extra_args
            )
        except (ValueError, OSError, AttributeError) as exc:
            reason = str(exc)
        offers.append(
            WrapperOffer(
                agent_type=str(wrapper.id),
                label=str(getattr(wrapper, "label", "") or wrapper.id),
                command=command,
                unavailable_reason=reason,
            )
        )
    return offers


def persist_agent_type(
    conn: sqlite3.Connection, *, socket: str, tmux_name: str, agent_type: str
) -> bool:
    """Record the picked wrapper on this session's row.

    Description: writes exactly ONE column, ``sessions.agent_type``, on
        the row for the newest instance of this tmux name on this socket.
        Nothing else is touched: not lineage, not origin, not the
        identity triple. A restart is still not a fork, and this write
        does not make it one.

        Call ONLY after a respawn verified a process alive in the pane
        with this wrapper's command - the module docstring says why.

    Inputs:
        conn: open connection to ``cloude.db``, writable.
        socket: tmux socket name the session lives on.
        tmux_name: literal tmux session name.
        agent_type: the wrapper id to record. Already validated.

    Output:
        bool: True when a row was updated. False means no row carried
            that instance (an external session the app has no record of),
            which is not an error - there is simply nowhere to remember
            it, and the restart itself still happened.

    Example:
        >>> persist_agent_type(conn, socket='cloude',
        ...     tmux_name='cloude_api', agent_type='claude-chrome')
        True
    """
    from src.core.session_store import identity_for_live_name

    row = identity_for_live_name(conn, socket=socket, name=tmux_name)
    if row is None:
        logger.info(
            "agent_choice_not_persisted_no_row",
            tmux_name=tmux_name,
            socket=socket,
            agent_type=agent_type,
        )
        return False

    conn.execute(
        "UPDATE sessions SET agent_type = ? WHERE id = ?",
        (agent_type, int(row["id"])),
    )
    conn.commit()
    logger.info(
        "agent_choice_persisted",
        session_row_id=int(row["id"]),
        tmux_name=tmux_name,
        agent_type=agent_type,
        was=row.get("agent_type"),
    )
    return True
