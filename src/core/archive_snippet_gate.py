"""The gate that decides whether a search PREVIEW may be served.

WHY THIS MODULE EXISTS. ``archive_search`` used to withhold a snippet on
``message_bodies.secret_finding_count > 0``. That is a proxy for "this
body contains a credential", and it was MEASURED wrong on 2026-08-31:
one credential (``value_sha256`` 0236d0f5...) sits in 762 bodies, and
415 of them carry ZERO findings, so 21 of 43 hits in a single
``transcript_id=4`` search returned it in cleartext.

THE ROOT CAUSE IS NOT STALE FINDINGS. Re-scanning the 415 unflagged
bodies with TODAY's detectors produced 0 findings for that credential,
and all 347 flagged bodies still flag - the stored flags are exactly
faithful to the current code. The value is 40 characters with no vendor
marker, so only ``high_entropy_assignment`` can see it, and that
detector is CONTEXTUAL: it needs a name that says "token"/"secret"/"key"
immediately before the value. Measured over every occurrence: all 533
detected ones sit in that assignment context and all 587 occurrences in
unflagged bodies do not. The gap is STRUCTURAL, so no re-scan closes it
and re-running the detectors over the snippet window closes it either -
a window is a subset of the body, and the body already scans clean.

WHAT THIS GATE ACTUALLY GUARANTEES, AND WHAT IT DOES NOT.

  GUARANTEED (hard, not best-effort): a value that this corpus has ever
  detected as a credential ANYWHERE will not appear in a snippet, even
  in a body that carries no finding of its own. That is layer 3, and it
  is what closes the measured defect. It works by hash membership - the
  index holds the sha256 and the length of every known credential and
  NEVER the value, so this module cannot leak what it is protecting.

  BEST EFFORT (stated, not claimed away): a credential this corpus has
  NEVER detected anywhere is not known to layer 3 and is invisible to
  layers 1 and 2 by the same recall limit that caused the defect. No
  gate here can promise otherwise. Callers who need a hard guarantee
  must ask for no snippets at all.

  NEVER SUPPRESSED: withholding a preview never suppresses the hit. The
  transcript, line, offset and length are reported either way.

THE THIRD OUTCOME IS A WITHHOLD. If the index cannot be built the gate
returns ``withheld_gate_unavailable``. "I could not evaluate whether
this is safe" must never render as "this is safe".
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Dict, FrozenSet, Optional, Tuple

from src.core.archive_cursor import CURSOR_LINES, CURSOR_VERSION, encode_cursor
from src.core.archive_read import RESULT_CANNOT_DETERMINE
from src.core.message_model_secrets import scan_text

#: Why a preview was withheld. The state NAMES the layer that tripped,
#: so an operator reading a response can tell a body-level flag from a
#: window-level match from a gate that could not run at all.
SNIPPET_INCLUDED = "included"
SNIPPET_WITHHELD_FLAGGED_BODY = "withheld_secret_bearing"
SNIPPET_WITHHELD_WINDOW_DETECTOR = "withheld_window_detector"
SNIPPET_WITHHELD_KNOWN_VALUE = "withheld_known_secret_value"
SNIPPET_WITHHELD_GATE_UNAVAILABLE = "withheld_gate_unavailable"
SNIPPET_WITHHELD_BY_REQUEST = "withheld_by_request"

#: A credential is a maximal run of these characters in every shape the
#: detectors produce, so only runs need to be tested rather than every
#: substring of the window. This is a COST optimisation with a stated
#: cost: a known value spliced inside a longer run of the same alphabet
#: is still caught (runs are searched at every offset), but one split
#: across a quote or a space is not - it is not the same value then.
_RUN_RE = re.compile(r"[A-Za-z0-9+/=_.\-]+")

#: Fingerprint of the findings table, so the index rebuilds when ingest
#: adds findings and is otherwise reused. COUNT+MAX(id) is enough for
#: append-only rows, and the DATABASE FILE is part of the key because two
#: different corpora can easily share a count - a cache keyed on the
#: numbers alone would serve one corpus's index for another's bodies.
_FINGERPRINT_SQL = "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM message_secret_findings"
_DB_FILE_SQL = "PRAGMA database_list"
_INDEX_SQL = "SELECT DISTINCT value_sha256, match_length FROM message_secret_findings"

_CACHE: Dict[Tuple[str, int, int], "KnownSecretIndex"] = {}


class KnownSecretIndex:
    """Every credential this corpus has detected, as hashes only.

    - ``hashes``: sha256 hex of each distinct detected value.
    - ``lengths``: the distinct match lengths, ascending, so the scan can
      stop as soon as a candidate run is shorter than the next length.

    There is deliberately no field holding a value. The index can answer
    "is this substring a known credential" without ever being a second
    place a credential lives.
    """

    __slots__ = ("hashes", "lengths")

    def __init__(self, hashes: FrozenSet[str], lengths: Tuple[int, ...]) -> None:
        self.hashes = hashes
        self.lengths = lengths

    def contains_known_value(self, window: str) -> bool:
        """Whether the window carries any known credential value.

        Inputs: window (str) - the candidate preview text.
        Output: bool - True when a substring hashes to a known value.
        Example: KnownSecretIndex(frozenset(), ()).contains_known_value("x")
          -> False
        """
        if not self.lengths:
            return False
        for match in _RUN_RE.finditer(window):
            run = match.group(0)
            run_len = len(run)
            for size in self.lengths:
                if size > run_len:
                    break
                for start in range(run_len - size + 1):
                    digest = hashlib.sha256(
                        run[start:start + size].encode("utf-8")
                    ).hexdigest()
                    if digest in self.hashes:
                        return True
        return False


def load_index(conn: sqlite3.Connection) -> Optional[KnownSecretIndex]:
    """Build (or reuse) the known-credential index for this corpus.

    Inputs: conn (sqlite3.Connection, read-only is fine).
    Output: KnownSecretIndex, or None when the index could NOT be built.
      None is a could-not-evaluate and the caller must withhold, never
      serve.
    Example: load_index(conn) is None  # -> True on a corpus with no
      message_secret_findings table
    """
    try:
        count, max_id = conn.execute(_FINGERPRINT_SQL).fetchone()
        main = [r for r in conn.execute(_DB_FILE_SQL) if r[1] == "main"]
        key = (str(main[0][2]) if main else "", int(count), int(max_id))
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        hashes = set()
        lengths = set()
        for value_sha256, match_length in conn.execute(_INDEX_SQL):
            size = int(match_length)
            if size > 0 and value_sha256:
                hashes.add(str(value_sha256))
                lengths.add(size)
        index = KnownSecretIndex(frozenset(hashes), tuple(sorted(lengths)))
        _CACHE.clear()  # one corpus per process; do not grow unbounded
        _CACHE[key] = index
        return index
    except (sqlite3.Error, TypeError, ValueError):
        # Specific, and deliberately swallowed into None: the caller
        # turns None into a WITHHOLD, which is the safe direction.
        # Re-raising would take down a search over a preview decision.
        return None


def _evaluate_window(
    window: str, index: Optional[KnownSecretIndex],
) -> Optional[str]:
    """Decide whether a candidate preview window may be served.

    Description: layer 2 (run the detectors over the window itself, so a
      credential the body-level flag missed for a stale or narrowed scan
      is still caught) then layer 3 (hash membership against every value
      this corpus has ever detected, which is the layer that closes the
      measured defect).
    Inputs: window (str) - the candidate preview. index - from
      :func:`load_index`, or None when it could not be built.
    Output: None when the window may be served, otherwise the
      ``snippet_state`` naming the layer that withheld it.
    Example: _evaluate_window("hi", KnownSecretIndex(frozenset(), ())) is
      None -> True
    """
    if index is None:
        return SNIPPET_WITHHELD_GATE_UNAVAILABLE
    if scan_text(window):
        return SNIPPET_WITHHELD_WINDOW_DETECTOR
    if index.contains_known_value(window):
        return SNIPPET_WITHHELD_KNOWN_VALUE
    return None


def layer_one_state(secret_finding_count: int) -> Optional[str]:
    """Layer 1: a body already flagged secret-bearing is withheld whole.

    Description: named and shared rather than written inline in each
      caller, so the body-flag rule has exactly ONE definition. A second
      copy is how two callers end up with two policies, which is the
      defect this whole module exists to prevent.
    Inputs: secret_finding_count (int) - the body's stored finding count.
    Output: None when this layer permits, otherwise the withhold state.
    Example: layer_one_state(0) is None -> True
    """
    return SNIPPET_WITHHELD_FLAGGED_BODY if secret_finding_count > 0 else None


def evaluate_text_window(
    conn: sqlite3.Connection, body_id: int, window: str,
    index: Optional[KnownSecretIndex],
) -> str:
    """All three gate layers over an arbitrary window of a body's text.

    Description: the composed entry point for any caller holding text
      that came out of a body but did NOT come from the search path's
      offset cut - notably a content-block preview, whose text is a
      substring of the same body and therefore carries the same
      exposure. It reads the body's own finding count for layer 1, so a
      caller that does not already have it cannot skip that layer by
      forgetting to pass it.
    Inputs: conn (sqlite3.Connection), body_id (int), window (str) - the
      candidate preview text, index - from :func:`load_index` or None.
    Output: SNIPPET_INCLUDED when the window may be served, otherwise the
      state naming the layer that withheld it, or RESULT_CANNOT_DETERMINE
      when the body row could not be read at all.
    Example: evaluate_text_window(conn, 1, "hi", None)
      -> "withheld_gate_unavailable"
    """
    row = conn.execute(
        "SELECT secret_finding_count FROM message_bodies WHERE id = ?",
        (body_id,),
    ).fetchone()
    if row is None:
        return RESULT_CANNOT_DETERMINE
    flagged = layer_one_state(int(row[0]))
    if flagged is not None:
        return flagged
    withheld = _evaluate_window(window, index)
    return withheld if withheld is not None else SNIPPET_INCLUDED


def snippet_gate_meta(index: Optional[KnownSecretIndex]) -> Dict[str, object]:
    """The gate's own declaration, for ``meta`` in the response envelope.

    Description: the response says what the gate checked and says out
      loud that it is best effort against an UNDETECTED credential. A
      guarantee nobody can keep is worse than a stated limitation.
    Inputs: index - from :func:`load_index`, or None.
    Output: dict for ``meta.snippet_gate``.
    Example: snippet_gate_meta(None)["known_values_indexed"] -> None
    """
    return {
        "guarantee": "best_effort",
        "layers": [
            "body_secret_finding_count",
            "detectors_over_window",
            "known_credential_value_hash",
        ],
        "known_values_indexed": None if index is None else len(index.hashes),
        "limitation": (
            "A credential never detected anywhere in this corpus is not in "
            "the known-value index and is invisible to the detectors for the "
            "same reason it was never detected. Pass snippets=false for a "
            "response that carries no preview text at all."
        ),
        "withholding_never_suppresses_a_hit": True,
    }


# --- The preview path: cut the window, gate it, render the hit --------
SNIPPET_CONTEXT_CHARS = 60
BODIES_HREF = "/api/v1/archive/bodies/{body_id}"
LINES_HREF = "/api/v1/archive/transcripts/{transcript_id}/lines?cursor={cursor}"

#: A SECOND query, issued ONLY for a body layer 1 cleared; SUBSTR cuts
#: inside SQLite so a 60 KB body is never transferred here.
_SNIPPET_SQL = """
SELECT SUBSTR(body_json, :start_1based, :length) AS window,
       LENGTH(body_json) AS total_chars
  FROM message_bodies WHERE id = :body_id
