"""Run one title-sync pass for a session, and write at most one row.

The seam between the pure reader in :mod:`src.core.claude_title_sync` and
the database. It exists as its own module for the reason the respawn
ladder keeps ``session_transcript_presence`` separate: the rules stay
testable without a datastore, and ``session_manager.py`` - already well
past the 500-line guideline - does not grow another method.

WHAT THIS RUNS ON. Every Claude Code hook event, including ``PreToolUse``
and ``PostToolUse``, which fire on every single tool call. So the cost of
the common path is the design constraint, not an afterthought:

  * one SELECT of five columns on an indexed-ish lookup,
  * one ``os.path.getsize``,
  * one read of at most 64 KB,
  * and NO write at all unless a name actually changed.

The transcript path is resolved once per conversation and cached in
process, because :func:`conversation_presence` falls back to scanning the
corpus root's subdirectories and there are 89 of them on this machine.
Doing that per tool call is exactly the kind of "small" cost that turns
into a stalled event loop, which this codebase has already paid for once
with ``PRAGMA integrity_check`` on the version endpoint.

IDEMPOTENCE IS NOT OPTIONAL HERE. Hook events are unordered, duplicated
and droppable, so a pass must survive the same event twice and a missing
one. It does, because the decision is a comparison against
``sessions.claude_title`` rather than a transition: running it twice
compares the second time against what the first time wrote and stops.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

from src.core.claude_title_sync import (
    TITLE_APPLIED,
    TITLE_BASELINE_RECORDED,
    TITLE_NOT_MEASURED,
    decide_title_application,
    read_newest_custom_title,
)

logger = structlog.get_logger()

#: Outcome: the session has no tmux name we can key a row on.
SYNC_NO_SESSION: str = "no_session"

#: Outcome: no row, or a row with no conversation bound. NOT a failure -
#: a session whose ``SessionStart`` hook has not landed yet is simply not
#: syncable, and it becomes syncable on its own later.
SYNC_NO_CONVERSATION: str = "no_conversation"

#: Outcome: the transcript could not be located on disk. Distinct from
#: SYNC_NO_CONVERSATION: here a uuid IS bound and the file is not there,
#: which is the phantom-uuid shape and worth being able to count.
SYNC_NO_TRANSCRIPT: str = "no_transcript"

#: Outcome: the datastore could not be reached or read.
SYNC_UNAVAILABLE: str = "unavailable"

#: Outcome: a pass ran to completion. ``action`` carries what it decided.
SYNC_RAN: str = "ran"

#: Cache of conversation uuid -> transcript path. A transcript never
#: moves once written, so this is safe to hold for the process lifetime.
#: A uuid that could not be resolved is NOT cached negatively: the file
#: appears later (measured: 2m33s after the row bound its uuid, on
#: 2026-09-08), and caching the absence would make the sync permanently
#: blind to exactly the sessions it is most needed for.
_TRANSCRIPT_PATHS: Dict[str, str] = {}


@dataclass(frozen=True)
class TitleSyncResult:
    """What one sync pass did.

    Description: the return of :func:`sync_claude_title`.

      - ``outcome``: one of the ``SYNC_*`` constants.
      - ``action``: the ``TITLE_*`` verdict, on :data:`SYNC_RAN` only.
      - ``title``: the name now recorded, when one was written.
      - ``broadcast_title``: the name to announce over the websocket, set
        ONLY when the visible title actually moved. A caller gates its
        broadcast on this being non-None, so a baseline pass can never
        announce a rename that did not happen.
      - ``detail``: a plain sentence naming why.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    outcome: str
    action: Optional[str] = None
    title: Optional[str] = None
    broadcast_title: Optional[str] = None
    detail: Optional[str] = None


