"""End-to-end sandbox exercise of the REAL, unmodified ``nuke.sh``.

Why this file exists
--------------------
``nuke.sh`` is the uninstaller. For months it reported a complete reset
while leaving the state directory - ``cloude.db``, ``refresh_tokens.db``,
``migration_trail.jsonl`` - untouched on disk, because it targeted a
hardcoded ``~/Library/Application Support/Cloude Code`` (with a space)
while the app has always used ``CloudeCode`` (no space). Surviving refresh
tokens are a security defect, not untidiness.

Nobody caught it because nobody could run the script: three of its targets
were non-redirectable (a machine-wide ``pgrep -f "Cloude Code" | xargs kill
-9``, absolute ``/tmp`` literals, and ``launchctl`` against the real GUI
session), so any rehearsal on a developer machine would have damaged that
machine.

The bar this file is written to
-------------------------------
Run the REAL script, byte-for-byte unmodified, with every destructive
effect confined to a temp sandbox, and judge the result FROM THE DISK
against a manifest taken before the run - never from the exit code. A
destructive script's exit code says nothing about what it destroyed.

On the two shims below
----------------------
``PATH`` is prefixed with a single stub: ``pgrep``. It exists only so that
a run of a HISTORICAL copy of this script (which honours no override at
all) cannot reach real processes with ``kill -9``. The current script must
not need it, and ``test_nuke_does_not_rely_on_path_stubs`` asserts it was
never invoked - so it is measured scaffolding, not hidden scaffolding. A
rehearsal that quietly depends on shims is a test of the shims.

``lsof`` is deliberately NOT stubbed: the script's port probe is read-only,
and ``_run_nuke`` refuses to run at all unless the sandbox port is verified
free, so the real ``lsof`` returns nothing and no kill follows.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NUKE_SH = REPO_ROOT / "nuke.sh"

# A port picked to be unremarkable and verified free immediately before the
# run. nuke.sh calls a real `lsof -ti:<port>`; pointing it at a port with a
# live listener would kill a real process.
SANDBOX_PORT = 45219

# Files the sandbox plants that nuke.sh must NOT touch. If any of these
# moves, the script's blast radius is wider than declared.
BYSTANDERS = {
    "home/Documents/keep-me.txt": "user document",
    "home/Library/Application Support/SomeOtherApp/data.db": "other app",
    "home/Library/LaunchAgents/com.example.other.plist": "other agent",
    "tmp/unrelated.log": "someone else's log",
    "install/README.md": "repo file",
    "install/src/main.py": "source file",
}


def _sha256(path: Path) -> str:
    """Description: hash a file's bytes.

    Inputs: path (Path).
    Output: str - hex digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> dict[str, str]:
    """Description: record every path under ``root`` and, for regular files,
    a hash of its contents. This is the evidence the assertions are made
    against; the script's exit code is not evidence.

    Inputs: root (Path) - sandbox root.
    Output: dict mapping relative path -> "dir" or a sha256 hex digest.
    """
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "link:" + os.readlink(p)
        elif p.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = _sha256(p)
    return out


