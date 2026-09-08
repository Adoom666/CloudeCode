"""Splitting a transcript line into identity + envelope, and putting it
back together byte-for-byte.

THE ONE THING THIS MODULE MUST NEVER GET WRONG. Nothing else in the
message model matters if a stored record cannot be turned back into the
exact bytes it arrived as. Every function here is pure and every claim it
makes is checkable against a sha256 of the original line - there is no
path where a record is called reconstructed without that comparison
actually running (see ``src/core/message_model_export.py``).

WHY A LINE CAN BE REBUILT AT ALL. Claude Code writes each JSONL line with
one serializer, and a 2026-08-11 audit found 100.0000% of 134,464 sampled
lines regenerate byte-exact from their parsed JSON using one of two
styles. Re-measured here on 2026-08-29 against 20,000 raw lines drawn
from the live claude_history database: 20,000 of 20,000 reproduced with
``compact`` and zero needed anything else. So the model stores a style
MARKER plus a hash, not a second copy of the bytes.

WHY THE STYLE LIST HAS FOUR ENTRIES AND NOT TWO. The two measured styles
differ in separators. ``ensure_ascii`` is a second, independent axis that
the separator measurement did not isolate: Python's ``json.dumps``
escapes non-ASCII by default and JavaScript's ``JSON.stringify`` does
not, so a corpus containing any non-ASCII character would fail to
reproduce under the wrong setting for a reason that has nothing to do
with separators. Enumerating both axes costs four cheap string
comparisons at ingest and removes a whole class of "it reproduced for
ASCII files and not for the others" from ever happening.

THE THIRD OUTCOME IS BUILT IN. :func:`detect_style` returns None when no
style reproduces the line, and that is a NAMED result the caller must
handle (by storing the raw line and raising the fidelity gate condition),
not an exception to swallow and not a default style to guess at.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: (name, separators, ensure_ascii). Order matters: :func:`detect_style`
#: returns the FIRST style that reproduces the line, and ``compact`` is
#: first because it accounted for 20,000 of 20,000 measured lines.
SERIALIZER_STYLES: Tuple[Tuple[str, Tuple[str, str], bool], ...] = (
    ("compact", (",", ":"), False),
    ("compact_ascii", (",", ":"), True),
    ("spaced", (", ", ": "), False),
    ("spaced_ascii", (", ", ": "), True),
)

STYLE_NAMES: Tuple[str, ...] = tuple(name for name, _, _ in SERIALIZER_STYLES)

_STYLE_BY_NAME: Dict[str, Tuple[Tuple[str, str], bool]] = {
    name: (seps, ensure_ascii) for name, seps, ensure_ascii in SERIALIZER_STYLES
}

#: The top-level keys measured to vary between two copies of the SAME
#: message uuid. A raw-JSON diff of one such pair (2026-08-29) found 12
#: keys byte-identical - message, sessionId, uuid, timestamp, parentUuid,
#: cwd, gitBranch, slug, permissionMode, type, userType, version - and
#: exactly these two differing. They are therefore per-APPEARANCE facts,
#: not part of the message's identity, and the difference between two
#: copies of one message is precisely the parent/child relationship this
#: pair encodes.
#:
#: This set is deliberately CLOSED and small. A key outside it differing
#: between two copies is not quietly absorbed into the envelope - it makes
#: the two copies two distinct bodies, which is what
#: GATE_DUPLICATE_UUID_BODY_CONFLICT reports.
APPEARANCE_KEYS: Tuple[str, ...] = ("isSidechain", "agentId")

#: Sentinel for a valid JSON line whose top-level value is not an object
#: (a bare string, list or number). There is no envelope to split out of
#: one, so its key order is recorded as this rather than as an empty list,
#: which would be indistinguishable from "an object with no keys".
KEY_ORDER_NOT_AN_OBJECT: str = "__not_an_object__"

#: The two compaction markers, and the two values the compact_subtype
#: lookup can hold. MEASURED against the live corpus on 2026-08-29:
#: exactly two non-null values across 3,004,324 rows - ``compact_boundary``
#: on 938 rows (a ``type: system`` record whose ``subtype`` says so) and
#: ``isCompactSummary`` on 944 (a ``type: user`` record carrying that
#: boolean). They are encoded in two DIFFERENT JSON shapes, which is why
#: this is a small branch and not one dict lookup: reading ``subtype``
#: alone would have interned every unrelated system subtype into a column
#: named for compaction, and reading ``isCompactSummary`` alone would have
#: missed the boundary record entirely.
COMPACT_SUBTYPE_BOUNDARY: str = "compact_boundary"
COMPACT_SUBTYPE_SUMMARY: str = "isCompactSummary"


def sha256_text(text: str) -> str:
    """Hash a string's UTF-8 bytes.

    Description: the single hashing entry point for this model, so a
      line hash written at ingest and a line hash computed at export can
      never disagree about encoding.
    Inputs: text (str).
    Output: str - lowercase hex sha256 digest.
    Example: sha256_text("a")[:8] -> "ca978112"
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_with_style(value: Any, style: str) -> str:
    """Serialize a parsed JSON value using one named style.

    Description: the only place ``json.dumps`` is called with formatting
      options in this model, so every render site is guaranteed to use the
      same option set for a given style name.
    Inputs: value (any JSON-serializable value), style (str - one of
      STYLE_NAMES).
    Output: str.
    Raises: KeyError - the style name is not registered.
    Example: render_with_style({"a": 1}, "compact") -> '{"a":1}'
    """
    separators, ensure_ascii = _STYLE_BY_NAME[style]
    return json.dumps(value, separators=separators, ensure_ascii=ensure_ascii)


