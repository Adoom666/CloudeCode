"""Keyset cursors for the archive browse API - encode, parse, or REFUSE.

This module is the ONLY place a cursor is built or read. Nothing else may
construct one, and nothing else may guess at what one means.

WHY REFUSING MATTERS MORE THAN PARSING. The tempting implementation of
``decode_cursor`` treats a defective cursor as "start at the beginning".
That turns a client bug into an infinite loop that LOOKS like it is
working: the client pages, the server silently restarts at page 1, the
client renders the same rows again, and nobody sees an error. Worse, the
mirror case skips: a cursor that half-parses can position a page past
rows that are then never visited by anyone. So every defect here raises
:class:`CursorError`, the route layer turns that into a 400 with
``result_status: "cannot_determine"``, and the client finds out.

THE KIND CHECK, AND A DISAGREEMENT WITH THE SPEC WORTH KNOWING ABOUT.
``docs/message-browser-api.md`` section 5.1 says ``kind`` is "embedded and
checked on decode". Every LITERAL cursor string printed elsewhere in that
same document decodes to a payload with NO ``kind`` key - for example
``eyJsaW5lX25vIjoyLCJ2IjoxfQ`` is exactly ``{"line_no":2,"v":1}``. The two
statements cannot both be implemented. The literal strings win here,
because they are byte-level artifacts that appear in the response examples
a client will be written against, while the prose describes a mechanism
whose GOAL - stopping a cursor for one endpoint being replayed against
another - is achieved just as well by validating the payload against the
kind's declared schema. A ``lines`` cursor offered to ``transcripts``
fails because it carries no ``ingested_at`` and no ``id``.

The one place that check is weaker than an embedded tag: ``transcripts``
and ``unattributed`` page the same columns in the same order, so their
payloads are structurally identical and a cursor for one is accepted by
the other. That is stated rather than hidden. It is also harmless, since
both order ``(ingested_at DESC, id DESC)`` over the same table and the
worst outcome is a page positioned correctly inside a different scope.

A cursor is OPAQUE but it is NOT a security boundary. It is not encrypted
and it is not signed, because every value inside it is already visible in
the response that produced it. Clients must not construct or inspect one.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Dict, Mapping, Tuple, Type

#: Every cursor payload carries this. A payload whose ``v`` is anything
#: else is a CursorError, not something to migrate in flight - a server
#: that guesses at an unknown cursor version is guessing at a POSITION.
CURSOR_VERSION = 1

#: The payload key holding the version. Named so the schemas below can
#: include it without three modules disagreeing about its spelling.
VERSION_KEY = "v"

# --- kind names, one per paginated endpoint -----------------------------

CURSOR_PROJECTS = "projects"
CURSOR_TRANSCRIPTS = "transcripts"
CURSOR_UNATTRIBUTED = "unattributed"
CURSOR_LINES = "lines"
CURSOR_SUBAGENTS = "subagents"
CURSOR_SEARCH = "search"

#: Field types, declared per kind. ``bool`` is deliberately NOT accepted
#: where ``int`` is required: in Python ``isinstance(True, int)`` is True,
#: so an unguarded int check lets ``{"line_no": true}`` through and pages
#: from line 1 while looking like a valid cursor.
_KEYSET_TRANSCRIPTS: Dict[str, Tuple[Type[Any], ...]] = {
    "ingested_at": (str,),
    "id": (int,),
}

#: kind -> {field name: accepted python types}. The version key is added
#: to every schema by :func:`_schema_for`, so it cannot be forgotten on a
#: kind added later.
CURSOR_SCHEMAS: Dict[str, Dict[str, Tuple[Type[Any], ...]]] = {
    CURSOR_PROJECTS: {"slug": (str,)},
    CURSOR_TRANSCRIPTS: dict(_KEYSET_TRANSCRIPTS),
    CURSOR_UNATTRIBUTED: dict(_KEYSET_TRANSCRIPTS),
    CURSOR_LINES: {"line_no": (int,)},
    CURSOR_SUBAGENTS: {"appearance_id": (int,)},
    CURSOR_SEARCH: {
        "t_ingested_at": (str,),
        "t_id": (int,),
        "line_no": (int,),
        "scanned": (int,),
        "bytes": (int,),
    },
}

#: Kinds whose payloads are structurally identical, documented in the
#: module docstring. Exposed so a test can assert the set has not grown
#: by accident rather than by decision.
INTERCHANGEABLE_KINDS: Tuple[Tuple[str, str], ...] = (
    (CURSOR_TRANSCRIPTS, CURSOR_UNATTRIBUTED),
)


class CursorError(ValueError):
    """A cursor could not be built or could not be parsed.

    Description: the single failure type this module raises. It exists as
      its own class so a route can catch exactly this and map it to a 400
      ``cannot_determine`` without catching every ValueError raised by
      unrelated code inside the same handler. ``str(exc)`` names which
      part failed and is safe to hand to a client: a cursor carries only
      values the client was already shown, never body text and never a
      secret.
    Inputs: message (str) - what specifically was wrong.
    Output: an exception instance.
    Example: raise CursorError("cursor payload is missing key 'line_no'")
    """


def _schema_for(kind: str) -> Dict[str, Tuple[Type[Any], ...]]:
    """Return the full field schema for a cursor kind, version key included.

    Description: one lookup point, so the version key is present on every
      kind by construction rather than by each schema remembering it.
    Inputs: kind (str) - one of the CURSOR_* constants.
    Output: dict mapping field name to a tuple of accepted types.
    Raises: CursorError - the kind is not a declared cursor kind. This is
      a programming error rather than client input, but it is raised as a
      CursorError so a route's existing handler reports it instead of
      returning a 500 with no explanation.
    Example: _schema_for("lines") -> {"v": (int,), "line_no": (int,)}
    """
    fields = CURSOR_SCHEMAS.get(kind)
    if fields is None:
        raise CursorError(
            f"unknown cursor kind {kind!r}; "
            f"known kinds are {sorted(CURSOR_SCHEMAS)}"
        )
    schema: Dict[str, Tuple[Type[Any], ...]] = {VERSION_KEY: (int,)}
    schema.update(fields)
    return schema


def _check_type(kind: str, key: str, value: Any, accepted: Tuple[Type[Any], ...]) -> None:
    """Assert one payload field holds an accepted type, or raise.

    Description: rejects ``bool`` wherever ``int`` is expected. Python
      makes bool a subclass of int, so ``isinstance(True, int)`` is True
      and an unguarded check would accept ``{"line_no": true}`` as a
      position of 1. That is the quietest possible paging corruption.
    Inputs: kind (str), key (str), value (Any), accepted (tuple of types).
    Output: None.
    Raises: CursorError - wrong type.
    Example: _check_type("lines", "line_no", 5, (int,)) -> None
    """
    if accepted == (int,) and isinstance(value, bool):
        raise CursorError(
            f"{kind} cursor key {key!r} is a boolean; an integer is required"
        )
    if not isinstance(value, accepted):
        names = "/".join(t.__name__ for t in accepted)
        raise CursorError(
            f"{kind} cursor key {key!r} is {type(value).__name__}, "
            f"expected {names}"
        )


def encode_cursor(kind: str, payload: Mapping[str, Any]) -> str:
    """Encode a keyset position as an opaque unpadded base64url string.

    Description: JSON with sorted keys and no whitespace, so the same
      position always produces the same string and a test can compare
      cursors by equality. The payload is validated against the kind's
      schema BEFORE encoding, so this function cannot mint a cursor that
      its own :func:`decode_cursor` would reject - an asymmetry there
      would strand a client on a page it can never leave.
    Inputs: kind (str) - a CURSOR_* constant. payload (Mapping) - the
      position; ``v`` may be omitted and is filled in as
      ``CURSOR_VERSION``.
    Output: str - base64url, unpadded, ASCII.
    Raises: CursorError - unknown kind, missing key, extra key, wrong
      type, or a payload that is not JSON-serializable.
    Example:
        >>> encode_cursor("lines", {"line_no": 2})
        'eyJsaW5lX25vIjoyLCJ2IjoxfQ'
    """
    schema = _schema_for(kind)
    body: Dict[str, Any] = dict(payload)
    body.setdefault(VERSION_KEY, CURSOR_VERSION)
    _validate_payload(kind, body, schema)
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CursorError(f"{kind} cursor payload is not serializable: {exc}") from exc
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _validate_payload(
    kind: str,
    body: Mapping[str, Any],
    schema: Mapping[str, Tuple[Type[Any], ...]],
) -> None:
    """Assert a decoded payload matches a kind's schema exactly, or raise.

    Description: checks the key set BOTH ways. A missing key is obvious.
      An EXTRA key is rejected too, because the only ways one appears are
      a cursor minted by a different version of this API or a client that
      built one by hand, and in both cases the fields this code does
      understand may not mean what it thinks they mean.
    Inputs: kind (str), body (Mapping) - the decoded payload, schema
      (Mapping) - from :func:`_schema_for`.
    Output: None.
    Raises: CursorError - wrong version, missing key, extra key, or a
      field of the wrong type.
    """
    version = body.get(VERSION_KEY)
    if version is None:
        raise CursorError(f"{kind} cursor payload has no {VERSION_KEY!r} key")
    _check_type(kind, VERSION_KEY, version, (int,))
    if version != CURSOR_VERSION:
        raise CursorError(
            f"{kind} cursor is version {version!r}, this server reads "
            f"version {CURSOR_VERSION} only"
        )
    missing = sorted(set(schema) - set(body))
    if missing:
        raise CursorError(f"{kind} cursor payload is missing key(s) {missing}")
    extra = sorted(set(body) - set(schema))
    if extra:
        raise CursorError(
            f"{kind} cursor payload carries unknown key(s) {extra}; "
            f"it was probably minted for a different endpoint or version"
        )
    for key, accepted in schema.items():
        _check_type(kind, key, body[key], accepted)


def decode_cursor(kind: str, raw: str) -> Dict[str, Any]:
    """Parse an opaque cursor into a validated position, or raise.

    Description: raises on ANY defect - a non-string, an empty string,
      bad base64, bytes that are not UTF-8, JSON that is not an object,
      the wrong version, a missing key, an unknown key, or a field of the
      wrong type. There is no recovery path and there must not be one.
      See this module's docstring for why silently restarting at page 1
      is worse than a 400.
    Inputs: kind (str) - a CURSOR_* constant naming which endpoint is
      asking. raw (str) - the cursor exactly as the client sent it.
    Output: dict - the validated payload, including ``v``.
    Raises: CursorError - naming which part failed.
    Example:
        >>> decode_cursor("lines", "eyJsaW5lX25vIjoyLCJ2IjoxfQ")
        {'line_no': 2, 'v': 1}
    """
    schema = _schema_for(kind)
    if not isinstance(raw, str):
        raise CursorError(
            f"{kind} cursor is {type(raw).__name__}, expected a string"
        )
    if raw == "":
        raise CursorError(
            f"{kind} cursor is the empty string; omit the parameter "
            f"entirely to request the first page"
        )
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CursorError(
            f"{kind} cursor did not decode as base64url: {exc}"
        ) from exc
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorError(
            f"{kind} cursor decoded to bytes that are not UTF-8: {exc}"
        ) from exc
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CursorError(f"{kind} cursor is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise CursorError(
            f"{kind} cursor decoded to {type(body).__name__}, expected a JSON object"
        )
    _validate_payload(kind, body, schema)
    return dict(body)
