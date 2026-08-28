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
    # Default is the empty-string sentinel meaning "not configured" — see
    # get_agent_command's resolution order. An unset claude_command must
    # fall back to the cld/cldor zsh-function launcher, never to a bare
    # "claude" invocation.
    assert cfg.claude_command == ""
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


# The ``claude`` agent_type falls back to building its command from the
# ``cld`` / ``cldor`` zsh functions ONLY when ``agents.claude_command`` is
# unset/empty (the AgentsConfig default). These are the exact wrapper
# strings, empirically verified against a real detached tmux session (see
# TODO.md findings log for the capture-pane evidence). ``CLAUDE_CLI_PATH``
# remains legacy / bypassed for this type — see its field comment in
# src/config.py.
_EXPECT_CLD = "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"


def _rc(command: str) -> str:
    """The rc-sourced rendering of a family's static command.

    Description: every family that launches a USER-INSTALLED CLI now
      sources ``~/.zshrc`` before looking the binary up, because the tmux
      pane shell is non-interactive and reads no rc, so a version-manager
      install (nvm, asdf, mise) is otherwise not on PATH. Built from the
      production helper rather than restated as a literal, so a change to
      the wrapper shape cannot leave these expectations quietly wrong.

      The assertions below stay EXACT equality. Only the expected
      rendering moved; none of these tests is about the rc wrapper, they
      are about WHICH command_field a family resolves from, and that
      question is unchanged.
    Inputs: command (str) - the family's configured static command.
    Output: str - the full tmux-ready shell string.
    Example: _rc("codex")  # "zsh -c 'source ~/.zshrc ...; codex'"
    """
    from src.core.shell_init import rc_prefixed

    return rc_prefixed(command)


@pytest.mark.parametrize(
    "agent_type,expected_attr",
    [
        ("codex", "codex_command"),
        ("hermes", "hermes_command"),
        ("openclaw", "openclaw_command"),
    ],
)
def test_get_agent_command_known_types(agent_type, expected_attr):
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command(agent_type) == _rc(getattr(agents, expected_attr))


def test_get_agent_command_claude_no_model_runs_cld():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("claude") == _EXPECT_CLD


def test_get_agent_command_case_insensitive():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("CODEX") == _rc(agents.codex_command)
    assert s.get_agent_command("OpenClaw") == _rc(agents.openclaw_command)


def test_get_agent_command_unknown_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("totally-bogus") == _EXPECT_CLD


def test_get_agent_command_none_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command(None) == _EXPECT_CLD


def test_get_agent_command_empty_falls_back_to_claude():
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    assert s.get_agent_command("") == _EXPECT_CLD


def test_get_agent_command_tolerates_auth_config_failure():
    """When load_auth_config raises, fall back to AgentsConfig defaults
    for the OTHER agent types; claude doesn't need AgentsConfig at all."""
    s = Settings(
        default_working_dir=os.environ["DEFAULT_WORKING_DIR"],
        log_directory=os.environ["LOG_DIRECTORY"],
    )

    def boom():
        raise RuntimeError("auth config missing")

    object.__setattr__(s, "load_auth_config", boom)
    defaults = AgentsConfig()
    assert s.get_agent_command("codex") == _rc(defaults.codex_command)
    assert s.get_agent_command("claude") == _EXPECT_CLD


def test_get_agent_command_claude_with_model_runs_cldor():
    """model set -> ``cldor <model>``, shlex-quoted (nested — once for the
    inner ``cldor`` arg, once for the outer ``zsh -c`` boundary)."""
    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    cmd = s.get_agent_command("claude", model="openai/gpt-5.6-sol")
    assert cmd == "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cldor openai/gpt-5.6-sol'"


def test_get_agent_command_claude_model_shell_injection_defused():
    """A malicious model string (which CreateSessionRequest / the
    provider-add endpoint would already reject with 400 before this is
    ever called) must still come out fully neutralized here — the double
    shlex.quote nesting is defense-in-depth, not the only guard. Verified
    by round-tripping the produced command through the SAME mechanism
    tmux uses internally (``exec $SHELL -c <command_string>``) and
    confirming the payload never executes / never splits into extra argv
    entries."""
    import subprocess

    agents = AgentsConfig()
    s = _settings_with_agents(agents)
    payload = "foo; touch /tmp/should_never_exist_pwn_marker; echo"
    cmd = s.get_agent_command("claude", model=payload)

    # Swap the real cldor for a harmless probe function that just echoes
    # its argv, then run the EXACT command string through zsh -c (mirrors
    # tmux's internal invocation) to prove the payload stays one literal
    # argument and nothing executes.
    probed = cmd.replace(
        "source ~/.zshrc >/dev/null 2>&1 </dev/null; cldor",
        "cldor() { echo \"ARGC:$#|GOT:[$1]\"; }; cldor",
    )
    proc = subprocess.run(
        ["zsh", "-c", probed], capture_output=True, text=True, timeout=5
    )
    # The payload text legitimately appears INSIDE the single echoed
    # argument (that's the point — it stayed one literal token, ARGC:1).
    # What must NOT happen is the embedded "; touch ..." being split out
    # and actually executed as a second command.
    assert proc.stdout.strip() == f"ARGC:1|GOT:[{payload}]"
    import os as _os
    assert not _os.path.exists("/tmp/should_never_exist_pwn_marker")