def detect_style(value: Any, original: str) -> Optional[str]:
    """Find which style, if any, reproduces ``original`` exactly.

    Description: tries each registered style in order and returns the
      first whose output is byte-identical to the original line. Returns
      None when none of them do - the third outcome, which the caller
      must handle by storing the raw line rather than by picking a style
      that does not actually work.
    Inputs: value (the parsed JSON value), original (str - the line's
      exact text, without its trailing newline).
    Output: str (a style name) or None.
    Example: detect_style({"a": 1}, '{"a":1}') -> "compact"
    """
    for name in STYLE_NAMES:
        if render_with_style(value, name) == original:
            return name
    return None


def canonical_json(value: Any) -> str:
    """Render a value in a canonical, order-insensitive form.

    Description: used ONLY to compute the body hash, never to reproduce a
      line. Sorting keys means two copies of a message that differ only in
      top-level key order hash the same and share one identity row, while
      each copy's own key order is preserved on its own appearance row so
      both still reconstruct byte-exactly.
    Inputs: value (any JSON-serializable value).
    Output: str.
    Example: canonical_json({"b": 1, "a": 2}) -> '{"a":2,"b":1}'
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def stored_body_json(value: Any) -> str:
    """Render a body for storage, PRESERVING key order at every depth.

    Description: what goes into ``message_bodies.body_json``. Order
      preserving because ``json.loads`` keeps insertion order, so a body
      stored this way parses back with its original key order intact and
      the appearance's own top-level key order can then be applied on top
      - which together is what makes reconstruction byte-exact for nested
      objects, not just for the top level.
    Inputs: value (any JSON-serializable value).
    Output: str.
    Example: stored_body_json({"b": 1, "a": 2}) -> '{"b":1,"a":2}'
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class SplitRecord:
    """One transcript line, split into the parts the model stores apart.

    - ``body``: the record with APPEARANCE_KEYS removed, or the whole
      value when the line's top-level JSON is not an object.
    - ``envelope``: the removed APPEARANCE_KEYS and their values. Empty
      when the line carried none of them (which is normal: a main-session
      message often has no ``agentId``).
    - ``key_order``: the ORIGINAL top-level key order of the object, all
      keys, envelope ones included - this is what makes reassembly
      byte-exact. KEY_ORDER_NOT_AN_OBJECT for a non-object line.
    - ``body_sha256``: hash of ``canonical_json(body)`` - ORDER
      INSENSITIVE, and therefore the right hash to ask "are these two
      copies the same message?" with.
    - ``body_bytes_sha256``: hash of ``stored_body_json(body)`` - ORDER
      SENSITIVE, and therefore the right hash to key STORAGE on, because
      a nested object's key order is part of what has to come back
      byte-exactly and two orderings cannot share one stored body.

    TWO HASHES, ON PURPOSE, AND THE FIRST ATTEMPT AT THIS HAD ONE. Using
    the canonical hash for both meant a nested object was stored with its
    keys SORTED, so a record whose ``message`` object was written
    role/model/content came back content/model/role - valid JSON, same
    meaning, different bytes, and export failed. Using the byte hash for
    both would have made a pure key-order difference look like two
    different messages under one uuid, which is the conflict condition
    and would have reported thousands of non-events. Identity of STORAGE
    and identity of MEANING are two questions; they get two hashes.
    """

    body: Any
    envelope: Dict[str, Any]
    key_order: Any
    body_sha256: str
    body_bytes_sha256: str


