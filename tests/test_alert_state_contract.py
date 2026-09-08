"""Properties that make the alert-state contract worth having.

Mirrors ``tests/test_hook_contract.py`` in spirit: the properties pinned
here are the ones that make a design document's machine-readable half
actually mean something, rather than being data nobody checks.
"""

import inspect
import itertools

import pytest

from src.core.hook_contract import ALL_HOOK_EVENTS
from src.core.session_status import (
    STATUS_DEAD,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_NOTICE,
    STATUS_QUESTION,
    STATUS_UNKNOWN,
    STATUS_WORKING,
)

from src.core.alert_state_contract import (
    ALL_ANIMATIONS,
    ALL_NODE_KINDS,
    ANIMATION_BREATHING,
    ANIMATION_STEADY,
    AXIS_ACTIVITY,
    AXIS_LIFECYCLE,
    CHILD_OWN_STATES,
    DESCENDANT_AXIS_STATES,
    HOOK_STATE_BY_NAME,
    HOOK_STATE_REGISTRY,
    LIGHT_TABLE,
    NODE_KIND_CHILD,
    NODE_KIND_SESSION,
    OWN_STATES_BY_NODE_KIND,
    SESSION_OWN_STATES,
    missing_light_rows,
    reduce_descendant_axis,
    status_light,
)


# ---- vocabularies ------------------------------------------------------

def test_session_own_states_is_the_activity_vocabulary_minus_working_subagent():
    """Explicit, tested statement of the one deliberate disagreement with
    the shipped code: STATUS_WORKING_SUBAGENT is not a color."""
    assert set(SESSION_OWN_STATES) == {
        STATUS_DEAD,
        STATUS_QUESTION,
        STATUS_NOTICE,
        STATUS_WORKING,
        STATUS_FINISHED_UNREAD,
        STATUS_IDLE,
        STATUS_UNKNOWN,
    }
    assert len(SESSION_OWN_STATES) == 7


def test_child_own_states_and_descendant_axis_share_the_same_vocabulary():
    """Not a coincidence - see the module docstring. A child reporting
    about itself and an ancestor's reduced view of its descendants use
    the identical three-value set, so no translation layer is needed."""
    assert set(CHILD_OWN_STATES) == set(DESCENDANT_AXIS_STATES)
    assert set(CHILD_OWN_STATES) == {STATUS_WORKING, STATUS_IDLE, STATUS_UNKNOWN}


def test_child_vocabulary_never_uses_the_word_stopped():
    """'stopped' is a durable LIFECYCLE value elsewhere in this app (a
    tmux instance that is GONE). Reusing it here for a perishable child
    rest-state would be exactly the cross-axis conflation
    session-status-model.md warns about."""
    assert "stopped" not in CHILD_OWN_STATES


def test_only_two_animations_exist():
    """No ANIMATION_UNCERTAIN. An unknown descendant does not get its own
    animation value - see ANIMATION_STEADY's docstring."""
    assert set(ALL_ANIMATIONS) == {ANIMATION_BREATHING, ANIMATION_STEADY}
    assert len(ALL_ANIMATIONS) == 2


# ---- table totality (the property the correction asked for) -----------

def test_light_table_has_exactly_the_full_cross_product():
    assert len(LIGHT_TABLE) == 21 + 9
    session_rows = [r for r in LIGHT_TABLE if r.node_kind == NODE_KIND_SESSION]
    child_rows = [r for r in LIGHT_TABLE if r.node_kind == NODE_KIND_CHILD]
    assert len(session_rows) == 21
    assert len(child_rows) == 9


def test_no_duplicate_rows():
    keys = [(r.node_kind, r.own_state, r.descendant_axis) for r in LIGHT_TABLE]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("node_kind", ALL_NODE_KINDS)
def test_missing_light_rows_is_empty_for_every_node_kind(node_kind):
    """The completeness proof itself. If this ever fails, a state was
    added to a vocabulary without adding its rows - exactly the failure
    mode the owner's correction required this contract to catch."""
    assert missing_light_rows(node_kind) == ()


@pytest.mark.parametrize("node_kind", ALL_NODE_KINDS)
def test_status_light_is_total_over_the_full_cross_product(node_kind):
    """The totality property, proven by actually calling the function
    over every combination in the vocabulary - not merely asserting the
    table's row count matches, which could pass with the WRONG rows."""
    own_states = OWN_STATES_BY_NODE_KIND[node_kind]
    for own_state, descendant_axis in itertools.product(own_states, DESCENDANT_AXIS_STATES):
        row = status_light(own_state, descendant_axis, node_kind)
        assert row.color in own_states
        assert row.animation in ALL_ANIMATIONS
        assert row.descendant_axis == descendant_axis
        assert isinstance(row.contradiction, bool)