def test_claude_cli_path_legacy_still_ignored():
    """``claude_cli_path`` (CLAUDE_CLI_PATH) remains LEGACY / silently
    bypassed for agent_type == "claude" — only agents.claude_command is
    consulted now."""
    agents = AgentsConfig()  # claude_command unset -> cld fallback
    s = _settings_with_agents(agents, claude_cli_path="/opt/custom/claude")
    assert s.get_agent_command("claude") == _EXPECT_CLD


def test_get_agent_command_explicit_claude_command_wins():
    """An explicit, non-empty agents.claude_command takes precedence over
    the cld/cldor fallback, wrapped the same way (source ~/.zshrc first)
    so both a plain binary and a shell function work."""
    agents = AgentsConfig(claude_command="claude --my-custom-flag")
    s = _settings_with_agents(agents)
    assert s.get_agent_command("claude") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; claude --my-custom-flag'"
    )


def test_get_agent_command_explicit_claude_command_wins_over_model():
    """Step 1 (explicit claude_command) wins outright, even when a model
    is also supplied — opting into a custom command opts out of the
    cldor/model routing entirely."""
    agents = AgentsConfig(claude_command="claude --my-custom-flag")
    s = _settings_with_agents(agents)
    assert s.get_agent_command("claude", model="x/y") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; claude --my-custom-flag'"
    )


def test_get_agent_command_empty_claude_command_falls_back_to_cld():
    """Explicitly setting claude_command to "" (or whitespace) must not be
    treated as "set" -- it falls back exactly like the unset default."""
    agents = AgentsConfig(claude_command="")
    s = _settings_with_agents(agents)
    assert s.get_agent_command("claude") == _EXPECT_CLD

    agents_ws = AgentsConfig(claude_command="   ")
    s_ws = _settings_with_agents(agents_ws)
    assert s_ws.get_agent_command("claude") == _EXPECT_CLD


def test_get_agent_command_default_claude_command_falls_back_to_cld():
    """Backward compat: the author's own machine has no reason to ever set
    agents.claude_command, so the untouched AgentsConfig default must keep
    resolving to the cld wrapper with zero config change."""
    s = _settings_with_agents(AgentsConfig())
    assert s.get_agent_command("claude") == _EXPECT_CLD
    assert s.get_agent_command("claude", model="openai/gpt-5.6-sol") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cldor openai/gpt-5.6-sol'"
    )


@pytest.mark.parametrize("agent_type", ["codex", "hermes", "openclaw"])
def test_get_agent_command_other_types_unaffected_by_claude_command(agent_type):
    """Setting agents.claude_command must not leak into the other agent
    types' command resolution."""
    agents = AgentsConfig(claude_command="claude --my-custom-flag")
    s = _settings_with_agents(agents)
    expected = _rc(getattr(agents, f"{agent_type}_command"))
    assert s.get_agent_command(agent_type) == expected


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
# CreateSessionRequest.model — provider-selector modal (v3.1) validation
# --------------------------------------------------------------------------- #


def test_create_session_request_model_none_by_default():
    req = CreateSessionRequest()
    assert req.model is None


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-5.6-sol", "qwen/qwen3.8-max", "a", "a" * 120, "~anthropic/x"],
)
def test_create_session_request_model_accepts_valid_ids(model):
    req = CreateSessionRequest(model=model)
    assert req.model == model


@pytest.mark.parametrize(
    "model",
    [
        "foo; rm -rf /",
        "foo`whoami`",
        "foo$(whoami)",
        "foo bar",
        "foo\nbar",
        "a" * 121,
        "",
    ],
)
def test_create_session_request_model_rejects_invalid_ids(model):
    with pytest.raises(ValidationError):
        CreateSessionRequest(model=model)


def test_session_model_field_round_trip():
    """Session.model persists alongside agent_type."""
    s = Session(
        id="ses_abc", working_dir="/tmp", agent_type="claude", model="openai/gpt-5.6-sol"
    )
    dumped = s.model_dump()
    assert dumped["model"] == "openai/gpt-5.6-sol"
    reloaded = Session(**dumped)
    assert reloaded.model == "openai/gpt-5.6-sol"


def test_session_model_field_defaults_to_none():
    s = Session(id="ses_abc", working_dir="/tmp")
    assert s.model is None


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
