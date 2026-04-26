"""Phase 10 — tests for ``agent_type`` plumbing across models, config, and
the session manager backfill.

Covers:
- ``Session.agent_type`` pydantic round-trip for all 4 known types + None
- ``AgentsConfig`` defaults match the corrected CLI invocations
- ``Settings.get_agent_command()`` matrix (known agents, unknown fallback,
  None/empty fallback, ``CLAUDE_CLI_PATH`` env override)
- ``ProjectConfig`` Literal validation rejects unknown agent_type
- ``CreateSessionRequest`` accepts explicit / None / missing ``agent_type``
- Backward compat: legacy session JSON without ``agent_type`` deserializes
- ``backfill_agent_type`` pure-function unit tests (owned vs adopted, idempotency)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_agent_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_agent_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from pydantic import ValidationError

from src.config import (
    AgentsConfig,
    AuthConfig,
    ProjectConfig,
    Settings,
)
from src.models import (
    AttachableSession,
    CreateSessionRequest,
    Session,
    SessionInfo,
    SessionStats,
    SessionStatus,
)
from src.core.session_manager import backfill_agent_type


# --------------------------------------------------------------------------- #
# Pydantic round-trip — Session.agent_type
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "agent_type",
    ["claude", "codex", "hermes", "openclaw", None],
)
def test_session_agent_type_round_trip(agent_type):
    s = Session(
        id="ses_abc12345",
        working_dir="/tmp/foo",
        agent_type=agent_type,
    )
    dumped = s.model_dump()
    assert dumped["agent_type"] == agent_type

    reloaded = Session(**dumped)
    assert reloaded.agent_type == agent_type
    assert reloaded.id == s.id


def test_session_agent_type_defaults_to_none_when_omitted():
    s = Session(id="ses_xyz", working_dir="/tmp/bar")
    assert s.agent_type is None


def test_session_info_agent_type_round_trip():
    """SessionInfo also carries agent_type at the top level (mirrored)."""
    s = Session(id="ses_1", working_dir="/tmp", agent_type="codex")
    info = SessionInfo(session=s, agent_type="codex", stats=SessionStats())
    dumped = info.model_dump()
    assert dumped["agent_type"] == "codex"
    reloaded = SessionInfo(**dumped)
    assert reloaded.agent_type == "codex"


def test_attachable_session_agent_type_round_trip():
    row = AttachableSession(
        name="cloude_test",
        created_by_cloude=True,
        created_at_epoch=1700000000,
        window_count=1,
        agent_type="hermes",
    )
    dumped = row.model_dump()
    assert dumped["agent_type"] == "hermes"
    reloaded = AttachableSession(**dumped)
    assert reloaded.agent_type == "hermes"

    # None is the default for not-yet-fingerprinted rows.
    row_none = AttachableSession(
        name="cloude_test2",
        created_by_cloude=False,
        created_at_epoch=1700000000,
        window_count=1,
    )
    assert row_none.agent_type is None


# --------------------------------------------------------------------------- #
# AgentsConfig defaults
# --------------------------------------------------------------------------- #


def test_agents_config_defaults():
    cfg = AgentsConfig()
    assert cfg.claude_command == "claude --dangerously-skip-permissions"
    assert cfg.codex_command == "codex"
    assert cfg.hermes_command == "hermes"
    assert cfg.openclaw_command == "openclaw tui"


def test_agents_config_overrides_apply():
    cfg = AgentsConfig(
        claude_command="claude-foo",
        codex_command="codex --x",
        hermes_command="hermes-bin",
        openclaw_command="openclaw chat",
    )
    assert cfg.claude_command == "claude-foo"
    assert cfg.codex_command == "codex --x"
    assert cfg.hermes_command == "hermes-bin"
    assert cfg.openclaw_command == "openclaw chat"


# --------------------------------------------------------------------------- #
# Settings.get_agent_command — fallback + override matrix
# --------------------------------------------------------------------------- #


_SENTINEL = object()


def _settings_with_agents(agents_cfg: AgentsConfig, claude_cli_path=_SENTINEL):
    """Build a Settings instance whose load_auth_config returns a fake
    AuthConfig containing the supplied AgentsConfig.

    ``claude_cli_path`` defaults to the literal string ``"claude"`` so the
    env-override branch in ``get_agent_command`` short-circuits to the
    bare model default — making the assertions deterministic regardless
    of whether ``claude`` happens to be on PATH on the test host. Pass
    a real path string to exercise the override behavior; pass ``None``
    to let the production logic resolve via PATH (rarely useful in tests).
    """
    s = Settings(
        default_working_dir=os.environ["DEFAULT_WORKING_DIR"],
        log_directory=os.environ["LOG_DIRECTORY"],
    )
    if claude_cli_path is _SENTINEL:
        s.claude_cli_path = "claude"
    elif claude_cli_path is not None:
        s.claude_cli_path = claude_cli_path
    # When claude_cli_path is explicitly None, leave the field at its
    # default (None) so the test exercises PATH resolution.
    fake_auth = SimpleNamespace(agents=agents_cfg)
    # pydantic v2 BaseSettings rejects assignment of non-field names via
    # ``__setattr__``. Bypass with object.__setattr__ to install a bound
    # stand-in for ``load_auth_config`` on this instance only.
    object.__setattr__(s, "load_auth_config", lambda: fake_auth)
    return s


@pytest.mark.parametrize(
    "agent_type,expected_attr",
    [
        ("claude", "claude_command"),
        ("codex", "codex_command"),
        ("hermes", "hermes_command"),
        ("openclaw", "openclaw_command"),
    ],
)
def test_get_agent_command_known_types(agent_type, expected_attr):
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command(agent_type) == getattr(agents, expected_attr)


def test_get_agent_command_case_insensitive():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("CODEX") == agents.codex_command
    assert s.get_agent_command("OpenClaw") == agents.openclaw_command


def test_get_agent_command_unknown_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("totally-bogus") == agents.claude_command


def test_get_agent_command_none_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command(None) == agents.claude_command


def test_get_agent_command_empty_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("") == agents.claude_command


def test_get_agent_command_tolerates_auth_config_failure():
    """When load_auth_config raises, fall back to AgentsConfig defaults."""
    s = Settings(
        default_working_dir=os.environ["DEFAULT_WORKING_DIR"],
        log_directory=os.environ["LOG_DIRECTORY"],
    )
    # Pin claude_cli_path to "claude" so the env-override branch in
    # get_agent_command is a no-op and we get the default string verbatim.
    s.claude_cli_path = "claude"

    def boom():
        raise RuntimeError("auth config missing")

    object.__setattr__(s, "load_auth_config", boom)
    defaults = AgentsConfig()
    assert s.get_agent_command("codex") == defaults.codex_command
    assert s.get_agent_command("claude") == defaults.claude_command


def test_claude_cli_path_override_applies_when_command_is_default():
    """``CLAUDE_CLI_PATH`` (settings.claude_cli_path) honored for claude
    iff the configured ``claude_command`` is still the model default."""
    agents = AgentsConfig()  # default claude_command
    s = _settings_with_agents(agents, claude_cli_path="/opt/custom/claude")
    cmd = s.get_agent_command("claude")
    assert cmd == "/opt/custom/claude --dangerously-skip-permissions"


def test_claude_cli_path_override_ignored_when_command_customized():
    """If the operator customized ``agents.claude_command`` in config.json,
    the env override yields to the explicit config value."""
    agents = AgentsConfig(claude_command="claude --my-custom-flag")
    s = _settings_with_agents(agents, claude_cli_path="/opt/custom/claude")
    cmd = s.get_agent_command("claude")
    assert cmd == "claude --my-custom-flag"


def test_claude_cli_path_bare_claude_returns_default_string_verbatim():
    """When the resolved CLI path is just ``"claude"`` (no explicit path,
    just the bare command name), return the model default string verbatim
    — no rewrite. Production code: ``if cli_path and cli_path != "claude":``
    short-circuits the rewrite when the resolved path equals "claude"."""
    agents = AgentsConfig()
    s = _settings_with_agents(agents, claude_cli_path="claude")
    cmd = s.get_agent_command("claude")
    assert cmd == "claude --dangerously-skip-permissions"


# --------------------------------------------------------------------------- #
# ProjectConfig Literal validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "agent_type",
    ["claude", "codex", "hermes", "openclaw"],
)
def test_project_config_accepts_known_agent_types(agent_type):
    p = ProjectConfig(
        name="x", path="/tmp", description="d", agent_type=agent_type
    )
    assert p.agent_type == agent_type


def test_project_config_rejects_unknown_agent_type():
    with pytest.raises(ValidationError):
        ProjectConfig(
            name="x",
            path="/tmp",
            description="d",
            agent_type="invalid_type",
        )


def test_project_config_defaults_to_claude():
    p = ProjectConfig(name="x", path="/tmp")
    assert p.agent_type == "claude"


# --------------------------------------------------------------------------- #
# CreateSessionRequest agent_type handling
# --------------------------------------------------------------------------- #


def test_create_session_request_explicit_agent_type():
    req = CreateSessionRequest(agent_type="codex")
    assert req.agent_type == "codex"


def test_create_session_request_none_agent_type():
    req = CreateSessionRequest(agent_type=None)
    assert req.agent_type is None


def test_create_session_request_missing_agent_type_defaults_none():
    req = CreateSessionRequest()
    assert req.agent_type is None


def test_create_session_request_accepts_arbitrary_string():
    """The model itself doesn't constrain agent_type to a Literal — that
    enforcement happens at the project-config layer. A request can carry
    any string; the session manager + get_agent_command handle unknowns
    via fallback. This protects forward-compat (a future agent type can
    arrive in a request before the server is upgraded)."""
    req = CreateSessionRequest(agent_type="future_agent")
    assert req.agent_type == "future_agent"


# --------------------------------------------------------------------------- #
# Backward compat — legacy session JSON without agent_type
# --------------------------------------------------------------------------- #


def test_legacy_session_json_loads_cleanly():
    """A pre-Phase-6 session_metadata.json has no ``agent_type`` field.
    pydantic must deserialize it as None without complaint."""
    legacy = {
        "id": "ses_legacy01",
        "pty_pid": None,
        "working_dir": "/tmp/legacy",
        "status": "running",
        "created_at": "2024-01-01T00:00:00",
        "last_activity": "2024-01-01T00:01:00",
        "tunnels": [],
        # no agent_type key at all
    }
    s = Session(**legacy)
    assert s.agent_type is None
    assert s.id == "ses_legacy01"
    assert s.status == SessionStatus.RUNNING


def test_legacy_session_json_with_extra_unknown_field_loads():
    """Pydantic's default is to ignore unknown fields; a v3.x metadata
    file with a future-added field should still load."""
    legacy = {
        "id": "ses_future",
        "working_dir": "/tmp/x",
        "future_field": "ignored",
    }
    s = Session(**legacy)
    assert s.id == "ses_future"
    assert s.agent_type is None


# --------------------------------------------------------------------------- #
# backfill_agent_type — pure-function unit tests
# --------------------------------------------------------------------------- #


def test_backfill_owned_session_gets_claude():
    s = Session(id="ses_owned1", working_dir="/tmp", agent_type=None)
    n = backfill_agent_type(s, owned_tmux_sessions={"cloude_owned1"})
    assert n == 1
    assert s.agent_type == "claude"


def test_backfill_adopted_session_stays_none():
    s = Session(id="adopted:cloude_userone", working_dir="/tmp", agent_type=None)
    n = backfill_agent_type(s, owned_tmux_sessions=set())
    assert n == 0
    assert s.agent_type is None


def test_backfill_returns_zero_when_already_set():
    """Idempotent: a session whose agent_type is already populated is
    not touched, and the function returns 0."""
    s = Session(id="ses_owned1", working_dir="/tmp", agent_type="codex")
    n = backfill_agent_type(s, owned_tmux_sessions={"cloude_owned1"})
    assert n == 0
    assert s.agent_type == "codex"


def test_backfill_idempotent_on_second_call():
    """Run twice — first call backfills, second is a no-op."""
    s = Session(id="ses_owned2", working_dir="/tmp", agent_type=None)
    n1 = backfill_agent_type(s, owned_tmux_sessions={"cloude_owned2"})
    n2 = backfill_agent_type(s, owned_tmux_sessions={"cloude_owned2"})
    assert n1 == 1
    assert n2 == 0
    assert s.agent_type == "claude"


def test_backfill_returns_zero_for_none_session():
    """Passing None session is a no-op, returns 0."""
    n = backfill_agent_type(None, owned_tmux_sessions=set())
    assert n == 0


def test_backfill_owned_set_none_falls_back_to_id_prefix():
    """When owned_tmux_sessions is None or empty, the adopted-prefix
    heuristic decides: non-adopted ids backfill to claude."""
    owned_session = Session(id="ses_normal", working_dir="/tmp", agent_type=None)
    n = backfill_agent_type(owned_session, owned_tmux_sessions=None)
    assert n == 1
    assert owned_session.agent_type == "claude"

    adopted_session = Session(
        id="adopted:something", working_dir="/tmp", agent_type=None
    )
    n2 = backfill_agent_type(adopted_session, owned_tmux_sessions=None)
    assert n2 == 0
    assert adopted_session.agent_type is None


def test_backfill_handles_all_known_agent_types_unchanged():
    """If a session already has any valid agent_type set, leave it alone."""
    for at in ["claude", "codex", "hermes", "openclaw"]:
        s = Session(id=f"ses_{at}", working_dir="/tmp", agent_type=at)
        assert backfill_agent_type(s, owned_tmux_sessions={"x"}) == 0
        assert s.agent_type == at