def test_status_light_raises_for_a_node_kind_not_in_the_domain():
    with pytest.raises(ValueError):
        status_light(STATUS_IDLE, STATUS_WORKING, "grandparent")


def test_missing_light_rows_raises_for_an_unknown_node_kind():
    with pytest.raises(ValueError):
        missing_light_rows("bogus")


def test_a_future_vocabulary_addition_without_rows_is_caught():
    """Simulates the exact failure the correction's point 2 describes:
    a state added to a vocabulary with no corresponding rows added.
    Proves missing_light_rows would catch it, without mutating the real
    table."""
    own_states = SESSION_OWN_STATES + ("brand_new_state",)
    present = {(r.own_state, r.descendant_axis) for r in LIGHT_TABLE if r.node_kind == NODE_KIND_SESSION}
    full = {(o, d) for o in own_states for d in DESCENDANT_AXIS_STATES}
    missing = full - present
    assert missing == {
        ("brand_new_state", STATUS_WORKING),
        ("brand_new_state", STATUS_UNKNOWN),
        ("brand_new_state", STATUS_IDLE),
    }


# ---- the invariants the table happens to hold today --------------------
# (See the module docstring: these are properties of the CURRENT table,
# not enforced by status_light itself. A future row is free to break
# either one; these tests document today's shape and would need updating
# if that ever happens on purpose.)

def test_color_always_equals_own_state_today():
    for row in LIGHT_TABLE:
        assert row.color == row.own_state


def test_animation_depends_only_on_descendant_axis_today():
    by_descendant_axis = {}
    for row in LIGHT_TABLE:
        by_descendant_axis.setdefault(row.descendant_axis, set()).add(row.animation)
    for descendant_axis, animations in by_descendant_axis.items():
        assert len(animations) == 1, (
            f"descendant_axis={descendant_axis!r} maps to more than one "
            f"animation: {animations!r}"
        )


def test_working_descendant_always_breathes_with_no_own_state_exception():
    """The owner's literal words: 'will always make it breathe'. No
    own_state - not even dead - is exempted."""
    for row in LIGHT_TABLE:
        if row.descendant_axis == STATUS_WORKING:
            assert row.animation == ANIMATION_BREATHING


def test_unknown_and_idle_descendant_share_animation_but_not_the_row():
    """The exact distinction the correction's point 3 requires: on the
    ANIMATION axis, unknown contributes nothing (same as idle); in the
    DATA, unknown is never coerced into idle - two different rows."""
    for own_state in SESSION_OWN_STATES:
        idle_row = status_light(own_state, STATUS_IDLE, NODE_KIND_SESSION)
        unknown_row = status_light(own_state, STATUS_UNKNOWN, NODE_KIND_SESSION)
        assert idle_row.animation == unknown_row.animation == ANIMATION_STEADY
        assert idle_row.descendant_axis == STATUS_IDLE
        assert unknown_row.descendant_axis == STATUS_UNKNOWN
        assert idle_row.descendant_axis != unknown_row.descendant_axis


def test_dual_axis_never_collapses_color_and_animation_together():
    """The whole point of the correction: idle-with-working-descendant and
    working-with-working-descendant must NOT render the same color, even
    though both breathe."""
    idle_row = status_light(STATUS_IDLE, STATUS_WORKING, NODE_KIND_SESSION)
    working_row = status_light(STATUS_WORKING, STATUS_WORKING, NODE_KIND_SESSION)
    assert idle_row.animation == working_row.animation == ANIMATION_BREATHING
    assert idle_row.color != working_row.color
    assert idle_row.color == STATUS_IDLE
    assert working_row.color == STATUS_WORKING


# ---- the one contradiction cell (correction point 4) -------------------

def test_exactly_one_row_is_a_contradiction():
    contradictions = [r for r in LIGHT_TABLE if r.contradiction]
    assert len(contradictions) == 1
    row = contradictions[0]
    assert (row.node_kind, row.own_state, row.descendant_axis) == (
        NODE_KIND_SESSION, STATUS_DEAD, STATUS_WORKING,
    )


def test_dead_parent_with_working_descendant_still_breathes():
    """The owner's explicit instruction: do NOT add a rule that a stopped
    (dead) parent forces the descendant axis quiet. Keep it for cleanup
    visibility."""
    row = status_light(STATUS_DEAD, STATUS_WORKING, NODE_KIND_SESSION)
    assert row.animation == ANIMATION_BREATHING
    assert row.contradiction is True


