"""When two bodies under one message uuid are the SAME MESSAGE.

WHY THIS EXISTS. ``GATE_DUPLICATE_UUID_BODY_CONFLICT`` fired whenever two
bodies stored under one uuid were not byte-identical after key sorting.
Against the owner's full corpus (19,541 transcripts, 2,432,762 bodies,
3,006,908 appearances) that produced 45,246 review items, 90.7 percent of
a 49,905-item queue. A queue that size is a queue nobody opens, and a
queue nobody opens is worse than no queue at all.

WHAT THE 45,246 ACTUALLY WERE. Measured, whole corpus, not sampled: every
one of the 45,246 groups' differences is drawn from a CLOSED SET OF 27
JSON PATHS, arranged into only 75 distinct difference signatures. Resume,
fork and subagent spawn replay a prior conversation verbatim into a new
transcript, and the replay rewrites the fields that say WHERE and WHEN
and BY WHAT the copy was recorded - the session it landed in, the CLI
version that wrote it, the fork it came from - while the message itself
is copied unchanged. Separately, an assistant record can be written once
as a streaming snapshot and once completed, which moves ``stop_reason``
and ``usage`` and nothing else.

WHAT THIS MODULE IS, AND IS NOT. It is a pure canonicalisation: body in,
canonical body out, no database, no clock, no I/O. It decides only what
is GATED. It never decides what is STORED. Two bodies under one uuid are
two rows in ``message_bodies`` whatever this module says about them, for
the same reason the store has never done keep-first: losing either copy
would be data loss, and the evidence that a transcript was EDITED AFTER
THE FACT lives precisely in the pair.

THE FAILURE MODE THIS MODULE IS ONE LINE AWAY FROM. Over-normalising is
silent and permanent: a rule one key too wide turns an edited transcript
into a non-event, and nothing downstream can ever notice, because the
finding was never raised. Under-normalising costs a slightly longer
queue. The two errors are not symmetric, so every rule below carries the
measured class that justifies it and a test proving it does not collapse
a genuine difference, and a rule that cannot cite a measured class is not
in the table.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

#: Value used to mean "this path is absent" when comparing, so an absent
#: key and a key holding the JSON null are distinguishable. They are NOT
#: treated as equal in general - only one measured path gets that
#: treatment, and it is named in NULLABLE_DROP_PATHS.
ABSENT: str = "__absent__"


@dataclass(frozen=True)
class EquivalenceRule:
    """One normalisation, with the measurement that justifies it.

    Description: the declaration format for this module, in the same
      spirit as ``GATE_CONDITIONS`` in message_gate_contract.py - a rule
      without a recorded reason is a rule nobody can argue with, which is
      how an over-wide normalisation survives review.
    - ``path``: dotted JSON path this rule acts on, top level first.
    - ``kind``: one of RULE_KINDS.
    - ``groups``: how many of the 45,246 conflicting uuid groups have a
      difference at this path.
    - ``justification``: why a difference here is not a difference in the
      message.
    Inputs: constructed at import time only.
    Output: n/a (data holder).
    Example: EquivalenceRule("cwd", "drop", 684, "x").path -> "cwd"
    """

    path: str
    kind: str
    groups: int
    justification: str

    def __post_init__(self) -> None:
        if self.kind not in RULE_KINDS:
            raise ValueError(f"{self.path}: unknown rule kind {self.kind!r}")
        if not self.justification:
            raise ValueError(f"{self.path}: justification must not be blank")


#: ``drop`` removes the path unconditionally. ``drop_if_null`` removes it
#: only when it holds JSON null, so two DIFFERENT non-null values still
#: gate. ``text_block_shape`` rewrites one measured representation into
#: the other without changing any text.
RULE_KINDS: FrozenSet[str] = frozenset(
    {"drop", "drop_if_null", "text_block_shape"})


EQUIVALENCE_RULES: Tuple[EquivalenceRule, ...] = (
    EquivalenceRule(
        "sessionId", "drop", 40607,
        "the id of the transcript this COPY was written into. A resumed "
        "or forked session replays prior messages under its own new "
        "sessionId by construction, so this field differing is the "
        "definition of a replay, not evidence about the message.",
    ),
    EquivalenceRule(
        "slug", "drop", 30853,
        "the human-readable name of the session that recorded the copy, "
        "derived from the session and regenerated per session. Same "
        "argument as sessionId. Measured absent on one side in the "
        "single-key case, which is a CLI version difference in what gets "
        "written, not a change to the message.",
    ),
    EquivalenceRule(
        "version", "drop", 29078,
        "the Claude Code CLI version that wrote the line (measured "
        "example: 2.1.73 against 2.1.74). It describes the writer, never "
        "the message. A message replayed weeks later is written by a "
        "newer binary and is still the same message.",
    ),
    EquivalenceRule(
        "forkedFrom", "drop", 17372,
        "provenance of the fork that replayed the message: the session "
        "forked from and the message forked at. Measured, it is present "
        "on the forked copy and absent on the original - it is a "
        "statement ABOUT this copy's origin, and its presence is exactly "
        "what makes the copy a replay rather than a conflict.",
    ),
    EquivalenceRule(
        "promptId", "drop", 4422,
        "the id of the user turn that the recording run attributed the "
        "line to. Regenerated per run, so a replay of one message under "
        "a second prompt id is the same message attributed twice.",
    ),
    EquivalenceRule(
        "gitBranch", "drop", 2365,
        "the branch checked out in the recording session (measured "
        "example: feat/remote-claude-mini against main). A property of "
        "the working tree at recording time, not of the message.",
    ),
    EquivalenceRule(
        "cwd", "drop", 684,
        "the directory the recording session was started in (measured "
        "example: .../camera-viewer/app against .../camera-viewer). Same "
        "class as gitBranch.",
    ),
    EquivalenceRule(
        "entrypoint", "drop", 226,
        "how the recording session was launched (measured: absent "
        "against 'cli', alongside a CLI version difference). A field a "
        "newer CLI began writing.",
    ),
    EquivalenceRule(
        "sourceToolAssistantUUID", "drop", 63,
        "a back-pointer added by a newer CLI from a tool-result record "
        "to the assistant message that requested it. Measured 63 of 63 "
        "groups are absent-against-present and ZERO hold two different "
        "values, so dropping it cannot collapse two copies that disagree "
        "about the link - and because both bodies are stored, the "
        "pointer is still readable from the copy that carries it.",
    ),
    EquivalenceRule(
        "attachment.displayPath", "drop", 30,
        "the path an attachment is DISPLAYED under, relative to the "
        "recording session's cwd, so it inherits cwd's per-recording "
        "nature. Measured 30 of 30 absent-against-present, ZERO with two "
        "differing values.",
    ),
    EquivalenceRule(
        "message.usage", "drop", 1013,
        "the API call's token accounting for THIS recording: "
        "input/output/cache tokens, inference_geo, server_tool_use "
        "counts, speed, iterations. Measured example: all four token "
        "counts 0 against 2/1394/2288/973311 for the same content. "
        "Decisive measurement: of the 45,246 groups, the number where a "
        "usage path differs AND message.content or toolUseResult or "
        "summary also differs is ZERO - usage never accompanies a real "
        "content change, it accompanies a second recording of one.",
    ),
    EquivalenceRule(
        "message.stop_reason", "drop", 426,
        "why generation ended, populated at completion time in the same "
        "object as usage. Measured across all 426 groups where it "
        "differs, EVERY one has null on one side (289 tool_use against "
        "null, 74 end_turn against null, 63 with three-or-more copies "
        "and the same shape) and ZERO have two different non-null "
        "values, which is the streaming-snapshot signature. The residual "
        "risk of dropping rather than null-gating it is a max_tokens "
        "against end_turn pair, and that pair cannot hide here: a "
        "truncated completion differs in message.content, which still "
        "gates. Measured co-occurrence of a stop_reason difference with "
        "a content difference is also ZERO.",
    ),
    EquivalenceRule(
        "message.context_management", "drop_if_null", 240,
        "measured 240 of 240 groups are the key ABSENT on one side "
        "against an explicit JSON null on the other, and ZERO hold a "
        "populated value. Absent and null say the same thing here, so "
        "the null is dropped and the key is left alone whenever it "
        "actually holds something - two different context-management "
        "settings still gate.",
    ),
    EquivalenceRule(
        "message.content", "text_block_shape", 18,
        "one copy stores the text as a bare string and the other as the "
        "single content block [{'type': 'text', 'text': <same string>}]. "
        "Measured 18 of the 73 groups with a message.content difference "
        "are exactly this shape change with byte-identical text; the "
        "other 55 are REAL content differences (a measured example adds "
        "a second block reading 'Tool loaded.') and MUST keep gating, "
        "which is why this rule rewrites one shape and never compares "
        "list contents.",
    ),
)

#: Paths deliberately NOT normalised, with the reason, because an
#: omission nobody wrote down reads as an oversight the next time.
NOT_NORMALISED: Tuple[Tuple[str, int, str], ...] = (
    ("parentUuid", 1494,
     "two copies of one message naming DIFFERENT parents is a difference "
     "in the conversation graph itself, not in where the copy was "
     "recorded. This one was nearly normalised on the strength of the "
     "fact that 1,155 of the affected groups also carry a forkedFrom, "
     "which reads as fork bookkeeping. Measuring it settled it: 1,474 of "
     "the 1,494 groups name TWO REAL PARENTS and only 20 are the "
     "null-against-a-value shape a re-rooted fork would produce. It "
     "keeps gating, and it is 96 percent of what is left."),
    ("message.content list contents", 55,
     "a differing number of content blocks, or differing text inside "
     "them, is the message differing. Only the scalar-against-single-"
     "block SHAPE is normalised."),
    ("everything outside the rule table", 0,
     "a path that has never been measured differing has no measured "
     "class to justify a rule, so it gates."),
)

_DROP_PATHS: Tuple[str, ...] = tuple(
    r.path for r in EQUIVALENCE_RULES if r.kind == "drop")
_NULL_DROP_PATHS: Tuple[str, ...] = tuple(
    r.path for r in EQUIVALENCE_RULES if r.kind == "drop_if_null")
_SHAPE_PATHS: Tuple[str, ...] = tuple(
    r.path for r in EQUIVALENCE_RULES if r.kind == "text_block_shape")


def _walk_to_parent(
    body: Any, path: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve a dotted path to the dict holding its last segment.

    Description: returns (None, leaf) when any segment on the way is
      missing or is not a dict, which is the ordinary case for a record
      type that does not carry the path at all.
    Inputs: body (any parsed JSON value), path (str - dotted).
    Output: (dict or None, str) - the owning dict and the leaf key.
    Example: _walk_to_parent({"a": {"b": 1}}, "a.b")[1] -> "b"
    """
    segments = path.split(".")
    node = body
    for segment in segments[:-1]:
        if not isinstance(node, dict) or segment not in node:
            return None, segments[-1]
        node = node[segment]
    if not isinstance(node, dict):
        return None, segments[-1]
    return node, segments[-1]