def split_record(value: Any) -> SplitRecord:
    """Split one parsed transcript line into identity body + envelope.

    Description: pure structural split, no interpretation of the values.
      A non-object top-level JSON value (a bare string or list - rare but
      legal, and this model refuses to drop a line for being unusual) is
      carried whole as the body with no envelope.
    Inputs: value (the parsed JSON value of one line).
    Output: SplitRecord.
    Example: split_record({"uuid": "u", "isSidechain": True}).envelope
      -> {"isSidechain": True}
    """
    if not isinstance(value, dict):
        return SplitRecord(
            body=value, envelope={}, key_order=KEY_ORDER_NOT_AN_OBJECT,
            body_sha256=sha256_text(canonical_json(value)),
            body_bytes_sha256=sha256_text(stored_body_json(value)),
        )
    body: Dict[str, Any] = {}
    envelope: Dict[str, Any] = {}
    for key, item in value.items():
        if key in APPEARANCE_KEYS:
            envelope[key] = item
        else:
            body[key] = item
    return SplitRecord(
        body=body, envelope=envelope, key_order=list(value.keys()),
        body_sha256=sha256_text(canonical_json(body)),
        body_bytes_sha256=sha256_text(stored_body_json(body)),
    )


def reassemble(body: Any, envelope: Dict[str, Any], key_order: Any) -> Any:
    """Put a split record back together in its original key order.

    Description: the inverse of :func:`split_record`. Iterating
      ``key_order`` rather than merging two dicts is what preserves the
      original interleaving - an envelope key that sat in the middle of
      the object goes back to the middle, not to the end. A key named in
      ``key_order`` but present in neither part is a corrupted stored row,
      and raises rather than being skipped, because silently emitting a
      shorter object would produce a line that fails its own hash check
      with no explanation of why.
    Inputs: body (the identity part), envelope (dict of APPEARANCE_KEYS),
      key_order (list of str, or KEY_ORDER_NOT_AN_OBJECT).
    Output: the reassembled JSON value.
    Raises: KeyError - key_order names a key neither part holds.
    Example: reassemble({"uuid": "u"}, {"isSidechain": True},
      ["isSidechain", "uuid"]) -> {"isSidechain": True, "uuid": "u"}
    """
    if key_order == KEY_ORDER_NOT_AN_OBJECT:
        return body
    out: Dict[str, Any] = {}
    for key in key_order:
        if key in envelope:
            out[key] = envelope[key]
        elif isinstance(body, dict) and key in body:
            out[key] = body[key]
        else:
            raise KeyError(
                f"key_order names {key!r} but neither the stored body nor "
                "the stored envelope holds it - the stored row is "
                "incomplete, not merely out of order"
            )
    return out


def render_line(
    body: Any, envelope: Dict[str, Any], key_order: Any, style: str,
) -> str:
    """Reassemble and serialize one line back to its original text.

    Description: the single call an exporter makes per line. Does NOT
      verify the result - verification is the caller's job and is done
      against the stored line hash, so that this function can never
      report a success it did not measure.
    Inputs: body, envelope, key_order (as for :func:`reassemble`), style
      (str - one of STYLE_NAMES).
    Output: str - the line's text, without a trailing newline.
    Example: render_line({"a": 1}, {}, ["a"], "compact") -> '{"a":1}'
    """
    return render_with_style(reassemble(body, envelope, key_order), style)