def _write(path: Path, content: str) -> Path:
    """Description: create a file and its parents.

    Inputs: path (Path); content (str).
    Output: Path - the file written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _port_is_free(port: int) -> bool:
    """Description: report whether anything is listening on ``port``, using
    the same tool nuke.sh uses so the answer is about the same reality.

    Inputs: port (int).
    Output: bool - True when lsof reports no holder.
    """
    proc = subprocess.run(
        ["lsof", f"-ti:{port}"], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() == ""


@pytest.fixture()
def sandbox(tmp_path: Path) -> dict:
    """Description: build a complete fake install + fake HOME + fake /tmp,
    populated with one instance of every target nuke.sh declares plus a set
    of bystanders that must survive.

    Inputs: tmp_path (pytest fixture).
    Output: dict with keys root, install, home, tmp, state, env, stub_marker.
    """
    root = tmp_path / "sbx"
    install = root / "install"
    home = root / "home"
    tmp = root / "tmp"
    state = root / "state"
    stubs = root / "stubs"

    # --- the install: a copy of the real script and everything it sources ---
    install.mkdir(parents=True)
    shutil.copy2(NUKE_SH, install / "nuke.sh")
    (install / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "resolve-port.sh", install / "scripts")
    shutil.copytree(
        REPO_ROOT / "scripts" / "upgrade_lib", install / "scripts" / "upgrade_lib"
    )
    assert _sha256(install / "nuke.sh") == _sha256(NUKE_SH), (
        "the sandbox copy of nuke.sh must be byte-identical to the real one; "
        "a test of an edited script proves nothing about the shipped script"
    )

    _write(
        install / ".env",
        "\n".join(
            [
                f"PORT={SANDBOX_PORT}",
                f"CLOUDE_STATE_DIR={state}",
                f"LOG_DIRECTORY={root / 'logs'}",
                f"DEFAULT_WORKING_DIR={root / 'projects'}",
                "",
            ]
        ),
    )
    _write(install / "config.json", json.dumps({"session": {"tmux_socket_name": "sbx"}}))
    _write(install / "totp-qr.png", "png")
    _write(install / "session_metadata.json", "{}")
    _write(install / ".env.tmp", "tmp")
    _write(install / "venv" / "bin" / "python3", "#!/bin/sh\n")

    # --- state directory: the thing the old script never removed -----------
    _write(state / "cloude.db", "sqlite")
    _write(state / "refresh_tokens.db", "tokens")
    _write(state / "migration_trail.jsonl", "{}\n")

    # --- HOME-derived targets ----------------------------------------------
    _write(home / "Library" / "LaunchAgents" / "com.cloudecode.menubar.plist", "plist")
    _write(home / "Library" / "Application Support" / "cloude-code-menubar" / "x", "x")
    _write(home / "Library" / "Application Support" / "Cloude Code" / "legacy", "x")

    # --- /tmp-derived targets ----------------------------------------------
    for name in (
        "cloudecode-server.log",
        "cloudecode-menubar.log",
        "cloudecode-menubar-error.log",
        "electron-test.log",
        "cloudecode-nuke.log",
    ):
        _write(tmp / name, name)
    _write(tmp / "cloude-app-extract" / "payload", "x")

    # --- .env-declared dirs -------------------------------------------------
    _write(root / "logs" / "server.log", "log")
    _write(root / "projects" / "proj" / "file.txt", "work")

    for rel, content in BYSTANDERS.items():
        _write(root / rel, content)

    # --- PATH stubs (see this module's docstring) ---------------------------
    stubs.mkdir()
    marker = root / "stub-invoked.txt"
    pgrep_stub = stubs / "pgrep"
    pgrep_stub.write_text(f'#!/bin/sh\necho "pgrep $*" >> "{marker}"\nexit 1\n')
    pgrep_stub.chmod(0o755)

    # A python3 that always fails, used only by the unresolvable-state-dir
    # test. Kept out of the default PATH.
    poison = root / "poison-path"
    poison.mkdir()
    (poison / "python3").write_text("#!/bin/sh\nexit 3\n")
    (poison / "python3").chmod(0o755)

    # --- tmux: a recording stub, so the kill branch is exercised without any
    # real tmux ever running. NEVER point this at a real tmux against a
    # real socket from a test.
    tmux_stub = stubs / "tmux-record"
    tmux_log = root / "tmux-argv.txt"
    tmux_stub.write_text(f'#!/bin/sh\necho "$*" >> "{tmux_log}"\nexit 0\n')
    tmux_stub.chmod(0o755)

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{stubs}:{env['PATH']}",
            "CLOUDE_NUKE_HOME": str(home),
            "CLOUDE_NUKE_TMP_DIR": str(tmp),
            "CLOUDE_NUKE_LAUNCHCTL": "/usr/bin/true",
            # Empty disables the machine-wide process match. The script
            # reports that as CANNOT DETERMINE rather than pretending it ran.
            "CLOUDE_NUKE_PGREP_PATTERN": "",
        }
    )
    env.pop("CLOUDE_STATE_DIR", None)
    env.pop("CLOUDE_NUKE_KILL_TMUX", None)
    env.pop("CLOUDE_NUKE_TMUX_SOCKET", None)

    return {
        "root": root,
        "install": install,
        "home": home,
        "tmp": tmp,
        "state": state,
        "env": env,
        "stub_marker": marker,
        "tmux_stub": tmux_stub,
        "tmux_log": tmux_log,
        "poison_path": poison,
    }


def _run_nuke(sbx: dict, *args: str, stdin: str = "NUKE\n") -> subprocess.CompletedProcess:
    """Description: execute the sandboxed copy of the real nuke.sh.

    Inputs: sbx (fixture dict); args (str) - extra argv; stdin (str) - fed
      to the typed-NUKE confirmation gate.
    Output: CompletedProcess.
    """
    if not _port_is_free(SANDBOX_PORT):
        pytest.skip(
            f"CANNOT DETERMINE: port {SANDBOX_PORT} has a live listener, so "
            "running nuke.sh would kill a real process. Not run."
        )
    return subprocess.run(
        [str(sbx["install"] / "nuke.sh"), *args],
        cwd=sbx["install"],
        env=sbx["env"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


# --- the actual tests --------------------------------------------------------


def test_nuke_removes_state_directory_and_auth_tokens(sandbox):
    """The regression that motivated this file. Judged from the disk."""
    before = _manifest(sandbox["root"])
    assert "state/refresh_tokens.db" in before

    result = _run_nuke(sandbox)
    after = _manifest(sandbox["root"])

    assert result.returncode == 0, result.stderr
    # The state directory and everything in it must be gone. This is the
    # assertion that goes red against the pre-fix script.
    assert not sandbox["state"].exists(), (
        "the state directory survived the nuke - cloude.db and "
        "refresh_tokens.db are still on disk after a reported full reset"
    )
    for gone in (
        "state",
        "state/cloude.db",
        "state/refresh_tokens.db",
        "state/migration_trail.jsonl",
    ):
        assert gone not in after


def test_nuke_removes_every_declared_target(sandbox):
    """Every other declared target, also judged from the disk."""
    _run_nuke(sandbox)
    after = _manifest(sandbox["root"])

    expected_gone = [
        "install/.env",
        "install/config.json",
        "install/totp-qr.png",
        "install/session_metadata.json",
        "install/.env.tmp",
        "install/venv",
        "home/Library/LaunchAgents/com.cloudecode.menubar.plist",
        "home/Library/Application Support/cloude-code-menubar",
        "home/Library/Application Support/Cloude Code",
        "tmp/cloudecode-server.log",
        "tmp/cloudecode-menubar.log",
        "tmp/cloudecode-menubar-error.log",
        "tmp/electron-test.log",
        "tmp/cloudecode-nuke.log",
        "tmp/cloude-app-extract",
        "logs",
        "projects",
    ]
    survived = [t for t in expected_gone if t in after]
    assert not survived, f"targets survived the nuke: {survived}"


def test_nuke_touches_nothing_it_did_not_declare(sandbox):
    """Blast radius. Every bystander must be byte-identical afterwards."""
    before = _manifest(sandbox["root"])
    _run_nuke(sandbox)
    after = _manifest(sandbox["root"])

    for rel in BYSTANDERS:
        assert rel in after, f"bystander destroyed: {rel}"
        assert after[rel] == before[rel], f"bystander modified: {rel}"


def test_nuke_does_not_rely_on_path_stubs(sandbox):
    """The pgrep/lsof stubs must be unused.

    If the current script only stayed inside the sandbox because those two
    stubs caught it, this file would be a test of the stubs rather than of
    the script. Measure it instead of assuming it.
    """
    _run_nuke(sandbox)
    assert not sandbox["stub_marker"].exists(), (
        "nuke.sh reached a PATH-stubbed machine-wide tool: "
        + sandbox["stub_marker"].read_text()
    )


def test_dry_run_deletes_nothing(sandbox):
    """--dry-run must leave the manifest bit-identical."""
    before = _manifest(sandbox["root"])
    result = _run_nuke(sandbox, "--dry-run", stdin="")
    after = _manifest(sandbox["root"])
    assert result.returncode == 0, result.stderr
    assert after == before
    assert "would remove" in result.stdout


def test_unresolvable_state_dir_fails_loudly_and_deletes_nothing(sandbox):
    """A nuke that cannot resolve a target must abort, not carry on.

    Removing the venv python and clearing PATH leaves resolve_state_dir()
    with no interpreter. The correct outcome is a hard, explained failure
    with nothing deleted - not a green run that silently skipped the state
    directory, which is precisely how the original defect presented.
    """
    before = _manifest(sandbox["root"])
    env = dict(sandbox["env"])
    # A python3 on PATH that always fails. Note this is NOT the same as an
    # empty PATH: emptying PATH would also break `dirname`, and the script
    # would die for an unrelated reason, proving nothing about this branch.
    env["PATH"] = f'{sandbox["poison_path"]}:{env["PATH"]}'

    result = subprocess.run(
        [str(sandbox["install"] / "nuke.sh"), "--skip-confirm"],
        cwd=sandbox["install"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    after = _manifest(sandbox["root"])
    assert result.returncode != 0
    assert "NOTHING has been deleted" in result.stderr
    assert after == before


def test_tmux_socket_is_not_killed_by_default(sandbox):
    """A tmux socket cannot be attributed to one install, so the default is
    to name it and leave it alone - CANNOT DETERMINE, not a silent pass."""
    env = dict(sandbox["env"])
    env["CLOUDE_NUKE_TMUX_BIN"] = str(sandbox["tmux_stub"])
    sandbox["env"] = env
    result = _run_nuke(sandbox)
    assert "CANNOT DETERMINE" in result.stdout
    assert "kill-server" in result.stdout  # told the user the exact command
    assert not sandbox["tmux_log"].exists(), "tmux was invoked despite the default"


def test_tmux_kill_is_opt_in_and_uses_the_configured_socket(sandbox):
    """With the opt-in set, the socket name comes from config.json."""
    env = dict(sandbox["env"])
    env["CLOUDE_NUKE_KILL_TMUX"] = "true"
    env["CLOUDE_NUKE_TMUX_BIN"] = str(sandbox["tmux_stub"])
    sandbox["env"] = env
    _run_nuke(sandbox)
    argv = sandbox["tmux_log"].read_text()
    assert "-L sbx kill-server" in argv, argv


# --- the GUI gate ------------------------------------------------------------
#
# THESE ARE STRUCTURAL ASSERTIONS, NOT BEHAVIOURAL ONES. They read source
# text; they do not launch Electron and they do not prove a pixel rendered.
# Driving the real tray menu would mean clicking an item whose next step is
# an irreversible reset of the developer's own machine, so it is not done
# here and this file does not pretend otherwise: the gate's runtime
# behaviour is CANNOT DETERMINE from this suite.

MAIN_JS = REPO_ROOT / "macOS" / "main.js"
CONFIRM_JS = REPO_ROOT / "macOS" / "nuke-confirm.js"


def test_gui_nuke_requires_typing_the_word():
    """--skip-confirm bypasses the shell gate, so the GUI must carry one of
    equivalent weight: a typed word, not a button."""
    main_src = MAIN_JS.read_text()
    assert "promptForNukeConfirmation" in main_src
    # The old one-click path must be gone.
    assert "'NUKE IT'" not in main_src
    assert "buttons: ['Cancel', 'NUKE IT']" not in main_src

    confirm_src = CONFIRM_JS.read_text()
    # Confirm control starts disabled and only enables on an exact match.
    assert "input.value !== 'NUKE'" in confirm_src
    assert "id=\"go\" disabled" in confirm_src
    # An unanswered or dismissed gate is a NO.
    assert "win.on('closed', () => finish(false))" in confirm_src


def test_gui_warning_names_what_is_destroyed_and_nothing_it_does_not():
    """The old dialog still promised to delete a Cloudflare tunnel and DNS
    records (both demolished in plan v3.2) and never mentioned the refresh
    tokens it actually destroys. A warning that is wrong in both directions
    trains people to click through it."""
    confirm_src = CONFIRM_JS.read_text()
    assert "refresh tokens" in confirm_src
    assert "Cloudflare" not in confirm_src
    assert "DNS records" not in confirm_src


def test_nuke_then_fresh_install_is_genuinely_fresh(sandbox):
    """The point of an uninstaller: the next install must start from zero.

    Two independent facts, because either one alone is a false green. The
    wizard's first-run path must engage (config.json and the secrets are
    gone, so setup evaluates INCOMPLETE), AND no prior database or refresh
    tokens may remain. The old script satisfied the first and failed the
    second, which is exactly why "the wizard came up" was never proof.
    """
    from src.core.setup_state import evaluate_setup_state

    _run_nuke(sandbox)

    state = evaluate_setup_state(sandbox["install"] / "config.json", None, None)
    assert not state.is_complete, "setup wizard would not engage after a nuke"
    assert state.status == "incomplete"

    for leftover in ("cloude.db", "refresh_tokens.db", "migration_trail.jsonl"):
        assert not (sandbox["state"] / leftover).exists()
    assert not (sandbox["install"] / "config.json").exists()
    assert not (sandbox["install"] / ".env").exists()
    assert not (sandbox["install"] / "venv").exists()
