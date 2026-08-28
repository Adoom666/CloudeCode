"""The one test that matters: a SAVED setting reaches a NEW terminal.

Everything else about this feature could be green while the feature does
nothing. Asserting that a value landed in config.json proves a file write.
Asserting that ``get_env_for_spawn`` returns a dict proves a dict. Neither
proves that a shell, started by tmux, in a pane, actually sees the
variable - which is the whole product.

So this file spawns a REAL tmux session on a per-process throwaway socket
(``derive_test_socket``, never the user's), runs ``printenv`` inside the
pane, and reads what that process saw. The evidence is the spawned
process's own environment, written by the spawned process, not anything
this test or the app asserts about it.

Three properties are checked end to end:

  1. A user env var arrives.
  2. ``development_root`` arrives as ``CLOUDE_DEV_ROOT``.
  3. ``default_shell`` arrives as ``SHELL``, which is what makes
     ``agents.shell_command``'s default ``$SHELL -i`` launch the
     configured shell - the mechanism, not a second code path. This one
     is asserted by having the pane EXPAND ``$SHELL`` rather than by
     reading the variable back, because expansion is the thing the
     feature depends on.

And one negative: the app's own control variables survive a config that
tries to shadow them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wsenv_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wsenv_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.config import settings
from src.core.session_manager import SessionManager
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not on PATH"
)


@pytest.fixture
def tmux_socket():
    """A private socket, killed with everything on it when the test ends."""
    name = derive_test_socket("wsenv")
    yield name
    tmux = shutil.which("tmux")
    if tmux:
        subprocess.run(
            [tmux, "-L", name, "kill-server"],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def workspace_config(tmp_path, monkeypatch):
    """Point ``settings`` at a throwaway config.json and write into it.

    Returns:
        A callable taking the ``workspace`` block to store.
    """
    path = tmp_path / "config.json"

    def _write(workspace: dict) -> Path:
        path.write_text(
            json.dumps({"projects": [], "agents": {}, "workspace": workspace})
        )
        settings._auth_config_cache = None
        return path

    monkeypatch.setattr(settings, "auth_config_file", str(path))
    settings._auth_config_cache = None
    yield _write
    settings._auth_config_cache = None


def _spawn_and_capture(socket_name: str, shell_snippet: str, work_dir: Path) -> str:
    """Spawn one real tmux session running ``shell_snippet`` and read its output.

    Args:
        socket_name: The throwaway tmux socket to use.
        shell_snippet: sh code whose stdout is redirected into a file. It
            receives ``$OUT`` as the path to write.
        work_dir: Working directory for the session.

    Returns:
        The file's contents once written.

    Raises:
        AssertionError: The spawned process never produced the file, which
            is a could-not-evaluate outcome and must never be read as a
            pass.
    """
    out_path = work_dir / f"env-{uuid.uuid4().hex[:8]}.txt"
    manager = SessionManager.__new__(SessionManager)
    manager._hook_tokens = {}
    session_id = f"wsenv-{uuid.uuid4().hex[:6]}"
    spawn_env = manager.get_env_for_spawn(session_id)

    backend = TmuxBackend(
        session_id=session_id,
        working_dir=work_dir,
        on_output=None,
        socket_name=socket_name,
    )

    async def _inner():
        await backend.start(
            command=f"/bin/sh -c 'OUT={out_path}; {shell_snippet}; sleep 20'",
            env=spawn_env,
        )

    try:
        asyncio.run(_inner())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if out_path.exists() and out_path.read_text().strip():
                return out_path.read_text()
            time.sleep(0.2)
    finally:
        asyncio.run(backend.stop())

    raise AssertionError(
        f"the spawned pane never wrote {out_path}, so whether the "
        "environment arrived could not be evaluated. This is not a pass."
    )


@requires_tmux
def test_a_saved_env_var_reaches_a_newly_spawned_terminal(
    tmux_socket, workspace_config, tmp_path
):
    workspace_config({"env": {"CC_E2E_MARKER": "arrived-97531"}})
    captured = _spawn_and_capture(tmux_socket, 'printenv > "$OUT"', tmp_path)
    assert "CC_E2E_MARKER=arrived-97531" in captured


@requires_tmux
def test_the_development_root_reaches_a_newly_spawned_terminal(
    tmux_socket, workspace_config, tmp_path
):
    root = tmp_path / "projects"
    root.mkdir()
    workspace_config({"development_root": str(root)})
    captured = _spawn_and_capture(tmux_socket, 'printenv > "$OUT"', tmp_path)
    assert f"CLOUDE_DEV_ROOT={root}" in captured


@requires_tmux
def test_the_default_shell_is_what_dollar_shell_expands_to_in_the_pane(
    tmux_socket, workspace_config, tmp_path
):
    """Asserts the MECHANISM, not the variable.

    ``agents.shell_command`` defaults to ``$SHELL -i``. Reading SHELL back
    out of printenv would prove the variable is set; expanding it inside
    the pane proves the thing that actually decides which shell runs.
    """
    workspace_config({"default_shell": "/bin/sh"})
    captured = _spawn_and_capture(
        tmux_socket, 'printf "expanded=%s\\n" "$SHELL" > "$OUT"', tmp_path
    )
    assert "expanded=/bin/sh" in captured


@requires_tmux
def test_a_config_cannot_shadow_the_apps_own_control_variables(
    tmux_socket, workspace_config, tmp_path
):
    """Write order, not the name policy, is what guarantees this.

    The config here is written directly to disk, bypassing the API
    validator that would have refused the reserved name - which is the
    point: even a hand-edited config.json cannot hijack the hook channel.
    """
    workspace_config({"env": {"CLOUDECODE_SESSION_ID": "hijacked"}})
    captured = _spawn_and_capture(tmux_socket, 'printenv > "$OUT"', tmp_path)
    assert "CLOUDECODE_SESSION_ID=hijacked" not in captured
    assert "CLOUDECODE_SESSION_ID=wsenv-" in captured


@requires_tmux
def test_a_change_reaches_the_next_terminal_and_not_the_previous_one(
    tmux_socket, workspace_config, tmp_path
):
    """The claim the settings screen makes to the user, measured.

    tmux copies the environment at ``new-session``, so a session already
    running keeps what it was born with. Both halves are asserted from
    the panes' OWN ``printenv`` output rather than from tmux's session
    environment table, because that table records only what tmux was told
    about explicitly and would answer this question about the wrong thing.

    Note the second spawn lands on an ALREADY RUNNING tmux server, which
    is the case where a client-side environment is normally discarded (see
    tests/test_pane_locale_spawn.py). That it still arrives is the part
    worth having a test for.
    """
    workspace_config({"env": {"CC_E2E_PHASE": "first"}})
    before = _spawn_and_capture(tmux_socket, 'printenv > "$OUT"', tmp_path)

    workspace_config({"env": {"CC_E2E_PHASE": "second"}})
    after = _spawn_and_capture(tmux_socket, 'printenv > "$OUT"', tmp_path)

    assert "CC_E2E_PHASE=first" in before
    assert "CC_E2E_PHASE=second" in after
    assert "CC_E2E_PHASE=second" not in before
