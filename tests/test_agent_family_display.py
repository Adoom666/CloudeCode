"""feat/agent-family-pills - the DISPLAY resolver, and its split from launch.

``get_family`` / ``resolve_agent_type`` (src/core/agent_families.py) are
LAUNCH-TIME resolvers: something has to run when a session launches, so an
unresolvable ``agent_type`` falls back to the claude family. That fallback
is correct for launching and WRONG for a status pill - an unresolvable
value rendered as a confident "claude" pill is a fact nobody measured,
which is exactly the THREE-OUTCOME RULE violation this repo's CLAUDE.md
calls out for the display layer specifically.

``resolve_family_for_display`` is the fix: a second, separate resolver
that returns ``(None, "unknown")`` instead of guessing. This file pins:
  - the two resolvers disagreeing on the SAME input, in one test, so a
    future "simplification" that merges them fails here
  - every source the display resolver can return, including the two that
    must render VISUALLY as guesses (fingerprint, derived_deepest)
  - that DEFAULT_FAMILY is never smuggled into a display field via any
    source other than a value that genuinely resolved to it
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# Same pattern as tests/test_agent_families.py; this repo has no
# conftest.py, so each module that touches src.config bootstraps its own.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_famdisp_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_famdisp_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.agent_families import (
    AGENT_FAMILY_NAMES,
    DEFAULT_FAMILY,
    get_family,
    resolve_agent_type,
)
from src.core.agent_family_display import (
    DISPLAY_FAMILY_SOURCES,
    FAMILY_SOURCE_DERIVED_DEEPEST,
    FAMILY_SOURCE_FINGERPRINT,
    FAMILY_SOURCE_RESERVED_NAME,
    FAMILY_SOURCE_UNKNOWN,
    FAMILY_SOURCE_WRAPPER,
    resolve_family_for_display,
)
from src.core.agent_wrappers import AgentWrapper


def _w(wid, family="claude", **kw):
    """Build an AgentWrapper with sane test defaults.

    Inputs: wid (str) - wrapper id; family (str); **kw - field overrides.
    Output: AgentWrapper.
    """
    base = {"id": wid, "family": family, "label": wid, "script": f"{wid} run"}
    base.update(kw)
    return AgentWrapper(**base)


# ---------------------------------------------------------------------- #
# THE defect this file exists to lock: an unresolvable agent_type must
# NEVER become a confident "claude" pill, even though it MUST still
# launch as claude.
# ---------------------------------------------------------------------- #

def test_launch_and_display_resolvers_disagree_on_an_unknown_wrapper_id():
    """A wrapper id belonging to no configured wrapper and no family name.

    Both assertions live in ONE test on purpose: a future refactor that
    merges the two resolvers back together must fail here, not in two
    separate files that could each be "fixed" independently by deleting
    the inconvenient one.
    """
    bogus = "deleted-wrapper-that-no-longer-exists"
    wrappers = [_w("cld", family="claude")]

    # Launch-time: still resolves to something runnable.
    launch_family = get_family(bogus)
    assert launch_family.name == DEFAULT_FAMILY == "claude"

    # Display-time: honestly reports it cannot determine this.
    display_family, source = resolve_family_for_display(bogus, wrappers)
    assert display_family is None
    assert source == FAMILY_SOURCE_UNKNOWN

    # And resolve_agent_type (the launch pairing/wrapper resolver) also
    # still resolves to claude with no explicit wrapper - unchanged.
    resolved_family, explicit_wrapper = resolve_agent_type(bogus, wrappers)
    assert resolved_family.name == "claude"
    assert explicit_wrapper is None


def test_get_family_launch_behaviour_is_unchanged_for_every_family_name():
    """Direct regression pin: get_family must still map every valid name
    to itself and every invalid one to DEFAULT_FAMILY - the exact
    behaviour this feature must NOT touch."""
    for name in AGENT_FAMILY_NAMES:
        assert get_family(name).name == name
    assert get_family(None).name == DEFAULT_FAMILY
    assert get_family("").name == DEFAULT_FAMILY
    assert get_family("totally-unrecognized").name == DEFAULT_FAMILY


# ---------------------------------------------------------------------- #
# Every source outcome, individually.
# ---------------------------------------------------------------------- #

def test_reserved_name_source_is_a_direct_family_name_match():
    family, source = resolve_family_for_display("codex", [])
    assert family is not None and family.name == "codex"
    assert source == FAMILY_SOURCE_RESERVED_NAME


def test_wrapper_source_is_an_explicit_stored_choice():
    wrappers = [_w("mywrap", family="hermes")]
    family, source = resolve_family_for_display("mywrap", wrappers)
    assert family is not None and family.name == "hermes"
    assert source == FAMILY_SOURCE_WRAPPER


def test_derived_deepest_source_is_a_wrapper_with_no_recorded_family():
    # Raw dict shape, as a pre-migration config read would produce -
    # AgentWrapper itself always coerces family to a valid string, so
    # this case can only arise from an unvalidated dict (see
    # wrappers_for_family's docstring for the same class of input).
    wrappers = [{"id": "legacy-wrap"}]
    family, source = resolve_family_for_display("legacy-wrap", wrappers)
    assert family is not None and family.name == DEFAULT_FAMILY
    assert source == FAMILY_SOURCE_DERIVED_DEEPEST


def test_fingerprint_source_requires_the_caller_to_say_so():
    # Same input as the reserved_name case, but the caller marks it as
    # having come from scrollback fingerprinting - the resolver cannot
    # infer this on its own, since the value is textually identical to
    # an explicit launch choice.
    family, source = resolve_family_for_display("openclaw", [], from_fingerprint=True)
    assert family is not None and family.name == "openclaw"
    assert source == FAMILY_SOURCE_FINGERPRINT

    # Without the flag, the SAME value resolves as a fact, not a guess.
    family2, source2 = resolve_family_for_display("openclaw", [])
    assert source2 == FAMILY_SOURCE_RESERVED_NAME
    assert source2 != source


def test_unknown_source_covers_missing_blank_and_unresolvable():
    for bad_value in (None, "", "   ", "not-a-family-or-wrapper-id"):
        family, source = resolve_family_for_display(bad_value, [])
        assert family is None
        assert source == FAMILY_SOURCE_UNKNOWN


def test_unknown_source_when_wrapper_matches_but_family_is_unrecognized():
    """A wrapper matched by id, but its recorded family is not one this
    build knows (e.g. config written by a newer version). Finding the
    wrapper does not mean we can answer - still unknown, not a fact."""
    wrappers = [{"id": "future-wrap", "family": "some-future-family"}]
    family, source = resolve_family_for_display("future-wrap", wrappers)
    assert family is None
    assert source == FAMILY_SOURCE_UNKNOWN


# ---------------------------------------------------------------------- #
# The THREE-OUTCOME contract, enforced structurally.
# ---------------------------------------------------------------------- #

def test_no_source_outside_the_declared_five_is_ever_returned():
    """Source-level assertion: sweep a wide range of inputs and confirm
    every returned source is one of the five declared outcomes - never a
    stray string, never None."""
    wrappers = [
        _w("cld", family="claude"),
        _w("codexwrap", family="codex"),
        {"id": "legacy"},
        {"id": "future", "family": "nonsense"},
    ]
    probes = [None, "", "claude", "codex", "hermes", "openclaw", "shell",
              "cld", "codexwrap", "legacy", "future", "garbage-id"]
    for value in probes:
        for fp in (False, True):
            _, source = resolve_family_for_display(value, wrappers, from_fingerprint=fp)
            assert source in DISPLAY_FAMILY_SOURCES, (value, fp, source)


def test_default_family_is_never_smuggled_into_an_unknown_result():
    """The exact defect: DEFAULT_FAMILY must never be the family returned
    alongside an "unknown"-flavoured resolution. Every non-None family
    returned must come with a source that actually justifies it."""
    wrappers = [_w("cld", family="claude")]
    probes = ["bogus", "", None, "not-a-wrapper", "12345"]
    for value in probes:
        family, source = resolve_family_for_display(value, wrappers)
        if source == FAMILY_SOURCE_UNKNOWN:
            assert family is None, (
                f"source was 'unknown' but a family was still returned: {family}"
            )


# ---------------------------------------------------------------------- #
# Wire models carry the pair, and default to the unresolved state.
# ---------------------------------------------------------------------- #

def test_session_info_and_attachable_session_carry_the_display_pair():
    from src.models import AttachableSession, SessionInfo, SessionStats, Session, SessionStatus
    from datetime import datetime

    info = SessionInfo(
        session=Session(
            id="s1",
            working_dir="/tmp",
            status=SessionStatus.RUNNING,
            agent_type="deleted-wrapper",
        ),
        stats=SessionStats(),
        agent_type="deleted-wrapper",
        agent_family=None,
        agent_family_source="unknown",
    )
    assert info.agent_family is None
    assert info.agent_family_source == "unknown"

    row = AttachableSession(
        name="cloude_x",
        created_by_cloude=True,
        created_at_epoch=1,
        window_count=1,
    )
    # Defaults, never a guessed family.
    assert row.agent_family is None
    assert row.agent_family_source is None


def test_session_carries_fingerprint_provenance_and_defaults_false():
    from src.models import Session, SessionStatus

    launched = Session(id="s1", working_dir="/tmp", status=SessionStatus.RUNNING,
                        agent_type="claude")
    assert launched.agent_type_via_fingerprint is False

    adopted = Session(id="s2", working_dir="/tmp", status=SessionStatus.RUNNING,
                       agent_type="codex", agent_type_via_fingerprint=True)
    assert adopted.agent_type_via_fingerprint is True


# ---------------------------------------------------------------------- #
# End-to-end through SessionManager._session_info_for - the actual read
# path the API serves. Reuses the fixture pattern from
# tests/test_session_ownership_source.py (this repo has no conftest.py,
# so each integration test builds its own bare manager).
# ---------------------------------------------------------------------- #

import pytest  # noqa: E402


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``, PLUS a
    real ``agents.wrappers`` list so the display resolver has something to
    consult (the ownership-source stub deliberately omits this - it never
    reads agent_type)."""

    def __init__(self, log_dir, wrappers=None):
        self._log_dir = log_dir
        self.port = 5001
        from types import SimpleNamespace
        self.agents = SimpleNamespace(wrappers=wrappers or [])

    def get_pinned_themes_path(self):
        return self._log_dir / "pinned_themes.json"

    def get_unread_state_path(self):
        return self._log_dir / "unread_state.json"

    @property
    def log_directory(self):
        return str(self._log_dir)

    def get_session_metadata_path(self):
        return self._log_dir / "session_metadata.json"