def _tmux_name_for(session_manager: Any, session_id: str) -> Optional[str]:
    """The tmux name a cloudecode session id keys its row on.

    Description: mirrors the resolution ``set_session_label`` does, minus
      the live tmux listing - this needs a name to SELECT on, not an
      epoch to key a new write on, so it never shells out. Falls back to
      the hook-reported map, then to the id itself, because an adopted
      session's id can BE a tmux name.
    Inputs: session_manager (SessionManager). session_id (str).
    Output: str | None - None when nothing can be keyed.
    Example: _tmux_name_for(mgr, 'ses_5a756046') -> 'cloude_Punchlist'
    """
    sess = getattr(session_manager, "sessions", {}).get(session_id)
    name = getattr(sess, "tmux_session", None) if sess else None
    if name:
        return str(name)
    hook_names = getattr(session_manager, "_hook_tmux_names", None) or {}
    name = hook_names.get(session_id)
    if name:
        return str(name)
    return session_id or None


def _transcript_path_for(uuid: str, working_dir: Optional[str]) -> Optional[str]:
    """Locate one conversation's jsonl, memoised for the process.

    Description: delegates to
      ``session_transcript_presence.conversation_presence``, which is the
      single source of truth for how a uuid maps to a file, so this never
      re-derives a slug. Only a PRESENT verdict is cached; see
      :data:`_TRANSCRIPT_PATHS` for why an absence deliberately is not.
    Inputs: uuid (str) - the conversation. working_dir (str | None) - the
      session's directory, used only for the fast-path slug.
    Output: str | None - the path, or None when it is not on disk.
    Example: _transcript_path_for('916c846d-...', '/Users/x/proj')
    """
    cached = _TRANSCRIPT_PATHS.get(uuid)
    if cached:
        return cached

    from src.core.session_transcript_presence import conversation_presence

    presence = conversation_presence(uuid, working_dir=working_dir)
    if presence.path is None:
        return None
    resolved = str(presence.path)
    _TRANSCRIPT_PATHS[uuid] = resolved
    return resolved


