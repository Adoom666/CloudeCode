"""The wrapper list used for display resolution, and the path that was wrong.

THE DEFECT. Three call sites read the configured wrappers as::

    getattr(getattr(settings, "agents", None), "wrappers", None) or []

``Settings`` has no ``agents`` attribute - the agents block lives on
``settings.load_auth_config().agents`` - so that chain evaluated to None,
then ``or []``, on every machine, always. The defensive getattr turned a
WRONG ATTRIBUTE PATH into a plausible empty answer instead of an
AttributeError.

WHY IT SURVIVED. An empty wrapper list is not obviously wrong. Its only
symptom was that ``resolve_family_for_display`` could never match a
wrapper id, so a session launched through a wrapper rendered "unknown
family" - while an ADOPTED session with no agent_type at all rendered
"~claude" off a fingerprint. The session we knew the most about displayed
worse than the one we had only guessed at, which reads like a
fingerprinting quirk rather than a wrong attribute two layers up.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.config import Settings
from src.core.agent_families import resolve_agent_type


def test_settings_has_no_agents_attribute():
    """The premise of the bug, asserted so it cannot be argued away.

    If someone later ADDS ``Settings.agents``, this test fails and whoever
    does it gets to decide deliberately whether the old chain should come
    back - rather than the two spellings silently disagreeing.
    """
    from src.config import settings

    assert not hasattr(settings, "agents"), (
        "Settings grew an 'agents' attribute; the wrapper-lookup helper and "
        "this test both assume the config path is load_auth_config().agents"
    )


def test_the_helper_reads_the_real_config(tmp_path):
    """The corrected path returns the wrappers that are actually there."""
    import src.core.session_manager as sm

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "config_version": 3,
        "agents": {
            "claude_command": "",
            "wrappers": [{
                "id": "claude-skip-permissions",
                "label": "claude",
                "script": "claude --dangerously-skip-permissions",
                "default": True,
                "family": "claude",
            }],
        },
        "projects": [],
    }))
    patched = Settings(
        default_working_dir=str(tmp_path),
        log_directory=str(tmp_path / "logs"),
        totp_secret="x", jwt_secret="y",
        auth_config_file=str(config_path),
    )
    original = sm.settings
    try:
        sm.settings = patched
        got = sm._configured_wrappers()
        assert [w.id for w in got] == ["claude-skip-permissions"], (
            "the helper did not read the configured wrappers"
        )
    finally:
        sm.settings = original


def test_a_wrapper_id_resolves_to_its_family(tmp_path):
    """THE USER-VISIBLE SYMPTOM, at its source.

    With the wrappers actually in hand, a launched session's agent_type -
    which is the WRAPPER ID - resolves to a real family. With the old
    empty list it could not, and the row rendered "unknown family".
    """
    import src.core.session_manager as sm

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "config_version": 3,
        "agents": {
            "claude_command": "",
            "wrappers": [{
                "id": "claude-skip-permissions", "label": "claude",
                "script": "claude --dangerously-skip-permissions",
                "default": True, "family": "claude",
            }],
        },
        "projects": [],
    }))
    patched = Settings(
        default_working_dir=str(tmp_path),
        log_directory=str(tmp_path / "logs"),
        totp_secret="x", jwt_secret="y",
        auth_config_file=str(config_path),
    )
    original = sm.settings
    try:
        sm.settings = patched
        family, explicit = resolve_agent_type(
            "claude-skip-permissions", sm._configured_wrappers()
        )
        assert family.name == "claude"
        assert explicit is not None and explicit.id == "claude-skip-permissions"
    finally:
        sm.settings = original


def test_an_unreadable_config_degrades_to_empty_not_an_exception(tmp_path):
    """The defensiveness that WAS worth keeping.

    A config that will not load must not break a session listing. What
    changed is that the try/except now wraps the IO, not a misspelled
    attribute path - so a wrong path raises during development instead of
    quietly answering [].
    """
    import src.core.session_manager as sm

    patched = Settings(
        default_working_dir=str(tmp_path),
        log_directory=str(tmp_path / "logs"),
        totp_secret="x", jwt_secret="y",
        auth_config_file=str(tmp_path / "does-not-exist.json"),
    )
    original = sm.settings
    try:
        sm.settings = patched
        assert sm._configured_wrappers() == []
    finally:
        sm.settings = original


def test_no_call_site_uses_the_old_broken_chain():
    """Structural: the wrong spelling must not come back.

    Checked by AST, not by scanning lines. A regex over source text also
    matches the docstring above, which QUOTES the broken form on purpose -
    that quote is how the next reader learns why this helper exists, so a
    check that cannot tell code from prose would force the explanation to
    be deleted to stay green. Parsing finds real Call nodes only.
    """
    import ast

    offenders = []
    for rel in ("src/core/session_manager.py", "src/api/routes.py"):
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id == "getattr"):
                continue
            if len(node.args) < 2:
                continue
            target, attr = node.args[0], node.args[1]
            is_settings = isinstance(target, ast.Name) and target.id == "settings"
            is_agents = isinstance(attr, ast.Constant) and attr.value == "agents"
            if is_settings and is_agents:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"the wrong wrapper-lookup path is back at {offenders}; "
        "use _configured_wrappers()"
    )