"""


def _snippet_for(
    conn: sqlite3.Connection, body_id: int, match_offset: int,
    match_length: int, index: Optional[KnownSecretIndex],
) -> Tuple[Optional[str], str]:
    """Cut a context window around a match, inside SQLite, then GATE it.

    The window must be read to be cleared; it is dropped unreturned and
    never logged when the gate trips. Layer 1 already ran in build_hit.

    Inputs: conn, body_id, match_offset (0-based), match_length, index.
    Output: (snippet, snippet_state); ``(None, "cannot_determine")`` if
      the row vanished between the two queries - not an empty preview;
      ``(None, "withheld_*")`` when the gate refused it.
    """
    start = max(0, match_offset - SNIPPET_CONTEXT_CHARS)
    end = match_offset + match_length + SNIPPET_CONTEXT_CHARS
    row = conn.execute(_SNIPPET_SQL, {
        "start_1based": start + 1, "length": end - start, "body_id": body_id,
    }).fetchone()
    if row is None:
        return None, RESULT_CANNOT_DETERMINE
    withheld = _evaluate_window(row["window"], index)
    if withheld is not None:
        return None, withheld
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < int(row["total_chars"]) else ""
    return f"{prefix}{row['window']}{suffix}", SNIPPET_INCLUDED


def build_hit(
    conn: sqlite3.Connection, trow: sqlite3.Row, row: sqlite3.Row, q: str,
    index: Optional[KnownSecretIndex], snippets: bool,
) -> Dict[str, Any]:
    """Render one match. A secret-bearing hit is REPORTED, not dropped.

    Inputs: conn, trow, row (a ``_HIT_SQL`` row), q, index, snippets
      (False suppresses every preview - the hard guarantee). Output: the
      spec 6.11 hit; ``snippet`` None and a ``withheld_*`` state when any
      layer refused, every other field still present.
    """
    secret_count = int(row["secret_finding_count"])
    body_id = int(row["body_id"])
    offset = int(row["match_offset"])
    if not snippets:
        snippet, state = None, SNIPPET_WITHHELD_BY_REQUEST
    elif layer_one_state(secret_count) is not None:
        snippet, state = None, SNIPPET_WITHHELD_FLAGGED_BODY
    else:
        snippet, state = _snippet_for(conn, body_id, offset, len(q), index)
    line_no = int(row["line_no"])
    href_cursor = encode_cursor(
        CURSOR_LINES, {"v": CURSOR_VERSION, "line_no": line_no - 1})
    return {
        "transcript_id": int(trow["id"]), "session_ref": trow["session_ref"],
        "line_no": line_no, "body_id": body_id, "match_offset": offset,
        # body_chars is the truthful name; body_bytes is the same number.
        "match_length": len(q), "body_chars": int(row["body_bytes"]),
        "body_bytes": int(row["body_bytes"]),
        "secret_finding_count": secret_count, "snippet": snippet,
        "snippet_state": state,
        "body_href": BODIES_HREF.format(body_id=body_id),
        "lines_href": LINES_HREF.format(
            transcript_id=int(trow["id"]), cursor=href_cursor),
    }