def sync_claude_title(session_manager: Any, session_id: str) -> TitleSyncResult:
    """Bring ``sessions.title`` in step with a name typed into the TUI.

    Description: reads the tail of the session's transcript, asks
      :func:`decide_title_application` what it means, and writes at most
      one row. NEVER RAISES: this runs on the hook endpoint's critical
      path and a title is telemetry, so every failure becomes a named
      outcome the caller may log and ignore.

      On :data:`TITLE_APPLIED` both ``title`` and ``claude_title`` are
      written; on :data:`TITLE_BASELINE_RECORDED` only ``claude_title``
      is, leaving the user's visible label alone. Nothing else in the row
      is touched - in particular no identity column, so a title sync can
      never move a row out from under the instance triple the way the old
      rename could.
    Inputs: session_manager (SessionManager) - for the tmux name and a
      datastore connection. session_id (str) - the cloudecode session id.
    Output: TitleSyncResult.
    Example: sync_claude_title(mgr, 'ses_5a756046').outcome -> 'ran'
    """
    tmux_name = _tmux_name_for(session_manager, session_id)
    if not tmux_name:
        return TitleSyncResult(
            SYNC_NO_SESSION,
            detail="no tmux name for this session, so no row can be keyed",
        )

    conn = None
    try:
        conn = session_manager._writable_datastore_connection()
        if conn is None:
            return TitleSyncResult(
                SYNC_UNAVAILABLE,
                detail="the datastore could not be opened for writing",
            )

        # THE ROW IS RESOLVED THROUGH THE SANCTIONED HELPER, NOT A SECOND
        # NAME-KEYED QUERY OF OUR OWN. A tmux name is reused every time a
        # session is recreated after its pane dies, so "the newest row
        # with this name" is a recency guess rather than identity -
        # tests/test_no_name_keyed_session_identity.py exists to stop new
        # instances of exactly that, and it caught this lookup. Reusing
        # ``identity_for_live_name`` means there is ONE such guess in the
        # codebase to fix rather than two, and it constrains on
        # ``tmux_socket`` as well, which a hand-rolled query here did not.
        # Everything after this keys on the primary key.
        from src.core.session_store import identity_for_live_name

        identity = identity_for_live_name(
            conn,
            socket=session_manager._tmux_socket_name(),
            name=tmux_name,
        )
        if not identity:
            return TitleSyncResult(
                SYNC_NO_CONVERSATION,
                detail="no instance row for this tmux name",
            )

        row = conn.execute(
            "SELECT claude_session_uuid, title, claude_title, working_dir "
            "FROM sessions WHERE id = ?",
            (identity["id"],),
        ).fetchone()
        if row is None:
            return TitleSyncResult(
                SYNC_NO_CONVERSATION,
                detail="the identified row disappeared between two reads",
            )

        row_id = identity["id"]
        uuid, current_title, claude_title, working_dir = (
            row[0],
            row[1],
            row[2],
            row[3],
        )
        if not uuid:
            return TitleSyncResult(
                SYNC_NO_CONVERSATION,
                detail=(
                    "no claude conversation is bound to this row yet, so "
                    "there is no transcript to read a name out of"
                ),
            )

        path = _transcript_path_for(str(uuid), working_dir)
        if not path:
            return TitleSyncResult(
                SYNC_NO_TRANSCRIPT,
                detail=(
                    "a conversation uuid is recorded but no transcript "
                    "for it is on this machine"
                ),
            )

        read = read_newest_custom_title(path)
        verdict = decide_title_application(
            read,
            current_title=current_title,
            recorded_claude_title=claude_title,
        )

        if verdict.action == TITLE_NOT_MEASURED:
            return TitleSyncResult(
                SYNC_RAN, action=verdict.action, detail=verdict.detail
            )

        if verdict.action == TITLE_BASELINE_RECORDED:
            from src.core.db import transaction

            with transaction(conn):
                conn.execute(
                    "UPDATE sessions SET claude_title = ? WHERE id = ?",
                    (verdict.title, row_id),
                )
            logger.info(
                "claude_title_baseline_recorded",
                session_id=session_id,
                row_id=row_id,
                note="visible title untouched; see claude_title_sync docstring",
            )
            return TitleSyncResult(
                SYNC_RAN,
                action=verdict.action,
                title=verdict.title,
                detail=verdict.detail,
            )

        if verdict.action == TITLE_APPLIED:
            from src.core.db import transaction

            with transaction(conn):
                if verdict.writes_visible_title:
                    conn.execute(
                        "UPDATE sessions SET title = ?, claude_title = ? "
                        "WHERE id = ?",
                        (verdict.title, verdict.title, row_id),
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET claude_title = ? WHERE id = ?",
                        (verdict.title, row_id),
                    )
            logger.info(
                "claude_title_synced",
                session_id=session_id,
                row_id=row_id,
                moved_visible_title=verdict.writes_visible_title,
            )
            return TitleSyncResult(
                SYNC_RAN,
                action=verdict.action,
                title=verdict.title,
                broadcast_title=(
                    verdict.title if verdict.writes_visible_title else None
                ),
                detail=verdict.detail,
            )

        return TitleSyncResult(
            SYNC_RAN, action=verdict.action, detail=verdict.detail
        )
    except sqlite3.Error as exc:
        logger.debug(
            "claude_title_sync_db_failed", session_id=session_id, error=str(exc)
        )
        return TitleSyncResult(
            SYNC_UNAVAILABLE, detail=f"the datastore refused the read: {exc}"
        )
    except OSError as exc:
        logger.debug(
            "claude_title_sync_io_failed", session_id=session_id, error=str(exc)
        )
        return TitleSyncResult(
            SYNC_UNAVAILABLE, detail=f"the transcript could not be reached: {exc}"
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                # A connection that will not close is already unusable and
                # the pass is over; nothing here can act on it.
                pass