def _as_text_blocks(value: Any) -> Any:
    """Rewrite a bare content string as its one-text-block equivalent.

    Description: the ONLY shape change this module makes. A value that is
      not a string is returned untouched, so a list of blocks is never
      reordered, merged or compared element-wise here.
    Inputs: value (any parsed JSON value).
    Output: the value, or [{"type": "text", "text": value}] for a str.
    Example: _as_text_blocks("hi") -> [{"type": "text", "text": "hi"}]
    """
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    return value


def normalise_body(body: Any) -> Any:
    """Apply every declared rule to one body, without mutating the input.

    Description: the pure heart of the module. Deep-copies first, so a
      caller can hand in a body it intends to store byte-exactly and get
      the gating form back without its own value being touched. A body
      that is not a JSON object (a bare string or list line, rare but
      legal) is returned as-is: none of the rules address a shape with no
      keys, and inventing a normalisation for one would be exactly the
      unjustified rule this table forbids.
    Inputs: body (any parsed JSON value).
    Output: a new value with the recording-context fields removed.
    Example: normalise_body({"uuid": "u", "cwd": "/a"}) -> {"uuid": "u"}
    """
    if not isinstance(body, dict):
        return body
    out = copy.deepcopy(body)
    for path in _DROP_PATHS:
        parent, leaf = _walk_to_parent(out, path)
        if parent is not None:
            parent.pop(leaf, None)
    for path in _NULL_DROP_PATHS:
        parent, leaf = _walk_to_parent(out, path)
        if parent is not None and parent.get(leaf, ABSENT) is None:
            parent.pop(leaf, None)
    for path in _SHAPE_PATHS:
        parent, leaf = _walk_to_parent(out, path)
        if parent is not None and leaf in parent:
            parent[leaf] = _as_text_blocks(parent[leaf])
    return out