def test_dead_parent_with_unknown_or_idle_descendant_is_not_a_contradiction():
    for descendant_axis in (STATUS_UNKNOWN, STATUS_IDLE):
        row = status_light(STATUS_DEAD, descendant_axis, NODE_KIND_SESSION)
        assert row.contradiction is False


def test_child_vocabulary_has_no_contradiction_row():
    """No terminal own_state exists in CHILD_OWN_STATES, so no analogous
    cell arises for a child-with-grandchild combination today."""
    assert not any(r.contradiction for r in LIGHT_TABLE if r.node_kind == NODE_KIND_CHILD)


# ---- reduce_descendant_axis --------------------------------------------

def test_reduce_descendant_axis_empty_is_idle():
    assert reduce_descendant_axis([]) == STATUS_IDLE


def test_reduce_descendant_axis_all_idle_is_idle():
    assert reduce_descendant_axis([STATUS_IDLE, STATUS_IDLE]) == STATUS_IDLE


def test_reduce_descendant_axis_any_unknown_no_working_is_unknown():
    assert reduce_descendant_axis([STATUS_IDLE, STATUS_UNKNOWN]) == STATUS_UNKNOWN


def test_reduce_descendant_axis_any_working_wins_over_unknown():
    """'Any working wins' - the owner's KISS answer, no counting."""
    assert reduce_descendant_axis([STATUS_UNKNOWN, STATUS_WORKING]) == STATUS_WORKING


def test_reduce_descendant_axis_rejects_a_foreign_value():
    with pytest.raises(ValueError):
        reduce_descendant_axis([STATUS_DEAD])


def test_reduce_descendant_axis_has_no_depth_parameter():
    """Structural enforcement of the owner's confirmed decision that
    depth does not matter to the reduction: the function CANNOT take a
    depth argument because it has no such parameter. spawnDepth remains
    available in meta.json for a future design - see the design doc."""
    params = set(inspect.signature(reduce_descendant_axis).parameters)
    assert not any("depth" in p.lower() for p in params)


def test_owners_own_worked_example():
    """Idle parent, two children working, one stops. The design doc
    section 3.6 claims this is derivable, not a gap - prove it."""
    two_working = reduce_descendant_axis([STATUS_WORKING, STATUS_WORKING])
    row1 = status_light(STATUS_IDLE, two_working, NODE_KIND_SESSION)

    one_stopped = reduce_descendant_axis([STATUS_WORKING, STATUS_IDLE])
    row2 = status_light(STATUS_IDLE, one_stopped, NODE_KIND_SESSION)

    assert (row1.color, row1.animation) == (row2.color, row2.animation) == (
        STATUS_IDLE, ANIMATION_BREATHING,
    )


# ---- hook state registry ------------------------------------------------

def test_hook_state_registry_covers_every_event_exactly_once():
    names = [role.name for role in HOOK_STATE_REGISTRY]
    assert sorted(names) == sorted(ALL_HOOK_EVENTS)
    assert len(names) == len(set(names))


def test_exactly_ten_events_carry_state():
    carrying = [role.name for role in HOOK_STATE_REGISTRY if role.carries_state]
    assert sorted(carrying) == sorted(
        [
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "SubagentStart",
            "SubagentStop",
            "Notification",
            "PermissionRequest",
            "Stop",
            "SessionStart",
            "SessionEnd",
        ]
    )


def test_perishable_events_carry_the_120s_heartbeat():
    perishable = [role for role in HOOK_STATE_REGISTRY if role.perishable]
    names = sorted(role.name for role in perishable)
    assert names == sorted(
        ["PreToolUse", "PostToolUse", "SubagentStart", "Notification", "PermissionRequest"]
    )
    for role in perishable:
        assert role.decay_seconds == 120
        assert role.axis == AXIS_ACTIVITY


def test_terminal_activity_events_do_not_decay():
    for name in ("SubagentStop", "Stop", "UserPromptSubmit"):
        role = HOOK_STATE_BY_NAME[name]
        assert role.carries_state is True
        assert role.perishable is False
        assert role.decay_seconds is None
        assert role.axis == AXIS_ACTIVITY


def test_lifecycle_events_are_not_perishable():
    for name in ("SessionStart", "SessionEnd"):
        role = HOOK_STATE_BY_NAME[name]
        assert role.axis == AXIS_LIFECYCLE
        assert role.perishable is False
        assert role.decay_seconds is None


def test_unsubscribed_events_carry_no_state():
    subscribed = {
        "UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart",
        "SubagentStop", "Notification", "PermissionRequest", "Stop",
        "SessionStart", "SessionEnd",
    }
    for role in HOOK_STATE_REGISTRY:
        if role.name not in subscribed:
            assert role.carries_state is False
            assert role.axis == "none"
            assert role.perishable is False
            assert role.decay_seconds is None
