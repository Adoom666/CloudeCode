"""The alert / hierarchy status-light contract. See docs/alert-state-model.md.

DESIGN ONLY. Nothing here has a caller yet. This module exists so the
design in ``docs/alert-state-model.md`` is checkable data rather than only
prose - the same relationship ``hook_contract.py`` has to the hook event
list.

THE CENTRAL IDEA, stated once here because it is the thing every function
below exists to serve: a status light is a function of TWO INDEPENDENT
INPUTS, never one collapsed value.

    color     = f(own_state)          -- what THIS node itself is doing
    animation = f(descendant_axis)    -- whether ANYTHING beneath it,
                                          at any depth, is currently working

The two axes never override one another. An idle session with a working
descendant is ``(idle, breathing)``. A working session with a working
descendant is ``(working, breathing)`` - same animation, different color,
both facts visible at once. This replaces an earlier draft of this module
that computed a single "roll-up" state by picking a winner between a
node's own state and its descendants'; that design was corrected by the
owner specifically because picking a winner destroys one of the two
facts. See ``docs/alert-state-model.md`` section 3 for the full account,
including why ``STATUS_WORKING_SUBAGENT`` (a real, shipped seventh value
of the OWN axis in ``session_status.py``) is deliberately NOT reused
here - under this model that concept is the ``breathing`` animation
layered on any color, not a color of its own. That is an explicit,
recorded disagreement with the shipped code, not a silent redefinition.

WHY THE TABLE IS DATA, NOT A FUNCTION BODY. Every row today happens to
follow two simple invariants (color always equals own_state; animation
depends only on descendant_axis). It is still stored as an explicit,
fully enumerated table rather than computed from those two invariants at
call time, because the owner has said more states are coming, and the
day one of them needs an EXCEPTION to either invariant, that exception
is a single row's ``animation`` (or, in principle, ``color``) column
changed - a data edit anyone can review as a diff - never a rewrite of
branching logic buried in a function. ``status_light`` performs exactly
one dict lookup and contains no state-name comparisons at all.

WHAT THIS DELIBERATELY DOES NOT DO. It does not read a filesystem, a
database, tmux, or a live hook stream. It does not know how to enumerate
a node's actual descendants - that data source is an open gap (see the
design doc, section 6, item 1). Every function here is pure: given the
same inputs, always the same outputs, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Sequence, Tuple

from src.core.hook_contract import ALL_HOOK_EVENTS, BY_NAME as HOOK_BY_NAME
from src.core.session_activity import WORKING_HEARTBEAT_TIMEOUT_SECONDS
from src.core.session_status import (
    STATUS_DEAD,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_NOTICE,
    STATUS_QUESTION,
    STATUS_UNKNOWN,
    STATUS_WORKING,
)

# ---------------------------------------------------------------------------
# Node kinds and own-axis vocabularies (docs/alert-state-model.md section 2)
# ---------------------------------------------------------------------------

#: A level-0 session - a tmux instance, everything charted in
#: ``docs/session-status-model.md``. Its own-axis vocabulary is that
#: document's ``ALL_ACTIVITY_STATUSES`` MINUS ``working_subagent`` - see
#: the module docstring for why that one value is deliberately excluded.
NODE_KIND_SESSION: str = "session"

#: A level >= 1 descendant - a subagent, a subagent's subagent, and so
#: on. ``spawnDepth`` (real data, read from a subagent's ``.meta.json`` -
#: see the design doc section 0) says how deep; every depth shares this
#: SAME own-axis vocabulary and the SAME table, by the explicit decision
#: in the design doc section 2.1 not to define a separate vocabulary per
#: depth.
NODE_KIND_CHILD: str = "child"

ALL_NODE_KINDS: Tuple[str, ...] = (NODE_KIND_SESSION, NODE_KIND_CHILD)

#: The session's own-axis vocabulary: everything ``session_status.py``
#: defines EXCEPT ``STATUS_WORKING_SUBAGENT`` (see module docstring).
#: Seven values, matching ``ALL_ACTIVITY_STATUSES`` minus one.
SESSION_OWN_STATES: Tuple[str, ...] = (
    STATUS_DEAD,
    STATUS_QUESTION,
    STATUS_NOTICE,
    STATUS_WORKING,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_UNKNOWN,
)

#: A child's own-axis vocabulary. Named ``idle`` rather than ``stopped``
#: on purpose - ``stopped`` already means something different and durable
#: on the LIFECYCLE axis (a tmux instance that is GONE). See design doc
#: section 2.2, "Naming note". Deliberately narrower than
#: ``SESSION_OWN_STATES``: no ``question`` and no ``notice`` (unverified
#: whether a subagent can independently block on, or call out to, the
#: user - design doc gap 2) and no
#: ``finished_unread`` (no per-child unread flag exists - design doc gap
#: 3). Reuses the exact same three string constants as
#: ``DESCENDANT_AXIS_STATES`` below - not a coincidence, see there.
CHILD_OWN_STATES: Tuple[str, ...] = (
    STATUS_WORKING,
    STATUS_IDLE,
    STATUS_UNKNOWN,
)

#: The three values the DESCENDANT axis can reduce to (section 3.1 of the
#: design doc). Reuses the same string constants as ``CHILD_OWN_STATES``
#: deliberately: what a child reports about ITSELF and what an ancestor
#: reduces its descendants TO are the same three-value vocabulary, so
#: reduction never has to translate between two different sets of names.
DESCENDANT_AXIS_STATES: Tuple[str, ...] = (
    STATUS_WORKING,
    STATUS_UNKNOWN,
    STATUS_IDLE,
)

#: Which own-axis vocabulary applies to which node kind. The one place
#: ``status_light`` / ``missing_light_rows`` resolve "which states are
#: valid here" - never hardcoded a second time.
OWN_STATES_BY_NODE_KIND: Dict[str, Tuple[str, ...]] = {
    NODE_KIND_SESSION: SESSION_OWN_STATES,
    NODE_KIND_CHILD: CHILD_OWN_STATES,
}

# ---------------------------------------------------------------------------
# Animation vocabulary
# ---------------------------------------------------------------------------

#: At least one descendant, at any depth, currently has own_state ==
#: STATUS_WORKING. The owner's own words: "working background will
#: always make it breathe" - absolute, no own_state carve-out. See the
#: (dead, working) row below, which is the one row where this absolute
#: rule is doing real work: it is kept ON specifically because that
#: combination should not be possible in steady state, so seeing it
#: breathe is a deliberate cleanup signal, not an oversight (design doc
#: section 3.5).
ANIMATION_BREATHING: str = "breathing"

#: No descendant is working. Produced by BOTH ``descendant_axis ==
#: STATUS_IDLE`` (every descendant that exists is definitely idle, or
#: there are none at all) AND ``descendant_axis == STATUS_UNKNOWN`` (at
#: least one descendant's state could not be evaluated, and none is
#: definitely working). Those are two DIFFERENT rows of ``LIGHT_TABLE``
#: that happen to share this one animation value - see the module
#: docstring's "why the table is data" note and design doc section 3.3.
#: An ``unknown`` descendant must not manufacture a breath it cannot
#: justify (a vanished subagent that keeps its ancestor breathing forever
#: is a stuck light nobody would trust - the owner's own words: "the
#: safety is against a STUCK CLAIM, not against telling the truth"), but
#: it is never rewritten to ``idle`` in the underlying data -
#: ``LightRow.descendant_axis`` on that row still reads the literal
#: string ``"unknown"``, inspectable by any caller that looks past the
#: animation.
ANIMATION_STEADY: str = "steady"

ALL_ANIMATIONS: Tuple[str, ...] = (
    ANIMATION_BREATHING,
    ANIMATION_STEADY,
)

#: descendant_axis value -> the animation it produces, on EVERY row today
#: (see the module docstring - this is a current invariant of the table,
#: not a rule the lookup function enforces). Used only to BUILD
#: ``LIGHT_TABLE`` below, literally, one row at a time - never consulted
#: by ``status_light`` itself, which does a table lookup and nothing else.
#: Two descendant_axis values map to the same animation (STATUS_UNKNOWN
#: and STATUS_IDLE both -> ANIMATION_STEADY) - deliberate, see
#: ANIMATION_STEADY's docstring above.
_ANIMATION_FOR_DESCENDANT_AXIS: Dict[str, str] = {
    STATUS_WORKING: ANIMATION_BREATHING,
    STATUS_UNKNOWN: ANIMATION_STEADY,
    STATUS_IDLE: ANIMATION_STEADY,
}

#: The one (node_kind, own_state, descendant_axis) key that is a
#: DELIBERATE CONTRADICTION rather than a normal cell: a session
#: confirmed ``dead`` by tmux (a terminal, authoritative fact - a process
#: exit, not a guess) with a descendant confirmed ``working`` (a real,
#: currently-heartbeating subagent, not merely ``unknown``). Physically
#: should not happen in steady state - a subagent is a subprocess of the
#: pane's own process - so the owner's instruction was to KEEP it
#: breathing rather than suppress it: seeing it IS the signal that
#: something did not clean up (design doc section 3.5). No rule
#: elsewhere in this module forces a dead/terminal own_state to render
#: quiet; this constant exists only to mark which single row gets the
#: extra ``contradiction=True`` field, so a caller can single that
#: condition out without comparing against ``own_state``/``descendant_axis``
#: by name at the call site.
_CONTRADICTION_KEYS: frozenset = frozenset(
    {(NODE_KIND_SESSION, STATUS_DEAD, STATUS_WORKING)}
)


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LightRow:
    """One entry of the status-light contract: a single, fully-specified
    (node_kind, own_state, descendant_axis) -> (color, animation,
    contradiction) fact.

    ``status_light`` returns the matched ``LightRow`` itself rather than
    a bare (color, animation) pair, so ``descendant_axis`` (the fact that
    must stay inspectable even when its animation is the same as a
    healthy row's - design doc section 3.3) and ``contradiction`` (the
    flag that must stay visible on the one row that carries it - design
    doc section 3.5) are never dropped between the table and the caller.

    Every field is a plain value so a new row is a one-line addition and
    a diff on this file shows exactly which combination changed.
    """

    node_kind: str
    own_state: str
    descendant_axis: str
    color: str
    animation: str
    contradiction: bool


def _row(node_kind: str, own_state: str, descendant_axis: str) -> LightRow:
    """Build one literal row. Not exported - a construction helper only,
    so the 30 rows below read as one fact per line without retyping the
    animation lookup 30 times. ``status_light`` never calls this; it
    reads the already-built ``LIGHT_TABLE``.
    """
    key = (node_kind, own_state, descendant_axis)
    return LightRow(
        node_kind=node_kind,
        own_state=own_state,
        descendant_axis=descendant_axis,
        color=own_state,
        animation=_ANIMATION_FOR_DESCENDANT_AXIS[descendant_axis],
        contradiction=key in _CONTRADICTION_KEYS,
    )


#: The full cross product, both node kinds, spelled out. 7 own states x 3
#: descendant states = 21 session rows; 3 x 3 = 9 child rows; 30 total.
#: It was 6 x 3 = 18 until 2026-09-08, when ``question`` split into
#: ``question`` (a PermissionRequest - the agent is stopped) and
#: ``notice`` (a Notification - it is not). Nothing here needed a new
#: rule for it: ``color`` IS ``own_state``, so a new own state is three
#: new rows and no new decision.
#: Totality of ``status_light`` over its documented domain rests entirely
#: on this list actually covering the whole cross product - proven by
#: ``missing_light_rows`` returning empty for both node kinds, asserted
#: in tests as a property, not spot-checked.
LIGHT_TABLE: Tuple[LightRow, ...] = tuple(
    _row(node_kind, own_state, descendant_axis)
    for node_kind in ALL_NODE_KINDS
    for own_state in OWN_STATES_BY_NODE_KIND[node_kind]
    for descendant_axis in DESCENDANT_AXIS_STATES
)

_seen_keys: set = set()
for _row_obj in LIGHT_TABLE:
    _key = (_row_obj.node_kind, _row_obj.own_state, _row_obj.descendant_axis)
    if _key in _seen_keys:
        raise ValueError(f"duplicate LIGHT_TABLE row for {_key!r}")
    _seen_keys.add(_key)
del _seen_keys, _row_obj, _key

#: Fast lookup used by ``status_light``. Built once, at import, from the
#: table above - never a second source of truth for what a row says.
_TABLE_INDEX: Dict[Tuple[str, str, str], LightRow] = {
    (row.node_kind, row.own_state, row.descendant_axis): row for row in LIGHT_TABLE
}


def status_light(own_state: str, descendant_axis: str, node_kind: str) -> LightRow:
    """Look up the full light fact for one node. A pure lookup, nothing
    else - see the module docstring for why.

    Description: the ONE place a caller turns (own_state, descendant_axis,
      node_kind) into what to render. Contains no branching on any
      specific state's name - adding a state to either vocabulary and its
      corresponding LIGHT_TABLE rows is the entire extension surface.
      Returns the matched ``LightRow`` whole, not a narrowed
      (color, animation) pair, so ``descendant_axis`` and
      ``contradiction`` stay available to any caller that wants them.
    Inputs:
        own_state: str - a member of ``OWN_STATES_BY_NODE_KIND[node_kind]``.
        descendant_axis: str - a member of ``DESCENDANT_AXIS_STATES``,
            normally the output of ``reduce_descendant_axis``.
        node_kind: str - one of ``ALL_NODE_KINDS``.
    Output: LightRow - ``.color``, ``.animation``, ``.descendant_axis``,
        ``.contradiction`` all present.
    Raises:
        ValueError: the triple is not a row in ``LIGHT_TABLE``. For an
            in-domain node_kind/own_state/descendant_axis this can only
            happen if a state was added to a vocabulary without adding
            its rows - see ``missing_light_rows``. For an out-of-domain
            node_kind or own_state it is a caller bug, not a state to
            render, and is kept distinct from passing STATUS_UNKNOWN
            (which IS in-domain and means "could not evaluate").
    Example:
        >>> status_light(STATUS_IDLE, STATUS_WORKING, NODE_KIND_SESSION).animation
        'breathing'
        >>> status_light(STATUS_WORKING, STATUS_WORKING, NODE_KIND_SESSION).color
        'working'
        >>> status_light(STATUS_DEAD, STATUS_WORKING, NODE_KIND_SESSION).contradiction
        True
    """
    row = _TABLE_INDEX.get((node_kind, own_state, descendant_axis))
    if row is None:
        raise ValueError(
            f"no LIGHT_TABLE row for node_kind={node_kind!r} "
            f"own_state={own_state!r} descendant_axis={descendant_axis!r} - "
            "see missing_light_rows() to check whether this is a coverage "
            "gap or simply an invalid input"
        )
    return row


def missing_light_rows(node_kind: str) -> Tuple[Tuple[str, str], ...]:
    """The completeness proof the design's totality requirement rests on.

    Description: computes the FULL cross product of ``node_kind``'s own
      states and ``DESCENDANT_AXIS_STATES``, and returns every
      (own_state, descendant_axis) pair that has NO row in
      ``LIGHT_TABLE``. This is what must fail loudly the day a state is
      added to a vocabulary without adding its corresponding rows -
      exactly the property the owner's correction asked this contract to
      guarantee, not merely document.
    Inputs:
        node_kind: str - one of ``ALL_NODE_KINDS``.
    Output: Tuple[Tuple[str, str], ...] - empty iff the table is complete
        for this node kind, sorted otherwise so a failure message is
        stable.
    Raises:
        ValueError: ``node_kind`` is not in ``ALL_NODE_KINDS``.
    Example:
        >>> missing_light_rows(NODE_KIND_SESSION)
        ()
    """
    own_states = OWN_STATES_BY_NODE_KIND.get(node_kind)
    if own_states is None:
        raise ValueError(f"unknown node_kind: {node_kind!r}")
    present = {
        (row.own_state, row.descendant_axis)
        for row in LIGHT_TABLE
        if row.node_kind == node_kind
    }
    full = {(o, d) for o in own_states for d in DESCENDANT_AXIS_STATES}
    return tuple(sorted(full - present))


# ---------------------------------------------------------------------------
# The descendant-axis reduction (design doc section 3.1)
# ---------------------------------------------------------------------------

_VALID_DESCENDANT_INPUTS: frozenset = frozenset(DESCENDANT_AXIS_STATES)


def reduce_descendant_axis(states: Sequence[str]) -> str:
    """Reduce a FLAT set of descendants' own-states to one axis value.

    Description: total and pure. Takes every descendant beneath a node,
      AT ANY DEPTH - there is deliberately no depth parameter on this
      function at all, which is how the design doc's "depth does not
      matter to the reduction" decision (section 3.1) is enforced
      structurally rather than merely documented. Precedence is
      ``working > unknown > idle``: a descendant with definite evidence
      of activity always outranks one that could not be evaluated, which
      always outranks a confirmed-resting one. An empty sequence reduces
      to ``idle`` - "nothing below this node" is a positive fact, not a
      failed measurement, matching how ``activity_persist.py`` already
      treats a rest state as true-until-contradicted rather than
      something that decays.
    Inputs:
        states: sequence[str] - each element a direct or indirect
            descendant's own_state, expected to be a member of
            ``DESCENDANT_AXIS_STATES`` (== ``CHILD_OWN_STATES``).
    Output: str - one of ``DESCENDANT_AXIS_STATES``.
    Raises:
        ValueError: any element is not a member of
            ``DESCENDANT_AXIS_STATES``.
    Example:
        >>> reduce_descendant_axis([])
        'idle'
        >>> reduce_descendant_axis([STATUS_WORKING, STATUS_IDLE])
        'working'
        >>> reduce_descendant_axis([STATUS_IDLE, STATUS_UNKNOWN])
        'unknown'
    """
    materialized = tuple(states)
    invalid = tuple(s for s in materialized if s not in _VALID_DESCENDANT_INPUTS)
    if invalid:
        raise ValueError(f"not a valid descendant own-state: {invalid!r}")
    if any(s == STATUS_WORKING for s in materialized):
        return STATUS_WORKING
    if any(s == STATUS_UNKNOWN for s in materialized):
        return STATUS_UNKNOWN
    return STATUS_IDLE


# ---------------------------------------------------------------------------
# The hook state model (design doc section 1)
# ---------------------------------------------------------------------------

AXIS_ACTIVITY: str = "activity"
AXIS_LIFECYCLE: str = "lifecycle"
AXIS_NONE: str = "none"


@dataclass(frozen=True)
class HookStateRole:
    """What one hook event does to the state model, if anything.

    - ``carries_state``: False for every one of the 21 events this app
      does not currently subscribe to - they cannot carry state they are
      never delivered for.
    - ``axis``: which durable/perishable axis this event writes, or
      ``AXIS_NONE``.
    - ``perishable``: True iff the state this event feeds decays on a
      clock rather than being true-until-contradicted.
    - ``decay_seconds``: the clock, when perishable; None otherwise.
    """

    name: str
    carries_state: bool
    axis: str
    perishable: bool
    decay_seconds: "int | None"
    why: str


#: Classification for exactly the 10 events ``hook_contract.HOOK_REGISTRY``
#: marks ``subscribed=True``, matching the table in design doc section 1.
#: Every other event in ``ALL_HOOK_EVENTS`` gets a default unsubscribed
#: row, built below, reusing ``hook_contract``'s own stated reason so
#: there is exactly one place that reason is spelled.
_SUBSCRIBED_STATE_INFO: Dict[str, Tuple[str, bool, "int | None"]] = {
    "UserPromptSubmit": (
        AXIS_ACTIVITY, False, None,
    ),
    "PreToolUse": (
        AXIS_ACTIVITY, True, WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    "PostToolUse": (
        AXIS_ACTIVITY, True, WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    "SubagentStart": (
        AXIS_ACTIVITY, True, WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    "SubagentStop": (
        AXIS_ACTIVITY, False, None,
    ),
    "Notification": (
        AXIS_ACTIVITY, True, WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    "PermissionRequest": (
        AXIS_ACTIVITY, True, WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    "Stop": (
        AXIS_ACTIVITY, False, None,
    ),
    "SessionStart": (
        AXIS_LIFECYCLE, False, None,
    ),
    "SessionEnd": (
        AXIS_LIFECYCLE, False, None,
    ),
}

_SUBSCRIBED_WHY: Dict[str, str] = {
    "UserPromptSubmit": (
        "an edge, not a level: clears permission_open and notice_open, "
        "and stamps a fresh "
        "heartbeat that PreToolUse/PostToolUse then refresh"
    ),
    "PreToolUse": "working heartbeat",
    "PostToolUse": "working heartbeat",
    "SubagentStart": (
        "increments subagent_depth today (session_activity.py); design "
        "doc section 2.3 proposes this become an edge into a CHILD "
        "node's own state instead of a same-node counter"
    ),
    "SubagentStop": (
        "terminal - floors subagent_depth at 0. Does NOT set the "
        "durable unread flag - design doc section 4, gap 3"
    ),
    "Notification": (
        "sets notice_open = True - claude wants attention and is NOT "
        "blocked, which is why it is a different flag and a different "
        "state from PermissionRequest"
    ),
    "PermissionRequest": (
        "sets permission_open = True - the agent is STOPPED until the "
        "user answers"
    ),
    "Stop": (
        "terminal, and the ONLY subscribed event that also sets the "
        "durable auto-unread flag (session_manager.py, EVENT_STOP branch)"
    ),
    "SessionStart": "binds claude_session_uuid, lineage, claude_title",
    "SessionEnd": "marks the conversation ended",
}


def _build_hook_state_registry() -> Tuple[HookStateRole, ...]:
    rows = []
    for name in ALL_HOOK_EVENTS:
        info = _SUBSCRIBED_STATE_INFO.get(name)
        if info is not None:
            axis, perishable, decay_seconds = info
            rows.append(
                HookStateRole(
                    name=name,
                    carries_state=True,
                    axis=axis,
                    perishable=perishable,
                    decay_seconds=decay_seconds,
                    why=_SUBSCRIBED_WHY[name],
                )
            )
        else:
            known = HOOK_BY_NAME.get(name)
            why = known.why if known is not None else "not in hook_contract.BY_NAME"
            rows.append(
                HookStateRole(
                    name=name,
                    carries_state=False,
                    axis=AXIS_NONE,
                    perishable=False,
                    decay_seconds=None,
                    why=why,
                )
            )
    return tuple(rows)


#: One row per event in ``hook_contract.ALL_HOOK_EVENTS``, in the same
#: order. Completeness (every event exactly once) is a structural
#: consequence of building this FROM that list, and is still asserted in
#: tests rather than only assumed - the same discipline
#: ``hook_contract.py``'s own registry test applies to itself.
HOOK_STATE_REGISTRY: Tuple[HookStateRole, ...] = _build_hook_state_registry()

HOOK_STATE_BY_NAME: Dict[str, HookStateRole] = {
    role.name: role for role in HOOK_STATE_REGISTRY
}
