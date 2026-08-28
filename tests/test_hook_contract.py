"""The hook contract: registry completeness, classification, drift.

The properties pinned here are the ones that make the contract worth
having. A registry that can silently omit an event, or a drift check that
cannot report removal, would look exactly like a working one.
"""

import pytest

from src.core.claude_hooks import (
    ACTIVITY_ONLY_EVENTS,
    LIFECYCLE_EVENTS,
    TOAST_EVENTS,
)
from src.core.hook_contract import (
    ALL_HOOK_EVENTS,
    BY_NAME,
    HOOK_REGISTRY,
    KNOWN_SUBSCRIBED,
    KNOWN_UNSUBSCRIBED,
    ROLE_ACTIVITY,
    ROLE_LIFECYCLE,
    ROLE_TOAST,
    SESSION_END_REASONS,
    SESSION_START_SOURCES,
    UNKNOWN_EVENT,
    classify_payload,
    diff_event_set,
    unreviewed_events,
)


# ---- the registry cannot drift from the event list --------------------

def test_every_shipped_event_has_exactly_one_registry_row():
    """Adding an event without deciding what to do about it is impossible.

    This is the invariant that makes the registry a DECLARATION rather
    than a list. Without it, an event could be added to ALL_HOOK_EVENTS
    and simply never considered - which is the exact shape of the
    coverage gaps this project keeps finding elsewhere (a container in no
    policy table, a host in no monitoring declaration).
    """
    names = [e.name for e in HOOK_REGISTRY]
    assert sorted(names) == sorted(ALL_HOOK_EVENTS)
    assert len(names) == len(set(names)), "an event is registered twice"


def test_the_registry_matches_what_the_app_actually_wires():
    """The contract must describe the REAL subscriptions, not an intent.

    A contract that says we subscribe to something we do not is worse
    than no contract: it is a document that will be trusted.
    """
    wired = set(TOAST_EVENTS) | set(ACTIVITY_ONLY_EVENTS) | set(LIFECYCLE_EVENTS)
    declared = {e.name for e in HOOK_REGISTRY if e.subscribed}
    assert declared == wired, (
        f"registry and claude_hooks disagree: "
        f"only-registry={declared - wired}, only-wired={wired - declared}"
    )


def test_roles_line_up_with_the_tuple_each_event_lives_in():
    for name in TOAST_EVENTS:
        assert BY_NAME[name].role == ROLE_TOAST
    for name in ACTIVITY_ONLY_EVENTS:
        assert BY_NAME[name].role == ROLE_ACTIVITY
    for name in LIFECYCLE_EVENTS:
        assert BY_NAME[name].role == ROLE_LIFECYCLE


def test_every_event_carries_a_reason():
    """An unsubscribed event with no reason is an oversight in disguise."""
    for e in HOOK_REGISTRY:
        assert e.why.strip(), f"{e.name} has no recorded reason"


def test_stopfailure_is_recorded_as_a_known_gap_not_a_choice():
    """The one unsubscribed event whose absence is a measured defect.

    A turn killed by a rate limit fires StopFailure and NOT Stop, so no
    unread flag is written and the session decays to idle - it reads as
    finished work when it is failed work. Pinned so the gap cannot be
    quietly reclassified as intentional.
    """
    e = BY_NAME["StopFailure"]
    assert e.subscribed is False
    assert "KNOWN GAP" in e.why
    assert "StopFailure" not in unreviewed_events()


# ---- classification ---------------------------------------------------

def test_the_three_verdicts_are_distinguishable():
    """Unknown must not collapse into unsubscribed.

    They mean different things: one is a decision, the other is a newer
    Claude Code this build has never heard of. Rendering them the same
    way is how a new release becomes invisible.
    """
    assert classify_payload({"hook_event_name": "Stop"}).verdict == KNOWN_SUBSCRIBED
    assert classify_payload({"hook_event_name": "FileChanged"}).verdict == KNOWN_UNSUBSCRIBED
    assert classify_payload({"hook_event_name": "NotAThing"}).verdict == UNKNOWN_EVENT


