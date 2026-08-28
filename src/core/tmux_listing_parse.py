"""Parsing one ``tmux list-sessions`` row, and deciding whether it is OURS.

Split out of src/core/tmux_backend.py for two reasons. The obvious one is
that the backend is 76K and already does too much. The load-bearing one is
that BOTH decisions in this file used to be made on a caller-controlled
string, inline, with no way to unit-test them without shelling out to
tmux - so neither was ever tested against a hostile input.

THE DELIMITER PROBLEM, AND WHY THE FIELD ORDER IS THE FIX.

The old format string was::

    #{session_name}|#{session_created}|#{session_windows}

parsed with ``line.split("|")`` taking fields 0, 1 and 2. tmux forbids
only ``.`` and ``:`` in a session name; ``|`` is legal. So a session
named ``victim|1755000000|1`` prints::

    victim|1755000000|1|1787070480|1

and fields 0, 1, 2 read back as name ``victim``, created ``1755000000``,
windows ``1`` - an instance triple the CALLER chose, not one tmux
reported. Both halves of the app's identity key were forgeable, and no
attacker was required: a project named ``api|prod`` does it by accident.

THE FIX IS STRUCTURAL, NOT A BLOCKLIST. Two properties together:

  1. THE VARIABLE-LENGTH FIELD GOES LAST. The format is now
     ``#{session_id}|#{session_created}|#{session_windows}|#{session_name}``
     and the row is split with ``split("|", 3)`` - a BOUNDED split. It
     stops after the third separator, so everything remaining, pipes and
     all, is the name, verbatim. The name can no longer reach the parser
     as more than one field, because the parser stops counting before it
     gets there.

  2. THE THREE LEADING FIELDS ARE TMUX-GENERATED AND STRICTLY VALIDATED.
     ``session_id`` is ``$`` followed by digits, ``session_created`` and
     ``session_windows`` are decimal integers. None of them can contain a
     ``|`` because tmux generates all three; none is caller-influenced in
     any way. Each is matched against an anchored pattern, and a row
     whose leading fields do not match is REJECTED, not guessed at.

Property 1 alone makes the name unforgeable WITHIN A ROW. Property 2 is
what makes the rejection honest: it is the only reason a malformed row
can be told apart from a valid one.

THE ROW DELIMITER IS THE SECOND HALF OF THE SAME PROBLEM, AND THIS
DOCSTRING USED TO GET IT WRONG. It previously claimed property 2 "would
catch a wrapped or truncated line, since a continuation could not begin
with ``$<digits>|``". That guarantee is FALSE and was disproven by
measurement. A continuation begins with whatever the session name says it
begins with, and a name is caller-controlled, so a name containing
``<boundary>$99|1755000000|1|cloude_work`` writes a continuation that
begins with exactly ``$<digits>|``, parses cleanly, and forges the whole
identity triple.

The gap was never the field parser. It was one layer up, in the CALLER,
which split the stdout blob with ``str.splitlines()``. Python recognises
TEN line boundaries; tmux rejects only SEVEN of them in a session name.
Measured against tmux 3.7b, tmux ACCEPTS ``U+0085`` (NEL), ``U+2028``
(LS) and ``U+2029`` (PS), and Python splits on all three - so ONE tmux
line became TWO parser rows, the second of them entirely attacker-chosen.

Two properties close it, and BOTH are applied because they are different
kinds of guarantee:

  3. THE ROW SPLIT IS THE ONE tmux ACTUALLY EMITS. :func:`split_listing_rows`
     splits on ``"\\n"`` and nothing else, because that is the only row
     terminator ``tmux list-sessions`` writes. This is the CORRECT PARSE:
     it makes the injected characters ordinary content inside a single
     row, where the bounded split of property 1 already renders them
     harmless. Every enumeration site in the backend uses it.

  4. NO PARSED NAME MAY CONTAIN A PYTHON LINE BOUNDARY.
     :func:`parse_listing_row` REFUSES a row whose name contains any of
     the ten. Under the correct split of property 3, the entire injection
     payload lands INSIDE the name field - so this turns the attack from
     "renders as one session with a strange name" into "refused and
     logged", which is the outcome an operator can actually see.

BE PRECISE ABOUT WHAT PROPERTY 4 DOES NOT DO, because the previous
version of this docstring was wrong in exactly this way and a wrong
guarantee is worse than none. It does NOT protect a caller that reaches
for ``splitlines()`` anyway: that splitter CONSUMES the boundary
character, so the forged row arrives here with a perfectly clean name
and nothing in this function can tell it from a real one. The check is
only reachable when the row was split correctly to begin with.

The guarantee that a caller cannot regress is therefore NOT a runtime
check and cannot be one. It is enforced structurally, by a test that
walks the AST of the backend and fails if any tmux listing output is
ever handed to ``splitlines()``. See
tests/test_tmux_row_delimiter.py::test_no_listing_site_uses_splitlines.
That test lives at the layer that actually enforces the property - the
call site - which is the lesson from the previous round, where an AST
proof constrained the field parser while the hole sat one layer up.

WHY ``session_id`` IS A DISCRIMINATOR AND NOT THE IDENTITY. tmux's
``#{session_id}`` (``$0``, ``$1``, ...) is unique per SERVER LIFETIME and
is never reused while that server lives, which makes it strictly better
than a one-second timestamp at telling two sessions apart RIGHT NOW. It
is strictly WORSE as a durable key, because the counter restarts at
``$0`` when the server does - so ``$3`` today and ``$3`` after a reboot
are different sessions with the same id. The stored identity therefore
stays the instance triple (socket, name, created epoch), which survives a
restart, and ``session_id`` is carried alongside it as a discriminator
that can only ever REFUSE a match, never widen one. See
src/core/session_identity.py for where that refusal is enforced.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()

#: The tmux ``-F`` format this module parses. The caller-controlled field
#: is LAST, which is what makes the bounded split below unambiguous.
#: Changing this string without changing :func:`parse_listing_row` in the
#: same edit is a parse the format no longer describes, so
#: tests/test_tmux_listing_parse.py asserts the two agree.
LISTING_FORMAT = "#{session_id}|#{session_created}|#{session_windows}|#{session_name}"

#: The delimiter. Present in exactly one place so the format string and
#: the split cannot drift apart.
FIELD_SEPARATOR = "|"

#: Number of separators to consume before the remainder is the name. One
#: fewer than the field count, by definition of ``str.split(sep, maxsplit)``.
_MAXSPLIT = 3

#: ``#{session_id}`` is tmux-generated: a literal ``$`` and a decimal
#: counter. Anchored, so nothing else is accepted as one.
_SESSION_ID_RE = re.compile(r"^\$\d+$")

#: ``#{session_created}`` and ``#{session_windows}`` are decimal integers.
_INTEGER_RE = re.compile(r"^\d+$")

#: The ONLY row terminator ``tmux list-sessions`` writes. Named here so
#: the split below is a statement about tmux's output format rather than
#: an incidental call to whatever splitter came to hand.
ROW_SEPARATOR = "\n"

#: Every character ``str.splitlines()`` treats as a line boundary.
#:
#: This tuple exists because Python's set and tmux's set DISAGREE, and
#: that disagreement was a forgery primitive. Measured against tmux 3.7b
#: (see the module docstring): tmux refuses the first seven in a session
#: name and ACCEPTS the last three, while Python splits on all ten. Any
#: code path that splits tmux output with ``splitlines()`` therefore
#: turns one tmux row into several parser rows at the caller's choosing.
#:
#: Kept as an explicit tuple, not derived from ``splitlines()`` at import
#: time, so the set this module refuses is auditable in the source and
#: cannot shift under a Python upgrade without a visible diff.
LINE_BOUNDARY_CHARS: Tuple[str, ...] = (
    "\n",        # LF
    "\r",        # CR
    "\v",        # VT      \x0b
    "\f",        # FF      \x0c
    "\x1c",      # FS
    "\x1d",      # GS
    "\x1e",      # RS
    "\x85",      # NEL     tmux ACCEPTS
    " ",    # LS      tmux ACCEPTS
    " ",    # PS      tmux ACCEPTS
)


def split_listing_rows(stdout_text: str) -> List[str]:
    """Split a tmux listing blob into rows on the terminator tmux emits.

    Description: the correct row parse, and the primary fix for the
      row-delimiter forgery described in the module docstring. Splits on
      ``ROW_SEPARATOR`` ("\\n") ALONE - never ``str.splitlines()``, whose
      wider ten-character alphabet lets a session name containing NEL, LS
      or PS manufacture extra rows that the field parser then reads as
      tmux-generated. A trailing carriage return is removed per row so a
      CRLF-terminated stream still yields clean rows, and blank rows
      (including the empty tail left by the final newline) are dropped.
    Inputs:
        stdout_text (str): decoded stdout from ``tmux list-sessions`` or
            ``tmux list-panes``. Untrusted: session names inside it may
            contain any character tmux permits, boundaries included.
    Output:
        List[str]: one entry per row tmux actually emitted, in order.
    Example:
        >>> split_listing_rows("$1|1|1|a\\u2028b\\n")
        ['$1|1|1|a\\u2028b']
        >>> len("$1|1|1|a\\u2028b\\n".splitlines())
        2
    """
    rows: List[str] = []
    for raw in (stdout_text or "").split(ROW_SEPARATOR):
        row = raw.rstrip("\r")
        if row.strip():
            rows.append(row)
    return rows


def name_has_line_boundary(name: str) -> bool:
    """Report whether a parsed session name contains a Python line boundary.

    Description: the defence-in-depth half of the row-delimiter fix. A
      name carrying any of :data:`LINE_BOUNDARY_CHARS` is refused even
      though :func:`split_listing_rows` has already made it harmless,
      because that correctness depends on every present and FUTURE caller
      avoiding ``splitlines()``. Refusing the name locally means a caller
      that regresses gets no rows rather than forged ones.
    Inputs:
        name (str): the parsed ``#{session_name}`` field.
    Output:
        bool: True when the name contains at least one boundary character
            and must not be trusted as a single row's name.
    Example:
        >>> name_has_line_boundary("plain")
        False
        >>> name_has_line_boundary("a\\u2028b")
        True
    """
    return any(char in name for char in LINE_BOUNDARY_CHARS)


def parse_listing_row(line: str) -> Optional[Dict[str, Any]]:
    """Parse one ``tmux list-sessions`` line into a row, or reject it.

    Description: the bounded-split parser described in the module
      docstring. Returns None rather than a partially-trusted row for any
      line it cannot fully validate, which is the three-outcome rule
      applied to a parser: parsed, absent, and NEVER a guess. A rejected
      row is logged by the caller, not silently dropped here, so that a
      format change shows up as rows going missing WITH a log line rather
      than as a quietly shorter list.
    Inputs:
        line (str): one line of ``tmux -F LISTING_FORMAT`` output, with
            or without its line terminator. Untrusted: the trailing
            name field may contain the separator, spaces, unicode, or
            anything else tmux permits in a session name.
    Output:
        dict | None: ``{"session_id": str, "created_at_epoch": int,
            "window_count": int, "name": str}`` when every leading field
            validated, the name is non-empty, and the name carries no
            Python line boundary; None otherwise.
    Example:
        >>> parse_listing_row("$3|1755000000|2|api|prod")["name"]
        'api|prod'
        >>> parse_listing_row("garbage") is None
        True
        >>> parse_listing_row("$3|1|1|a\\u2028b") is None
        True
    """
    # Only the LINE TERMINATOR is stripped, not all whitespace: tmux
    # permits leading and trailing spaces in a session name, and this
    # parser must return the name tmux actually holds. Rejecting the row
    # for an empty remainder is handled by the name check below.
    stripped = (line or "").rstrip("\r\n")
    if not stripped.strip():
        return None

    parts = stripped.split(FIELD_SEPARATOR, _MAXSPLIT)
    if len(parts) != _MAXSPLIT + 1:
        return None

    session_id, created_raw, windows_raw, name = parts

    # Every leading field is tmux-generated. A value that does not match
    # its anchored pattern means we are not looking at the format we
    # asked for, and the safe response is to refuse the whole row.
    if not _SESSION_ID_RE.match(session_id):
        return None
    if not _INTEGER_RE.match(created_raw):
        return None
    if not _INTEGER_RE.match(windows_raw):
        return None
    if not name:
        return None
    # Defence in depth for the row delimiter (module docstring property
    # 4). split_listing_rows has already made a boundary character
    # ordinary content, so reaching here with one in the name means
    # either a caller that split the blob some other way, or a name no
    # legitimate tmux workflow produces. Refuse either.
    if name_has_line_boundary(name):
        return None

    return {
        "session_id": session_id,
        "created_at_epoch": int(created_raw),
        "window_count": int(windows_raw),
        "name": name,
    }


def resolve_ownership(
    name: str,
    created_at_epoch: int,
    owned_instances: Optional[Set[Tuple[str, int]]],
    owned_names: Optional[Set[str]],
    prefix: str = "",
) -> bool:
    """Decide whether one live tmux session was created by this app.

    Description: the ownership badge, in one place, with an explicit
      precedence order. The tier that matters is the SECOND one, and it
      is what closes the wildcard hole.

      1. INSTANCE MATCH. ``(name, epoch)`` is in ``owned_instances``.
         This is an identity match and it is the only positive answer
         backed by evidence about THIS process.

      2. NEGATIVE DB OPINION. The name appears in ``owned_instances``
         under a DIFFERENT epoch. The datastore has a specific,
         epoch-keyed opinion about this name and THIS INSTANCE IS NOT
         IT - so the answer is False and the lower tiers are never
         consulted. Previously the legacy name set was folded into
         ``owned_instances`` as a ``(name, None)`` wildcard which matched
         here instead, which meant the epoch was ignored for every
         session the app had created since the last restart - exactly the
         sessions the epoch was added to protect. A dead ``cloude_work``
         replaced by the user's own unrelated ``cloude_work`` badged as
         ours.

      3. LEGACY NAME SET. The datastore has no opinion about this name at
         all, and the in-memory ``owned_tmux_sessions`` holds it. This is
         a DEGRADED, name-only tier and it is lossy in the documented
         way: a name owned as one instance and now reused by a different
         one reads as owned. It is reached only when the DB is silent
         about the name, so it cannot override tier 2.

      4. PREFIX HEURISTIC. Only when neither owned argument was supplied
         at all. Spoofable, and the live app path should always pass an
         owned set.
    Inputs:
        name (str): the tmux session name, as parsed.
        created_at_epoch (int): ``#{session_created}``.
        owned_instances (set[tuple[str, int]] | None): ``(name, epoch)``
            pairs from ``sessions.origin``. None means the datastore had
            NO opinion (pre-v2, unreadable); an EMPTY SET is a real
            answer of "the app owns nothing" and is not the same thing.
            Entries must carry an integer epoch; a None epoch is not a
            wildcard and is ignored.
        owned_names (set[str] | None): the legacy in-memory name set.
        prefix (str): session-name prefix for the tier-4 heuristic.
    Output:
        bool: True when this instance badges as OURS.
    Example:
        >>> resolve_ownership("a", 2, {("a", 1)}, {"a"})
        False
    """
    if owned_instances is not None:
        if (name, created_at_epoch) in owned_instances:
            return True
        # Tier 2. Any stored instance for this name is a specific opinion
        # keyed on an epoch, and this instance did not match it.
        if any(
            owned_name == name and owned_epoch is not None
            for owned_name, owned_epoch in owned_instances
        ):
            return False

    if owned_names is not None:
        return name in owned_names

    if owned_instances is not None:
        # An owned-instance set was supplied and had nothing to say about
        # this name. That is a real answer, not a reason to guess.
        return False

    return bool(prefix) and name.startswith(prefix)
