"""The user-facing LABEL, and the lossy filter that derives a tmux name.

TWO THINGS THAT WERE ONE THING. A session's displayed name and its tmux
session name were the same string, so renaming a session meant
``tmux rename-session``, which moves the one field the identity key
``(socket, name, epoch)`` was built on. The stored row stopped matching
the live listing, the reaper called it dead, and the same live session
came back through the adopt path as a stranger and got a second row. One
session, two rows, one of them a corpse.

Separating them does not patch that path - it makes it UNREACHABLE from
the rename surface. A label lives in ``sessions.title``. Changing it
writes one column. The tmux name and the creation epoch never move, so
the identity triple is never touched by a rename at all.

It also removes a restriction the user asked to have removed: the old
rename endpoint enforced ``^[A-Za-z0-9_-]{1,64}$``, so "Media
Compression" was not a legal session name. A label is a label; it takes
what a human types.

WHAT WAS MEASURED ABOUT TMUX, RATHER THAN ASSUMED. On a throwaway socket,
``new-session -s`` ACCEPTED every one of: a space, ``:``, ``.``, ``/``,
``"``, ``$``. So tmux is far more permissive than the old validator
implied. Two real constraints remain:

  NON-ASCII IS NOT PRESERVED. A session created as ``emoji 🚀 ok`` reads
  back from ``list-sessions`` as ``emoji __ ok``. tmux mangles it, so a
  derived name containing one would not round-trip and every lookup by
  name would miss.

  ``:`` AND ``.`` ARE TARGET SYNTAX. tmux's ``-t`` grammar splits on
  them, so a name carrying either is legal to CREATE and awkward to
  ADDRESS - which is what every other call site in this codebase does.

Neither constrains the LABEL, because the label is never handed to tmux.

THE FILTER IS LOSSY AND THEREFORE NOT AN IDENTITY. ``client: acme`` and
``client. acme`` both sanitise to ``client_acme``. That is deliberate and
safe here precisely because the tmux name no longer identifies anything
on its own - the row is identified by ``(socket, name, epoch)`` and, for
a rename, by ``(epoch, #{session_id})``. The collision is resolved by
appending a suffix, never by rejecting the user's label, and never by
letting two live sessions share a tmux name.

WHY THIS DOES NOT DISTURB SIDEBAR GROUPS. Group membership is keyed on
``tmux_name`` on purpose: an unadopted tmux session has no ``sessions``
row to key on, so a row-keyed membership could not represent it at all.
That reasoning is untouched here. The tmux name is now MORE stable than
before, not less - nothing renames it after creation - so the group key
is strictly safer than it was.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from typing import Iterable, Optional, Set

import structlog

from src.core.trail_entry import utc_now

logger = structlog.get_logger()

#: Longest label we will store. Generous, because it is a human label and
#: not a key, but bounded so no surface has to defend against an
#: unbounded string and no row can be made pathological on purpose.
LABEL_MAX_CHARS: int = 200

#: Longest derived tmux name. tmux itself imposes no such limit; this
#: keeps the value comfortable inside a FIFO filename and a status line.
TMUX_NAME_MAX_CHARS: int = 64

#: The prefix this app puts on the tmux names it creates. Stripped when
#: deriving a label from an existing name, because it is an artefact of
#: the launcher rather than anything a user typed.
APP_TMUX_PREFIX = "cloude_"

#: Used when a label sanitises down to nothing at all. tmux will not take
#: an empty name and the caller has no better fallback than we do.
FALLBACK_TMUX_NAME = "session"

#: Characters replaced by ``_`` in a derived tmux name: tmux target
#: separators plus anything whitespace-like. Applied AFTER non-ASCII has
#: been dropped, so this only ever sees a byte it can reason about.
_UNSAFE_FOR_TMUX = re.compile(r"[\s:./\\'\"`$;|&<>(){}\[\]*?!#%^~,=+@]+")

#: Control characters, which no label may contain. Includes newline and
#: tab: a label is rendered on one line everywhere it appears, and a
#: newline would either truncate it or break the surface showing it.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class InvalidLabel(ValueError):
    """A label that cannot be stored or rendered as given.

    Description: raised rather than silently corrected, because quietly
      mangling what a user typed and then showing them the mangled
      version is how a UI teaches people not to trust it. The one
      correction applied without complaint is stripping surrounding
      whitespace, which is essentially always a paste artefact.
    """


def validate_label(label: Optional[str]) -> str:
    """Accept a human label, or say exactly why it cannot be one.

    Description: permissive on PURPOSE. Spaces, punctuation, mixed case
      and non-ASCII are all fine - this string is never handed to tmux,
      so tmux's constraints do not apply to it. Only two classes are
      refused: empty (there is nothing to show) and control characters
      (they break the single-line surfaces that render it).
    Inputs: label (str | None) - the raw value from the user.
    Output: str - the label to store, surrounding whitespace stripped.
    Raises: InvalidLabel - empty, too long, or carrying a control char.
    Example: validate_label('  Media Compression ')  # 'Media Compression'
    """
    if label is None:
        raise InvalidLabel("a session label cannot be empty")
    cleaned = label.strip()
    if not cleaned:
        raise InvalidLabel("a session label cannot be empty")
    if len(cleaned) > LABEL_MAX_CHARS:
        raise InvalidLabel(
            f"a session label may be at most {LABEL_MAX_CHARS} characters"
        )
    if _CONTROL_CHARS.search(cleaned):
        raise InvalidLabel(
            "a session label cannot contain control characters such as a "
            "newline or a tab"
        )
    return cleaned


def sanitize_tmux_name(label: str) -> str:
    """Derive a tmux-safe session name from a free-form label.

    Description: THE FILTER, and it is lossy by design - see the module
      docstring. Three passes, in this order:

        1. Drop everything non-ASCII. Measured: tmux does not preserve
           it, so a name carrying it would not round-trip through
           ``list-sessions`` and every lookup by name would miss.
        2. Replace runs of tmux-hostile characters with a single ``_``.
           That set leads with ``:`` and ``.`` because they are tmux's
           own ``-t`` target separators, and continues through the shell
           metacharacters, since this value ends up in command lines and
           in a FIFO filename.
        3. Trim leading and trailing ``_`` and truncate.

      Never returns an empty string: a label of ``!!!`` reduces to
      nothing and gets ``FALLBACK_TMUX_NAME`` instead, because tmux would
      refuse the empty name and this function's caller has no better
      answer available than this one does.
    Inputs: label (str) - any label; need not have been validated.
    Output: str - a non-empty ASCII name safe in a tmux target.
    Example: sanitize_tmux_name('client: acme')  # 'client_acme'
    """
    folded = unicodedata.normalize("NFKD", label or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    replaced = _UNSAFE_FOR_TMUX.sub("_", ascii_only)
    replaced = re.sub(r"_+", "_", replaced).strip("_")
    trimmed = replaced[:TMUX_NAME_MAX_CHARS].strip("_")
    return trimmed or FALLBACK_TMUX_NAME


def unique_tmux_name(label: str, *, taken: Iterable[str]) -> str:
    """Derive a tmux name that no live session already holds.

    Description: the collision half of a lossy filter. Two labels CAN
      sanitise to one name, and the answer is a suffix rather than a
      rejection: the user's label is theirs and is stored intact, while
      the tmux name is an internal handle that nobody reads and nothing
      identifies a session by on its own.

      The walk is BOUNDED. ``_2`` through ``_49``, and if every one of
      those is taken it falls back to an 8-hex-character digest of the
      label plus the taken-set size, which cannot loop. An unbounded
      "keep incrementing" would be a hang under a pathological set, and
      a hang in the create path is worse than an ugly name.
    Inputs: label (str) - the user's label. taken (Iterable[str]) - the
      names already in use on this socket.
    Output: str - a name not present in ``taken``.
    Example: unique_tmux_name('acme', taken={'acme'})  # 'acme_2'
    """
    used: Set[str] = set(taken)
    base = sanitize_tmux_name(label)
    if base not in used:
        return base
    room = max(1, TMUX_NAME_MAX_CHARS - 4)
    stem = base[:room].strip("_") or FALLBACK_TMUX_NAME
    for suffix in range(2, 50):
        candidate = f"{stem}_{suffix}"
        if candidate not in used:
            return candidate
    digest = hashlib.sha256(
        f"{label}|{len(used)}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{stem[: TMUX_NAME_MAX_CHARS - 9]}_{digest}"


def label_from_tmux_name(tmux_name: Optional[str]) -> Optional[str]:
    """Derive a readable label from an existing tmux session name.

    Description: used to give rows that predate the label a sensible one
      instead of a blank. Two transforms, both reversing something the
      app did rather than something the user chose: the ``cloude_``
      prefix the launcher adds, and the underscores the filter
      substituted for spaces.

      THE PREFIX IS ONLY REMOVED WHEN SOMETHING SURVIVES IT. A name of
      exactly ``cloude_`` keeps its stem rather than becoming empty; a
      blank label is worse than an ugly one, and the whole point of this
      column is that a row always has something to show.
    Inputs: tmux_name (str | None).
    Output: str | None - the derived label, or None for no input.
    Example: label_from_tmux_name('cloude_Media')  # 'Media'
    """
    if not tmux_name:
        return None
    stem = tmux_name
    if stem.startswith(APP_TMUX_PREFIX) and len(stem) > len(APP_TMUX_PREFIX):
        stem = stem[len(APP_TMUX_PREFIX) :]
    return stem.replace("_", " ").strip() or tmux_name


def set_label(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
    label: str,
    now: Optional[str] = None,
) -> bool:
    """Store a user-chosen label on one session row.

    Description: THE USER SET, and deliberately not the same operation as
      ``session_lineage._record_claude_title``. That one records what
      CLAUDE calls the session, into ``claude_title``. This one is a
      person deciding what their session is called, into ``title``.

      They used to be two operations on ONE column, which is why that one
      had to refuse to overwrite - a guard that stopped Claude's
      auto-generated name replacing a chosen one, at the cost of making a
      genuine later Claude-side rename impossible to record. Since v10
      they are two columns with two owners, so neither has to compromise
      and neither needs a guard against the other.

      IDENTITY IS NOT IN THE UPDATE. ``tmux_name``, ``tmux_created_epoch``
      and ``tmux_session_id`` are not in the SET clause and never will
      be. That is the property that makes a rename structurally incapable
      of splitting a session into two rows, so it is asserted by a test
      rather than left to review.

      The caller owns the transaction, matching every other write path in
      this package.
    Inputs: conn (sqlite3.Connection). session_uuid (str) - the row's
      external identity, which survives a tmux rename and is therefore
      the right handle for this. label (str) - validated here, before any
      write. now (str | None) - ISO stamp, defaults to ``utc_now()``.
    Output: bool - True when a row was updated. False means NO SUCH ROW,
      which is a definite negative the caller can render, not an error.
    Raises: InvalidLabel - the label cannot be stored; nothing is written.
    Example: set_label(conn, session_uuid='u1', label='Media Compression')
    """
    cleaned = validate_label(label)
    stamp = now or utc_now()
    cursor = conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE session_uuid = ?",
        (cleaned, stamp, session_uuid),
    )
    if cursor.rowcount > 0:
        logger.info(
            "session_label_set",
            session_uuid=session_uuid,
            note="title only; no identity column is in this statement",
        )
        return True
    logger.warning(
        "session_label_no_such_row",
        session_uuid=session_uuid,
        note="no row carries that session_uuid, so nothing was labelled",
    )
    return False


def set_label_for_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
    label: str,
    now: Optional[str] = None,
) -> bool:
    """Store a label on the row for one tmux INSTANCE.

    Description: the same write as :func:`set_label`, keyed on the
      instance triple instead of the row uuid, because that is what the
      HTTP rename surface has in hand - it knows which tmux session the
      user is looking at, not which row id.

      KEYED ON THE FULL TRIPLE, NEVER THE NAME ALONE. Names are reusable
      and this table can legitimately hold several rows under one name
      with different epochs (history of a name that was used twice). A
      write keyed on the name alone would relabel all of them, which is
      the same class of defect as adopting by name - and it is asserted
      by a test rather than left to review. A None epoch has no identity
      to key on and refuses immediately.

      IDENTITY IS NOT IN THE UPDATE. ``tmux_name``, ``tmux_created_epoch``
      and ``tmux_session_id`` appear only in the WHERE clause. That is
      what makes a rename structurally incapable of moving a session's
      identity, which is the entire point of separating the two.
    Inputs: conn (sqlite3.Connection). socket (str), name (str), epoch
      (int | None) - the instance triple. label (str) - validated before
      any write. now (str | None) - ISO stamp.
    Output: bool - True when a row was updated; False means NO SUCH
      INSTANCE, a definite negative rather than an error.
    Raises: InvalidLabel - nothing is written.
    Example:
        set_label_for_instance(conn, socket='cloude', name='cloude_a',
                               epoch=7, label='Media Compression')
    """
    cleaned = validate_label(label)
    if epoch is None:
        logger.warning(
            "session_label_no_epoch",
            tmux_socket=socket,
            tmux_name=name,
            note=(
                "no creation epoch, so there is no instance to key on; "
                "nothing written rather than relabelling by name"
            ),
        )
        return False
    stamp = now or utc_now()
    cursor = conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ?",
        (cleaned, stamp, socket, name, int(epoch)),
    )
    if cursor.rowcount > 0:
        logger.info(
            "session_label_set_for_instance",
            tmux_socket=socket,
            tmux_name=name,
            tmux_created_epoch=int(epoch),
            note="title only; no identity column is in the SET clause",
        )
        return True
    logger.warning(
        "session_label_no_such_instance",
        tmux_socket=socket,
        tmux_name=name,
        tmux_created_epoch=int(epoch),
        note="no row carries that instance triple, so nothing was labelled",
    )
    return False


def _clean_title(value) -> Optional[str]:
    """Normalise a stored ``title`` into a label or None.

    Description: the ONE place "is there a label here" is decided, so
      every read agrees. A NULL title, a title that is not text, and a
      title that is only whitespace are all the same answer: this row
      carries no label. Rendering a blank string would be worse than
      rendering the tmux handle, so the caller must be able to tell.
    Inputs: value (Any) - whatever the ``title`` column held.
    Output: str | None - the trimmed label, or None.
    Example: _clean_title('  Media  ')  -> 'Media'
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def label_for_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
) -> Optional[str]:
    """Read the label for one tmux INSTANCE. Exact, or None.

    Description: the read counterpart of :func:`set_label_for_instance`,
      keyed on the same full triple. Use this wherever the caller holds
      a creation epoch - a tmux listing row carries ``created_at_epoch``
      and the attribution prompt carries ``epoch``, so for those callers
      the exact key is free.

      There is no guessing in here at all. The answer is either the
      label stored against THIS instance or None; it can never be some
      other session's label, which is what makes it safe for a surface a
      human reads as identity. A None epoch has nothing to key on and
      answers None immediately rather than falling back to the name.

      NEVER RAISES. A decoration read must not be able to fail the
      payload it is decorating.
    Inputs: conn (sqlite3.Connection). socket (str), name (str), epoch
      (int | None) - the instance triple.
    Output: str | None - the label, or None for "this instance carries
      none", which every client renders by falling back to the tmux name.
    Example:
        label_for_instance(conn, socket='cloude', name='cloude_a',
                           epoch=7)  -> 'Media Compression'
    """
    if not name or epoch is None:
        return None
    try:
        row = conn.execute(
            "SELECT title FROM sessions "
            "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ? "
            "ORDER BY id DESC LIMIT 1",
            (socket, name, int(epoch)),
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.debug("session_label_instance_read_threw", error=str(exc))
        return None
    if row is None:
        return None
    return _clean_title(dict(row).get("title"))


def label_for_name(
    conn: sqlite3.Connection, *, socket: str, name: str
) -> Optional[str]:
    """Read the label for a tmux NAME. Weaker key, bounded weakness.

    Description: for the callers that genuinely have no epoch - a toast
      is recorded from a hook event carrying only a session id, and
      probing tmux for a creation time on every hook event is not a cost
      a notification path can pay.

      THE NEWEST ROW DECIDES, AND ITS ANSWER IS FINAL. Names are
      reusable, so this table can hold several rows under one name. The
      newest row for a name is the most recently created instance of it,
      which is the live one whenever there is a live one. If that row
      carries no title, the answer is None.

      What that rule FORBIDS is the previous shape, which filtered to
      rows that had a title and took the newest of those: a dead
      predecessor could then lend its label to a live session that had
      never been named, and the user would read a stranger's name as
      this session's identity. Asserted in
      tests/test_session_label_reads.py rather than left to review.

      One case this key still cannot cover: a live session with NO row
      at all (an external session this app never created) sharing a name
      with a former app-created session. Prefer :func:`label_for_instance`
      wherever an epoch exists.

      NEVER RAISES.
    Inputs: conn (sqlite3.Connection). socket (str), name (str).
    Output: str | None - the label, or None.
    Example: label_for_name(conn, socket='cloude', name='cloude_a')
    """
    if not name:
        return None
    try:
        row = conn.execute(
            # tmux_created_epoch IS NOT NULL RESTRICTS THIS TO ROWS THAT
            # ARE ACTUALLY TMUX INSTANCES.
            #
            # A CONVERSATION row shares the socket and name of the session
            # it happened inside and carries a NULL epoch - that is the
            # lineage shape. Without this clause the "newest row for the
            # name" is whatever conversation ran most recently, and a
            # conversation never has a title. So one /clear inside a
            # session made its NAME disappear: the row was there, the
            # title was there, the instance-keyed read returned it
            # correctly, and this one answered None. A rename that
            # persisted and then refused to render.
            #
            # The docstring's reasoning about a dead predecessor is
            # unaffected: among real instances the newest still decides.
            "SELECT title FROM sessions "
            "WHERE tmux_socket = ? AND tmux_name = ? "
            "AND tmux_created_epoch IS NOT NULL "
            "ORDER BY tmux_created_epoch DESC, id DESC LIMIT 1",
            (socket, name),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("session_label_name_read_threw", error=str(exc))
        return None
    if row is None:
        return None
    return _clean_title(dict(row).get("title"))