class _FakeBackend:
    def __init__(self, tmux_session):
        self.tmux_session = tmux_session

    def is_alive(self):
        return True


def _bare_manager_with_wrappers(monkeypatch, tmp_path, wrappers=None):
    from src.core.session_manager import SessionManager

    (tmp_path / "logs").mkdir(exist_ok=True)
    stub = _StubSettings(log_dir=tmp_path / "logs", wrappers=wrappers)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


@pytest.mark.asyncio
async def test_session_info_for_unresolvable_agent_type_renders_unknown(monkeypatch, tmp_path):
    """The end-to-end version of the defect: a live session whose
    agent_type no longer matches any wrapper or family must come back
    from SessionManager as (None, "unknown") on the wire, never as a
    smuggled "claude"."""
    from src.models import Session, SessionStatus

    mgr = _bare_manager_with_wrappers(monkeypatch, tmp_path)
    sess = Session(
        id="ses_x",
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_ses_x",
        agent_type="wrapper-that-was-deleted",
    )
    mgr.sessions["ses_x"] = sess
    mgr.backends["ses_x"] = _FakeBackend("cloude_ses_x")
    mgr._subscribers.setdefault("ses_x", [])
    monkeypatch.setattr(
        mgr, "_build_tmux_status_map",
        lambda: {"cloude_ses_x": {"status": "idle", "pid": 1}},
    )

    info = await mgr.get_session_info(session_id="ses_x")
    assert info is not None
    assert info.agent_type == "wrapper-that-was-deleted"
    assert info.agent_family is None
    assert info.agent_family_source == "unknown"


@pytest.mark.asyncio
async def test_session_info_for_resolvable_agent_type_renders_the_fact(monkeypatch, tmp_path):
    """The healthy counterpart: a live session actually launched as
    codex resolves as a fact, not a guess."""
    from src.models import Session, SessionStatus

    mgr = _bare_manager_with_wrappers(monkeypatch, tmp_path)
    sess = Session(
        id="ses_y",
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_ses_y",
        agent_type="codex",
    )
    mgr.sessions["ses_y"] = sess
    mgr.backends["ses_y"] = _FakeBackend("cloude_ses_y")
    mgr._subscribers.setdefault("ses_y", [])
    monkeypatch.setattr(
        mgr, "_build_tmux_status_map",
        lambda: {"cloude_ses_y": {"status": "idle", "pid": 1}},
    )

    info = await mgr.get_session_info(session_id="ses_y")
    assert info is not None
    assert info.agent_family == "codex"
    assert info.agent_family_source == "reserved_name"