def test_only_subscribed_events_are_actionable():
    assert classify_payload({"hook_event_name": "Stop"}).actionable is True
    assert classify_payload({"hook_event_name": "FileChanged"}).actionable is False
    assert classify_payload({"hook_event_name": "NotAThing"}).actionable is False


def test_a_new_field_is_reported_but_never_rejected():
    """A payload that grew a field must still be actionable.

    Claude adding a field is Claude telling us something new. Dropping
    the payload would turn a feature addition into an outage.
    """
    c = classify_payload({
        "hook_event_name": "SessionStart", "source": "startup",
        "cwd": "/x", "session_id": "s", "transcript_path": "/t",
        "brand_new_field_from_the_future": 1,
    })
    assert c.actionable is True
    assert c.unexpected_fields == ("brand_new_field_from_the_future",)


def test_a_missing_required_field_is_reported_but_never_rejected():
    """A partial payload still carries a session id we can act on."""
    c = classify_payload({"hook_event_name": "SessionStart", "session_id": "s"})
    assert c.missing_required == ("source",)
    assert c.actionable is True


def test_an_optional_field_absence_is_not_a_finding():
    """SessionStart omits session_title for an unnamed session.

    Absent means 'no statement', never 'the name was removed'. Reading it
    the other way would invent a rename nobody performed.
    """
    c = classify_payload({
        "hook_event_name": "SessionStart", "source": "resume",
        "session_id": "s", "transcript_path": "/t", "cwd": "/x",
    })
    assert c.missing_required == ()
    assert c.unexpected_fields == ()


@pytest.mark.parametrize("bad", [None, [], "Stop", 42])
def test_junk_payloads_classify_rather_than_raise(bad):
    assert classify_payload(bad).verdict == UNKNOWN_EVENT


# ---- drift ------------------------------------------------------------

def test_no_drift_against_itself():
    assert diff_event_set(list(ALL_HOOK_EVENTS)).drifted is False


def test_a_removed_event_is_reported_as_the_dangerous_direction():
    """The whole reason diff_event_set exists.

    A subscription to an event that no longer fires does not error. It
    simply never arrives, and every state it fed becomes permanently
    unreachable while the code reading it still looks complete.
    """
    observed = [e for e in ALL_HOOK_EVENTS if e != "SubagentStart"]
    d = diff_event_set(observed)
    assert d.removed == ("SubagentStart",)
    assert "UNREACHABLE" in d.summary()


def test_a_new_event_is_reported_too():
    d = diff_event_set(list(ALL_HOOK_EVENTS) + ["SomethingNew"])
    assert d.added == ("SomethingNew",)
    assert d.removed == ()


def test_no_observation_is_cannot_determine_not_total_removal():
    """An empty read is a failed measurement, not 'this build has none'.

    Treating it as removal would false-alarm on every event at once,
    which is how a drift check gets muted permanently.
    """
    assert diff_event_set(None) is None
    assert diff_event_set([]) is None


# ---- the enums --------------------------------------------------------

def test_session_start_sources_match_the_lineage_layer():
    from src.core.db_models import (
        SESSION_FORK_KIND_CLEAR,
        SESSION_FORK_KIND_COMPACT,
        SESSION_FORK_KIND_FORK,
    )

    for kind in (SESSION_FORK_KIND_FORK, SESSION_FORK_KIND_CLEAR, SESSION_FORK_KIND_COMPACT):
        assert kind in SESSION_START_SOURCES


def test_session_end_reasons_are_not_the_mirror_of_start_sources():
    """They are different enums and must not be treated as inverses.

    SessionEnd carries logout and prompt_input_exit, which have no
    start-side counterpart, and lacks startup/compact/fork.
    """
    assert "logout" in SESSION_END_REASONS
    assert "prompt_input_exit" in SESSION_END_REASONS
    assert "startup" not in SESSION_END_REASONS
    assert set(SESSION_END_REASONS) != set(SESSION_START_SOURCES)
