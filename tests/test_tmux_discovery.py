"""Finding tmux, and admitting when we cannot tell.

WHY THIS FILE EXISTS

Two separate defects, both of which present as a packaged app that silently
runs the wrong backend.

1. DISCOVERY DEPENDED ON ONE LINE IN THE LAUNCHER. tmux is not on the bare
   PATH a GUI-launched process inherits - measured on this machine::

       env -i /usr/bin/which tmux        -> nothing, exit 1
       env -i /bin/sh -c 'echo $PATH'    -> /usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.
       /opt/homebrew/bin/tmux -V         -> tmux 3.7c

   The app only ever found it because macOS/server-manager.js prepends
   ``/opt/homebrew/bin:/usr/local/bin`` to PATH before spawning the server.
   That works, and it is the PATH of one launcher: any other way of starting
   the server (an adopted process, a launchd job, start.sh from a GUI
   context) has no tmux, and the app degrades to the PTY backend with only a
   warning. Discovery has to work from the process itself.

2. ``shutil.which`` TRUTHINESS WAS TREATED AS PROOF TMUX RUNS. which() only
   reports that a name resolves to a file with the executable bit set. A
   quarantined binary, a wrong-architecture build, a broken dylib link or a
   dangling symlink all resolve and all fail to execute. The old factory
   went straight from ``bool(shutil.which("tmux"))`` to
   ``session_backend_selected backend=tmux``, which is a verdict nobody
   measured - the exact false green this repo's THREE-OUTCOME RULE exists
   to kill.

So the probe has three outcomes: available (it resolved AND it ran),
absent (nothing resolved), undetermined (something resolved and could not be
run). Undetermined must never be reported as tmux.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from src.core import tmux_discovery
from src.core.tmux_discovery import (
    TMUX_ABSENT,
    TMUX_AVAILABLE,
    TMUX_UNDETERMINED,
    probe_tmux,
    reset_probe_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Drop the memoized probe around every test.

    The probe is cached for the process lifetime in production, which would
    otherwise leak one test's fake tmux into the next.
    """
    reset_probe_cache()
    yield
    reset_probe_cache()


def _make_fake_tmux(path: Path, body: str) -> Path:
    """Write an executable stub standing in for the tmux binary.

    Args:
        path: Where to write it.
        body: Shell script body, without the shebang.

    Returns:
        The path written.
    """
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_absent_when_nothing_resolves(tmp_path, monkeypatch):
    """An empty PATH with no well-known copy is ABSENT, not undetermined."""
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    probe = probe_tmux()
    assert probe.state == TMUX_ABSENT
    assert probe.path is None
    assert probe.detail


def test_available_requires_the_binary_to_actually_run(tmp_path, monkeypatch):
    """A resolvable tmux that reports a version is AVAILABLE."""
    fake = _make_fake_tmux(tmp_path / "tmux", 'echo "tmux 3.7c"')
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    probe = probe_tmux()
    assert probe.state == TMUX_AVAILABLE
    assert probe.path == str(fake)
    assert probe.version == "tmux 3.7c"


def test_resolvable_but_unrunnable_is_UNDETERMINED_not_available(tmp_path, monkeypatch):
    """The load-bearing case: it resolves, it does not run.

    This is what ``bool(shutil.which("tmux"))`` could never distinguish, and
    reporting it as tmux is a backend claim the app cannot honour.
    """
    _make_fake_tmux(tmp_path / "tmux", "exit 127")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    probe = probe_tmux()
    assert probe.state == TMUX_UNDETERMINED, (
        "a tmux that resolves but will not run was reported as "
        f"{probe.state!r}; the only two honest answers are UNDETERMINED or a "
        "measured failure, never 'available'"
    )
    assert probe.path is not None
    assert "127" in probe.detail or "exit" in probe.detail.lower()


def test_timeout_is_UNDETERMINED_not_absent(tmp_path, monkeypatch):
    """A hung tmux is 'could not evaluate', never 'not installed'."""
    _make_fake_tmux(tmp_path / "tmux", "/bin/sleep 30")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    monkeypatch.setattr(tmux_discovery, "PROBE_TIMEOUT_SECONDS", 1.0)
    probe = probe_tmux()
    assert probe.state == TMUX_UNDETERMINED
    assert "timed out" in probe.detail.lower()


def test_found_off_PATH_at_a_well_known_location(tmp_path, monkeypatch):
    """Discovery must not depend on the launcher's PATH.

    This is the GUI-launch case: PATH holds nothing useful, and the binary
    is where Homebrew puts it. Before this, the app found tmux only because
    macOS/server-manager.js prepends Homebrew's bin to PATH.
    """
    brew = tmp_path / "brewbin"
    brew.mkdir()
    fake = _make_fake_tmux(brew / "tmux", 'echo "tmux 3.7c"')
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", (str(fake),))
    probe = probe_tmux()
    assert probe.state == TMUX_AVAILABLE
    assert probe.path == str(fake)


def test_well_known_list_covers_the_real_install_locations():
    """The shipped list must name the places tmux actually lives on macOS."""
    joined = " ".join(tmux_discovery.WELL_KNOWN_PATHS)
    for expected in (
        "/opt/homebrew/bin/tmux",   # Apple Silicon Homebrew
        "/usr/local/bin/tmux",      # Intel Homebrew
        "/opt/local/bin/tmux",      # MacPorts
        "/usr/bin/tmux",            # system / Linux packages
    ):
        assert expected in joined, f"{expected} is not in WELL_KNOWN_PATHS"


def test_probe_is_cached(tmp_path, monkeypatch):
    """The probe runs a subprocess, so it must not run per session create."""
    _make_fake_tmux(tmp_path / "tmux", 'echo "tmux 3.7c"')
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    calls = []
    real_run = subprocess.run

    def counting_run(*a, **kw):
        calls.append(a)
        return real_run(*a, **kw)

    monkeypatch.setattr(tmux_discovery.subprocess, "run", counting_run)
    probe_tmux()
    probe_tmux()
    probe_tmux()
    assert len(calls) == 1, f"probe executed tmux {len(calls)} times, expected 1"


def test_factory_refuses_to_claim_tmux_when_undetermined(tmp_path, monkeypatch):
    """The whole point: an unrunnable tmux must not select the tmux backend."""
    from src.core.session_backend import build_backend
    from src.utils.pty_session import PTYBackend

    _make_fake_tmux(tmp_path / "tmux", "exit 127")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmux_discovery, "WELL_KNOWN_PATHS", ())
    backend = build_backend(None, "ses_x", tmp_path, lambda _c: None)
    assert isinstance(backend, PTYBackend), (
        "the factory selected the tmux backend against a tmux that cannot "
        "run - a backend claim the app cannot honour"
    )