def identity_key(message_uuid: Optional[str], body_sha256: str) -> str:
    """Build the stored primary identity string for a message body.

    Description: ``message_bodies.identity_key`` is stored rather than
      expressed as UNIQUE (message_uuid, body_sha256) because SQLite
      treats NULLs in a unique index as DISTINCT, so the composite index
      would let unlimited duplicate rows through for the uuid-less
      records this corpus genuinely contains. An explicit string has no
      NULL in it and therefore no exemption.
    Inputs: message_uuid (str or None), body_sha256 (str - the ORDER
      SENSITIVE ``body_bytes_sha256``, because two orderings of one body
      are two stored rows even though they are one message).
    Output: str.
    Example: identity_key(None, "ab") -> ":ab"
    """
    return f"{message_uuid or ''}:{body_sha256}"


def parse_line(text: str) -> Tuple[str, Any]:
    """Classify one line's text as blank, invalid JSON, or parsed.

    Description: mirrors ``transcript_archive._parse_one_line``'s
      vocabulary deliberately, so a line classified 'invalid_json' by the
      archive is classified the same way here - two different verdicts on
      one line from two modules in the same repo would be its own defect.
    Inputs: text (str) - one line's text, without its trailing newline.
    Output: (status, value) where status is 'blank', 'invalid_json' or
      'ok'. ``value`` is the parsed JSON value only when status is 'ok'.
    Example: parse_line("   ") -> ("blank", None)
    """
    if text.strip() == "":
        return "blank", None
    try:
        return "ok", json.loads(text)
    except json.JSONDecodeError:
        return "invalid_json", None


def scalar_fields(body: Any) -> Dict[str, Any]:
    """Pull the queryable scalars out of an identity body.

    Description: the fields promoted into their own columns so a query
      does not have to parse ``body_json``. ``role`` and ``model`` live
      inside the nested ``message`` object rather than at the top level,
      which is why this is a function and not four dict lookups at the
      call site. Every value is returned as-is or None - never coerced,
      never defaulted, because a missing field and a field whose value is
      falsy are different facts.
    Inputs: body (the identity body, normally a dict).
    Output: dict with keys record_type, role, model, compact_subtype,
      parent_uuid, ts, origin_session_ref, message_uuid,
      is_compact_boundary.
    Example: scalar_fields({"type": "user", "uuid": "u"})["record_type"]
      -> "user"
    """
    empty = {
        "record_type": None, "role": None, "model": None,
        "compact_subtype": None, "parent_uuid": None, "ts": None,
        "origin_session_ref": None, "message_uuid": None,
        "is_compact_boundary": 0,
    }
    if not isinstance(body, dict):
        return empty
    inner = body.get("message")
    inner = inner if isinstance(inner, dict) else {}

    def _s(value: Any) -> Optional[str]:
        return value if isinstance(value, str) else None

    subtype = _s(body.get("subtype"))
    if subtype == COMPACT_SUBTYPE_BOUNDARY:
        compact_subtype = COMPACT_SUBTYPE_BOUNDARY
    elif body.get("isCompactSummary") is True:
        compact_subtype = COMPACT_SUBTYPE_SUMMARY
    else:
        compact_subtype = None
    return {
        "record_type": _s(body.get("type")),
        "role": _s(inner.get("role")),
        "model": _s(inner.get("model")),
        "compact_subtype": compact_subtype,
        "parent_uuid": _s(body.get("parentUuid")),
        "ts": _s(body.get("timestamp")),
        "origin_session_ref": _s(body.get("sessionId")),
        "message_uuid": _s(body.get("uuid")),
        "is_compact_boundary": 1 if compact_subtype else 0,
    }


#: The prefixes a subagent session id can carry. MEASURED, not assumed,
#: and the measurement corrected the brief this was built from: that
#: brief named only ``agent-``, and a direct count of the live sessions
#: table on 2026-08-29 found ``agent:`` on 17,996 rows and ``agent-`` on
#: 224. The rarer form is the one that was documented. Both are
#: legitimate, and a checker that knew only ``agent-`` would have
#: classified 17,996 subagent sessions as malformed uuids - the exact
#: shape of "an assumed column value" this repo's hazard list warns
#: about, where the guess is usually right and therefore invisible.
AGENT_REF_PREFIXES: Tuple[str, ...] = ("agent:", "agent-")

