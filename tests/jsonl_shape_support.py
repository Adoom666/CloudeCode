"""Shared machinery for the JSONL shape suite: corpus access, the shape
signature recipe, and the could-not-evaluate ledger.

WHY THIS MODULE EXISTS AT ALL. Four test modules need the same three
things - a read-only handle on a corpus that may not be present, the
signature recipe that turns a live row into a shape id, and one honest
way to say "I could not check this". Three copies of any of those would
drift, and a drifted signature recipe fails in the worst direction: it
computes ids nobody has ever seen and reports them as drift, or worse,
computes an id that collides with a known one and reports novelty as
normal.

THE SIGNATURE RECIPE IS A RE-IMPLEMENTATION ON PURPOSE. The manifest at
``tests/fixtures/jsonl_shape_manifest.json`` was built by scripts that
live OUTSIDE this repo (see the census document's "Reproducing this"
section). Code the repo cannot see is code the repo cannot defend, so
:func:`signature_for_row` re-derives the recipe here from the manifest's
own ``signature_definition`` block. That is not duplication for its own
sake - it is the only way the recipe becomes testable, and
:func:`recompute_matches_manifest` in the drift suite proves the
re-implementation agrees with the file by recomputing real exemplars and
comparing ids. A recipe that agreed with nothing would be caught there.

THE THIRD OUTCOME IS A LEDGER, NOT A SKIP. A corpus-dependent test that
finds no corpus must not read as a pass. ``pytest.skip`` alone renders as
a quiet 's' that a green summary swallows, which is precisely the
false-green this suite exists to prevent. So every corpus-dependent
module calls :func:`record_not_evaluated` before skipping, and the
terminal-summary hook in ``tests/conftest.py`` prints a loud named block
at the end of EVERY run saying exactly which guarantees went unverified.
The skip still happens; it just can no longer happen silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Environment variable naming the corpus to verify against. The default
#: below is the known development corpus; CI has neither and is expected
#: to report could-not-evaluate rather than to pass quietly.
CORPUS_DB_ENV_VAR: str = "CLOUDE_FIDELITY_DB"

DEFAULT_CORPUS_DB: str = (
    "/Users/jsugamele/Scratch/llmScratch/cc-dev-state/cloude.db"
)

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

MANIFEST_PATH: Path = REPO_ROOT / "tests" / "fixtures" / "jsonl_shape_manifest.json"

#: The manifest format this module knows how to read. A manifest written
#: to a later format is refused rather than parsed optimistically, because
#: a field that moved would be read as absent and absence is scored as a
#: dimension value.
SUPPORTED_MANIFEST_SCHEMA_VERSION: int = 1

#: Truncation lengths from the manifest's own ``signature_definition``.
#: Named because a bare 24 and a bare 16 four lines apart are exactly the
#: two magic numbers most likely to be swapped by accident.
SIGNATURE_ID_HEX_CHARS: int = 24
KEY_ORDER_DIGEST_HEX_CHARS: int = 16

#: What the key-order digest hashes when ``key_order_json`` is NULL. The
#: NUL prefix is what keeps it from colliding with a real JSON string.
KEY_ORDER_NULL_SENTINEL: str = "\x00<NULL>"

#: Stand-ins the recipe uses for absent structure. They are strings and
#: not None so that "this column was NULL" and "this dimension does not
#: apply" stay distinguishable in the hashed dict.
NULL_TOKEN: str = "<NULL>"
UNPARSEABLE_TOKEN: str = "<UNPARSEABLE>"
NO_BODY_TOKEN: str = "<no body>"
NO_MESSAGE_TOKEN: str = "<no message obj>"
ABSENT_TOKEN: str = "<absent>"
NON_STRING_TOKEN: str = "<non-str>"
NON_STRING_TYPE_TOKEN: str = "<non-str type>"

#: The agent-id prefixes the census characterised. Anything else is
#: 'other'; a NULL agent id is its own token.
AGENT_ID_PREFIXES: Tuple[str, ...] = ("agent:", "agent-")

#: Body dimensions, in the manifest's declared order. Held as a constant
#: because :func:`body_dimensions` must emit every one of them even when
#: there is no body row, and a missing key would silently change the id.
BODY_DIMENSION_NAMES: Tuple[str, ...] = (
    "record_type", "role", "model", "compact_subtype", "is_compact_boundary",
    "parent_uuid", "ts", "message_uuid", "content_shape",
    "content_block_types", "usage_shape", "stop_reason", "body_toplevel_type",
)


# ---- the could-not-evaluate ledger -------------------------------------

#: Module-level ledger of guarantees this run did NOT verify. Read by the
#: terminal-summary hook in tests/conftest.py. A list rather than a set so
#: the order reported is the order encountered.
_NOT_EVALUATED: List[Tuple[str, str]] = []


def record_not_evaluated(guarantee: str, reason: str) -> str:
    """Record that a named guarantee went unverified, and say so loudly.

    Description: the single entry point for the third outcome. Callers
      pass the result straight to ``pytest.skip`` so the reason string
      and the ledger entry can never disagree about what was missed.
    Inputs: guarantee (str - what was NOT proven, in plain words),
      reason (str - why it could not be proven, naming the missing
      input).
    Output: str - the full skip reason, prefixed COULD-NOT-EVALUATE.
    Example: record_not_evaluated("x", "no db") ->
      "COULD-NOT-EVALUATE: x - no db"
    """
    message = f"COULD-NOT-EVALUATE: {guarantee} - {reason}"
    _NOT_EVALUATED.append((guarantee, reason))
    return message


def not_evaluated_entries() -> List[Tuple[str, str]]:
    """Every guarantee recorded as unverified so far in this run.

    Inputs: none.
    Output: list[tuple[str, str]] - (guarantee, reason) pairs.
    Example: not_evaluated_entries() -> []
    """
    return list(_NOT_EVALUATED)


# ---- corpus access ------------------------------------------------------

def corpus_path() -> Optional[str]:
    """Resolve the corpus to verify against, or None when there is none.

    Description: the env var wins when set, so a machine with a corpus
      somewhere else needs no code change; the development default is
      used only when it actually exists on disk. An env var pointing at a
      missing file returns None like any other absence - it does not
      silently fall back to the default, because a caller who named a
      path meant that path.
    Inputs: none (reads CORPUS_DB_ENV_VAR).
    Output: str (an existing file path) or None.
    Example: corpus_path() -> "/path/to/cloude.db"
    """
    override = os.environ.get(CORPUS_DB_ENV_VAR)
    if override:
        return override if Path(override).is_file() else None
    return DEFAULT_CORPUS_DB if Path(DEFAULT_CORPUS_DB).is_file() else None


def open_corpus_readonly(path: str) -> sqlite3.Connection:
    """Open the corpus read-only and PROVE the read-only guard took.

    Description: opens through a ``mode=ro`` URI, sets
      ``PRAGMA query_only=ON``, then READS THE PRAGMA BACK and refuses to
      return a handle unless it reads 1. A pragma that silently did not
      take is a false green on the one property that matters here, which
      is that a test run can never damage the user's live archive. The
      readback is the measurement; setting the pragma is only a request.
    Inputs: path (str) - an existing SQLite file.
    Output: sqlite3.Connection - read-only, proven.
    Raises: RuntimeError - query_only did not read back as 1.
    Example: open_corpus_readonly("/tmp/x.db").execute("SELECT 1")
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    got = conn.execute("PRAGMA query_only").fetchone()[0]
    if got != 1:
        conn.close()
        raise RuntimeError(
            f"REFUSING to read {path}: PRAGMA query_only read back as {got!r}, "
            "not 1, so this connection is not provably read-only"
        )
    return conn