def canonical_identity(body: Any) -> str:
    """The order-insensitive string two bodies are compared as.

    Description: normalise, then render with sorted keys, so top-level
      and nested key ORDER never registers as a difference here. Key
      order is still preserved on the stored row and on the appearance,
      which is what keeps export byte-exact - this string is only ever
      used to answer "are these the same message?".
    Inputs: body (any parsed JSON value).
    Output: str.
    Example: canonical_identity({"b": 1, "a": 2}) -> '{"a":2,"b":1}'
    """
    return json.dumps(normalise_body(body), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def bodies_equivalent(left: Any, right: Any) -> bool:
    """Whether two bodies are the same message recorded twice.

    Description: the gate's question, and the whole public contract of
      this module. False means a genuine difference and a STOP finding;
      True means a benign recording difference and an ADVISORY one.
      Never means "do not store the second body" - that decision is not
      this module's and is not the store's either.
    Inputs: left, right (parsed JSON values).
    Output: bool.
    Example: bodies_equivalent({"cwd": "/a"}, {"cwd": "/b"}) -> True
    """
    return canonical_identity(left) == canonical_identity(right)


def _flatten(value: Any, prefix: str, out: Dict[str, Any]) -> None:
    """Flatten a JSON value to path -> scalar, list indices collapsed later.

    Description: internal to :func:`difference_paths`. A list records its
      own length under a ``#len`` path so a length change is visible even
      when every shared index matches.
    Inputs: value, prefix (str), out (dict, mutated).
    Output: None.
    Example: d = {}; _flatten({"a": 1}, "", d); d -> {"a": 1}
    """
    if isinstance(value, dict):
        if not value:
            out[prefix] = "__empty_object__"
        for key, sub in value.items():
            _flatten(sub, f"{prefix}.{key}" if prefix else key, out)
    elif isinstance(value, list):
        out[f"{prefix}#len"] = len(value)
        for index, sub in enumerate(value):
            _flatten(sub, f"{prefix}[{index}]", out)
    else:
        out[prefix] = value


def difference_paths(bodies: List[Any]) -> List[str]:
    """Every JSON path on which the given bodies disagree, RAW.

    Description: computed BEFORE normalisation, so it names what actually
      differed rather than what survived the rules. That is what makes an
      advisory finding readable: "these two copies differ at sessionId
      and version" is actionable, "these two copies are equivalent" is
      not. List indices are kept, because a caller that wants them
      collapsed can do so and a caller that needs the exact index cannot
      recover it once thrown away.
    Inputs: bodies (list of parsed JSON values, two or more).
    Output: list[str], sorted.
    Example: difference_paths([{"a": 1}, {"a": 2}]) -> ["a"]
    """
    maps: List[Dict[str, Any]] = []
    for body in bodies:
        flat: Dict[str, Any] = {}
        _flatten(body, "", flat)
        maps.append(flat)
    every_key: set = set()
    for flat in maps:
        every_key |= set(flat)
    differing = []
    for key in every_key:
        rendered = {
            json.dumps(flat.get(key, ABSENT), sort_keys=True, default=str)
            for flat in maps
        }
        if len(rendered) > 1:
            differing.append(key)
    return sorted(differing)


@dataclass(frozen=True)
class DuplicateVerdict:
    """What the gate should record about a second body under one uuid.

    - ``code``: GATE_DUPLICATE_UUID_BODY_CONFLICT when the difference
      survives normalisation, GATE_DUPLICATE_UUID_RECORDING_VARIANT when
      it does not.
    - ``detail``: the finding text. Names the differing PATHS and never
      their values, because a body can hold credential material and this
      string is written to the findings table.
    """

    code: str
    detail: str


def duplicate_verdict(
    new_body: Any, stored_bodies: List[Any], uuid: str,
) -> Optional[DuplicateVerdict]:
    """Classify a second body arriving under an already-seen uuid.

    Description: the single decision point the store calls. Returns None
      when nothing differs at all, which is the ordinary resume/fork
      replay of a byte-identical record and is not a finding of any
      kind. Otherwise it names the difference and says whether it
      survives the declared equivalence. Pure: takes parsed bodies, not
      a connection.
    Inputs: new_body (parsed JSON value), stored_bodies (list of parsed
      JSON values already stored under this uuid), uuid (str).
    Output: DuplicateVerdict or None.
    Example: duplicate_verdict({"a": 1}, [{"a": 1}], "u") -> None
    """
    from src.core.message_gate_contract import (
        GATE_DUPLICATE_UUID_BODY_CONFLICT,
        GATE_DUPLICATE_UUID_RECORDING_VARIANT,
    )

    every = [new_body] + list(stored_bodies)
    paths = difference_paths(every)
    if not paths:
        return None
    shown = ", ".join(paths[:8])
    more = "" if len(paths) <= 8 else f" (+{len(paths) - 8} more)"
    if len({canonical_identity(body) for body in every}) > 1:
        return DuplicateVerdict(
            GATE_DUPLICATE_UUID_BODY_CONFLICT,
            f"uuid {uuid} is stored with {len(every)} bodies that differ "
            f"at {len(paths)} json path(s) and remain different after the "
            f"declared recording-context equivalence: {shown}{more}. All "
            "bodies are kept as separate identity rows.",
        )
    return DuplicateVerdict(
        GATE_DUPLICATE_UUID_RECORDING_VARIANT,
        f"uuid {uuid} is stored with {len(every)} bodies differing at "
        f"{len(paths)} json path(s), all absorbed by the declared "
        f"recording-context equivalence: {shown}{more}. Same message, "
        "recorded more than once. All bodies are kept as separate "
        "identity rows.",
    )