#: The canonical 8-4-4-4-12 hex form, anchored at both ends. Case is
#: accepted in either direction because RFC 4122 says a reader must, and
#: a direct count of the live corpus on 2026-09-02 found 1,432 uuid-scheme
#: refs of which zero carry an upper-case digit - so accepting upper case
#: costs nothing today and refuses to manufacture a false OPAQUE_SCHEME
#: the day a differently-cased id arrives.
#:
#: DELIBERATELY NOT ``uuid.UUID()``. That constructor also accepts braces,
#: a ``urn:uuid:`` prefix and the 32-character undashed form, none of which
#: this corpus produces; admitting them would let a ref that is plainly not
#: one of this corpus's session ids pass as one. It is also deliberately
#: version-agnostic: requiring a version-4 nibble would be an assumption
#: about how ids are minted, and this classifier's whole job is to stop
#: assuming shapes it never measured.
_CANONICAL_UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

#: The scheme names this classifier can return, and the exact set the
#: ``message_transcripts.session_ref_scheme`` CHECK constraint permits.
UUID_SCHEME: str = "uuid"
AGENT_SCHEME: str = "agent"

#: THE THIRD OUTCOME. A ref that carries neither an agent prefix nor a
#: well-formed uuid: the string is present and readable, it simply
#: declares no identity scheme this corpus recognises. That is a
#: MEASUREMENT of the ref, not a bucket for anything unrecognised and not
#: a "could not evaluate" - the classifier looked at the whole string and
#: found it matched neither pattern.
#:
#: 'opaque' rather than 'unknown' precisely because 'unknown' reads as
#: "nobody measured this", which is the third state of a different
#: question and the one value this must never be confused with.
OPAQUE_SCHEME: str = "opaque"

SESSION_REF_SCHEMES: Tuple[str, ...] = (UUID_SCHEME, AGENT_SCHEME, OPAQUE_SCHEME)


def session_ref_scheme(session_ref: str) -> str:
    """Say which session identity scheme a ref uses, or that it uses none.

    Description: the corpus names sessions either by uuid or by one of
      the AGENT_REF_PREFIXES forms used for subagent sessions. Both are
      valid; an agent id is NOT a malformed uuid and must never be
      repaired into one.

      THREE OUTCOMES, NOT TWO. This used to return 'uuid' BY ELIMINATION
      for anything without an agent prefix, so a ref that was neither -
      a literal 'audit' or 'journal' derived from a filename stem, which
      is what 19 rows of the live corpus carried on 2026-09-02 - was
      recorded as a uuid and counted in the owner's own sessions. Five of
      those were local-agent permission-and-decision audit trails, not
      conversations at all. The uuid answer is now MEASURED against
      _CANONICAL_UUID rather than inferred from the absence of a prefix,
      and a ref matching neither returns OPAQUE_SCHEME.
    Inputs: session_ref (str) - the ref exactly as the corpus holds it.
    Output: str - one of SESSION_REF_SCHEMES.
    Example: session_ref_scheme("agent:a7b0a2e") -> "agent"
      session_ref_scheme("audit") -> "opaque"
    """
    if any(session_ref.startswith(p) for p in AGENT_REF_PREFIXES):
        return AGENT_SCHEME
    if _CANONICAL_UUID.match(session_ref):
        return UUID_SCHEME
    return OPAQUE_SCHEME


def split_lines(text: str) -> Tuple[List[str], bool]:
    """Split a transcript's text into lines plus a trailing-newline flag.

    Description: a trailing newline terminates the last line rather than
      creating an empty one after it, but a genuinely blank line in the
      middle (or a deliberate blank final line, i.e. two newlines) IS a
      line and is kept - it is real content the source file held, and
      dropping it would make export short by a byte.
    Inputs: text (str) - the whole transcript's text.
    Output: (lines, has_trailing_newline).
    Example: split_lines("a\\nb\\n") -> (["a", "b"], True)
    """
    if text == "":
        return [], False
    has_trailing = text.endswith("\n")
    body = text[:-1] if has_trailing else text
    return body.split("\n"), has_trailing


def join_lines(lines: Sequence[str], has_trailing_newline: bool) -> str:
    """Join lines back into a transcript's exact text.

    Description: exact inverse of :func:`split_lines`.
    Inputs: lines (sequence of str), has_trailing_newline (bool).
    Output: str.
    Example: join_lines(["a", "b"], True) -> "a\\nb\\n"
    """
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if has_trailing_newline else "")