def load_manifest() -> Dict[str, Any]:
    """Load and version-check the shape manifest.

    Description: refuses a manifest whose schema_version this module was
      not written against, rather than reading a moved field as absent.
    Inputs: none.
    Output: dict - the parsed manifest.
    Raises: RuntimeError - unsupported manifest schema_version.
    Example: load_manifest()["totals"]["distinct_signatures"] -> 1347
    """
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    found = manifest.get("schema_version")
    if found != SUPPORTED_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"manifest schema_version is {found!r}, but this module reads "
            f"only v{SUPPORTED_MANIFEST_SCHEMA_VERSION}. Re-read "
            "signature_definition before changing this number."
        )
    return manifest


# ---- the signature recipe ----------------------------------------------

def _classify_json(value: Any) -> str:
    """Name a parsed JSON value's type using the census's vocabulary.

    Description: bool is checked before the numeric branch because a
      Python bool IS an int, and letting it fall through would score
      ``true`` as a number and change the signature of every body
      carrying one.
    Inputs: value (any parsed JSON value).
    Output: str - one of null, bool, string, number, array, object,
      other.
    Example: _classify_json(True) -> "bool"
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def agent_id_form(agent_id: Optional[str]) -> str:
    """Characterise an agent id structurally, never by its value.

    Description: the census records agent ids by prefix only, so no id
      text ever reaches a signature or a test report.
    Inputs: agent_id (str or None).
    Output: str - 'agent:', 'agent-', 'other', or NULL_TOKEN.
    Example: agent_id_form(None) -> "<NULL>"
    """
    if agent_id is None:
        return NULL_TOKEN
    for prefix in AGENT_ID_PREFIXES:
        if agent_id.startswith(prefix):
            return prefix
    return "other"


def key_order_digest(key_order_json: Optional[str]) -> str:
    """Digest a stored key ordering into a short stable token.

    Description: 447 orderings would bloat every signature dict if
      carried whole, so the ordering is hashed. A NULL ordering hashes a
      sentinel rather than the empty string, which an object with no keys
      would also produce.
    Inputs: key_order_json (str or None) - the stored column verbatim.
    Output: str - KEY_ORDER_DIGEST_HEX_CHARS hex characters.
    Example: key_order_digest(None)[:4] -> "d6b4"
    """
    text = KEY_ORDER_NULL_SENTINEL if key_order_json is None else key_order_json
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:KEY_ORDER_DIGEST_HEX_CHARS]


def _key_order_len(key_order_json: Optional[str]) -> Optional[int]:
    """Count the keys in a stored ordering, or None when there is no list.

    Description: a non-object line records the sentinel string rather
      than a list, and has no key count; NULL has none either. Both are
      None here, and they stay distinguishable through the digest.
    Inputs: key_order_json (str or None).
    Output: int or None.
    Example: _key_order_len('["a"]') -> 1
    """
    if key_order_json is None:
        return None
    try:
        parsed = json.loads(key_order_json)
    except json.JSONDecodeError:
        return None
    return len(parsed) if isinstance(parsed, list) else None


def _envelope_keys(envelope_json: Optional[str]) -> Any:
    """Extract an envelope's key SET, never its values.

    Description: values are message content and must not enter a
      signature or a test report. An envelope that will not parse is
      named as such rather than treated as empty, because "no keys" and
      "unreadable" are different facts.
    Inputs: envelope_json (str or None).
    Output: list[str] sorted, or NULL_TOKEN, or [UNPARSEABLE_TOKEN].
    Example: _envelope_keys('{"b":1,"a":2}') -> ["a", "b"]
    """
    if envelope_json is None:
        return NULL_TOKEN
    try:
        parsed = json.loads(envelope_json)
    except json.JSONDecodeError:
        return [UNPARSEABLE_TOKEN]
    if not isinstance(parsed, dict):
        return [UNPARSEABLE_TOKEN]
    return sorted(parsed.keys())


def body_dimensions(
    body: Any, record_type: Optional[str], role: Optional[str],
    model: Optional[str], compact_subtype: Optional[str],
    is_compact_boundary: int, parent_uuid: Optional[str],
    ts: Optional[str], message_uuid: Optional[str],
) -> Dict[str, Any]:
    """Derive the thirteen body-side dimensions from a parsed body.

    Description: the interned scalars arrive already resolved by the
      caller's join, because re-deriving them from the JSON would be a
      second reading of the same fact that could disagree with the
      stored column. Everything shape-related is read from the parsed
      body. Nullable scalars collapse to 'set'/'null' so no identifier
      text enters the signature.
    Inputs: body (parsed body JSON), record_type / role / model /
      compact_subtype (str or None, from the interning tables),
      is_compact_boundary (int), parent_uuid / ts / message_uuid (str or
      None).
    Output: dict keyed by BODY_DIMENSION_NAMES.
    Example: body_dimensions({}, None, None, None, None, 0, None, None,
      None)["content_shape"] -> "<no message obj>"
    """
    inner = body.get("message") if isinstance(body, dict) else None
    inner = inner if isinstance(inner, dict) else None
    if inner is None:
        content_shape = NO_MESSAGE_TOKEN
        blocks: List[str] = []
        usage_shape = NO_MESSAGE_TOKEN
        stop_reason = NO_MESSAGE_TOKEN
    else:
        content_shape = (
            ABSENT_TOKEN if "content" not in inner
            else _classify_json(inner["content"])
        )
        blocks = []
        if content_shape == "array":
            seen = set()
            for block in inner["content"]:
                if isinstance(block, dict):
                    kind = block.get("type")
                    seen.add(
                        kind if isinstance(kind, str) else NON_STRING_TYPE_TOKEN
                    )
                else:
                    seen.add(f"<non-object block:{_classify_json(block)}>")
            blocks = sorted(seen)
        usage_shape = (
            ABSENT_TOKEN if "usage" not in inner
            else _classify_json(inner["usage"])
        )
        if "stop_reason" not in inner:
            stop_reason = ABSENT_TOKEN
        elif inner["stop_reason"] is None:
            stop_reason = "null"
        elif isinstance(inner["stop_reason"], str):
            stop_reason = inner["stop_reason"]
        else:
            stop_reason = NON_STRING_TOKEN
    return {
        "record_type": record_type, "role": role, "model": model,
        "compact_subtype": compact_subtype,
        "is_compact_boundary": int(is_compact_boundary),
        "parent_uuid": "set" if parent_uuid is not None else "null",
        "ts": "set" if ts is not None else "null",
        "message_uuid": "set" if message_uuid is not None else "null",
        "content_shape": content_shape, "content_block_types": blocks,
        "usage_shape": usage_shape, "stop_reason": stop_reason,
        "body_toplevel_type": (
            "dict" if isinstance(body, dict) else _classify_json(body)
        ),
    }


def no_body_dimensions() -> Dict[str, Any]:
    """The thirteen body dimensions for an appearance with no body row.

    Description: every name is still emitted, so the hashed dict has the
      same key set whether or not a body exists. A missing key would
      change the id for a reason that is about this function rather than
      about the data.
    Inputs: none.
    Output: dict keyed by BODY_DIMENSION_NAMES.
    Example: no_body_dimensions()["body_toplevel_type"] -> "<no body>"
    """
    dims: Dict[str, Any] = {name: None for name in BODY_DIMENSION_NAMES}
    dims["body_toplevel_type"] = NO_BODY_TOKEN
    return dims


def signature_id(dimensions: Dict[str, Any]) -> str:
    """Hash a dimensions dict into the manifest's signature id.

    Description: canonical JSON with sorted keys, so insertion order
      cannot change the id, then sha256 truncated. This is the one place
      the id is computed; every caller goes through it.
    Inputs: dimensions (dict - the full 25-key dimension dict).
    Output: str - SIGNATURE_ID_HEX_CHARS hex characters.
    Example: signature_id({"a": 1})[:4] -> "ca97"
    """
    canonical = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:SIGNATURE_ID_HEX_CHARS]
