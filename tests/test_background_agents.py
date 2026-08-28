"""Background-agent discovery: the sessions with no terminal of their own.

The property that matters most here is the one an empty list cannot
express: a FAILED query and a genuinely empty result are different facts.
Collapsing them tells a user nothing is running while an agent burns
tokens, which is the same false-green this project keeps finding.
"""

import json
import subprocess
from unittest import mock

import pytest

from src.core.background_agents import (
    KIND_BACKGROUND,
    QUERY_OK,
    QUERY_UNAVAILABLE,
    QUERY_UNPARSEABLE,
    AgentsResult,
    list_background_agents,
)

FORK_AGENT = {
    "pid": 37457,
    "id": "7c3dc773",
    "cwd": "/x/ScratchLab",
    "kind": "background",
    "startedAt": 1787933593112,
    "sessionId": "7c3dc773-c7d5-4de6-bb47-201e5cf8ebe4",
    "name": "Base",
    "status": "idle",
    "state": "done",
}
INTERACTIVE_AGENT = {
    "pid": 1,
    "cwd": "/x",
    "kind": "interactive",
    "startedAt": 1,
    "sessionId": "aaaa-bbbb",
    "name": "a tmux session",
    "status": "idle",
}


def _run(stdout="[]", returncode=0):
    return mock.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        ),
    )


def test_a_background_agent_is_found_and_parsed():
    """The real record shape, taken verbatim from the live CLI."""
    with _run(json.dumps([FORK_AGENT])):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.measured is True
    assert len(r.agents) == 1
    a = r.agents[0]
    assert a.short_id == "7c3dc773"
    assert a.name == "Base"
    assert a.state == "done"
    assert a.pid == 37457


def test_interactive_sessions_are_excluded():
    """Those are the tmux sessions CloudeCode already lists.

    Showing them here would double every session in the UI, which is
    worse than not showing the background ones at all.
    """
    with _run(json.dumps([INTERACTIVE_AGENT, FORK_AGENT])):
        r = list_background_agents(claude_path="/fake/claude")
    assert [a.short_id for a in r.agents] == ["7c3dc773"]


def test_a_genuinely_empty_list_is_measured():
    with _run("[]"):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.agents == []
    assert r.measured is True, "no agents is a real answer"


# ---- the three-outcome half --------------------------------------------

def test_a_timeout_is_not_an_empty_list():
    """THE DISCRIMINATING CASE.

    Both produce zero agents. Only one of them means 'none are running'.
    A test that only checked `agents == []` would pass against code that
    silently swallowed every failure.
    """
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 10)):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.agents == []
    assert r.measured is False
    assert r.status == QUERY_UNAVAILABLE
    assert "did not answer" in (r.detail or "")


def test_a_missing_binary_is_not_an_empty_list():
    r = list_background_agents(claude_path="/no/such/claude")
    assert r.measured is False
    assert r.status == QUERY_UNAVAILABLE


def test_a_nonzero_exit_is_not_an_empty_list():
    with _run("[]", returncode=2):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.measured is False
    assert "exited 2" in (r.detail or "")


def test_unparseable_output_is_its_own_state():
    """Distinct from unavailable: the CLI answered, we could not read it.

    That points at a format change rather than a missing binary, and the
    two need different fixes.
    """
    with _run("not json at all"):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.status == QUERY_UNPARSEABLE
    assert r.measured is False


def test_a_json_object_instead_of_an_array_is_unparseable():
    with _run('{"agents": []}'):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.status == QUERY_UNPARSEABLE


# ---- defensive parsing --------------------------------------------------

def test_a_record_gaining_or_losing_fields_does_not_break_the_listing():
    """The CLI is another program on its own release schedule.

    A listing that throws because one record changed shape would take the
    whole feature down for a cosmetic upstream change.
    """
    odd = {"kind": KIND_BACKGROUND, "sessionId": "s1", "brand_new_field": 1}
    with _run(json.dumps([odd])):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.measured is True
    assert r.agents[0].session_id == "s1"
    assert r.agents[0].name is None


def test_a_record_with_no_session_id_is_dropped_not_faked():
    with _run(json.dumps([{"kind": KIND_BACKGROUND, "name": "nameless"}])):
        r = list_background_agents(claude_path="/fake/claude")
    assert r.agents == []
    assert r.measured is True


def test_measured_is_the_flag_callers_must_branch_on():
    assert AgentsResult(status=QUERY_OK).measured is True
    assert AgentsResult(status=QUERY_UNAVAILABLE).measured is False
    assert AgentsResult(status=QUERY_UNPARSEABLE).measured is False
