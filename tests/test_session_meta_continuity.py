"""Session-metadata continuity across an upgrade -> downgrade round trip.

THE QUESTION. ``v0.8.1`` reads ``session_metadata.json`` from
``LOG_DIRECTORY`` and nowhere else. The current version resolves the same
file through ``Settings._resolve_state_file()``, which prefers the state
directory and falls back to ``LOG_DIRECTORY``. The round-trip harness
(``scripts/ci/roundtrip-upgrade-downgrade.sh``) ran with zero sessions and
reported this step as CANNOT DETERMINE rather than guessing. This module
is the measurement that replaces the guess.

WHAT IS MEASURED, AND WITH WHOSE CODE. Nothing here paraphrases either
version. The new side runs the real ``Settings`` and the real
``SessionManager._load_session_metadata`` / ``_save_session_metadata``
/ ``_clear_stale_metadata``. The old side runs ``v0.8.1``'s OWN
``get_session_metadata_path`` source, extracted from the tag with
``git show`` and compiled at test time - see ``_old_reader``. If that
extraction cannot be done (shallow clone, tag absent), the affected tests
report CANNOT DETERMINE by skipping with a named reason; they never
degrade into a pass.

THE DETECTOR IS ITSELF TESTED. ``downgrade_verdict()`` returns one of
three values - INTACT, STALE, ABSENT - and
``test_detector_reports_absent_when_pointed_at_an_empty_location`` and
``test_detector_reports_stale_when_the_old_copy_diverges`` prove it can
report the losses it claims to detect. A continuity assertion that has
never been watched go red is not evidence of continuity.

SAFETY. Hermetic. Every path is a ``tmp_path`` fixture; no test here
opens a tmux socket, and the suite-wide guard in ``tests/conftest.py``
would raise if one tried to reach the production ``cloude`` socket.
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_smc_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_smc_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.config import settings
from src.core.session_manager import SessionManager
from src.models import Session

OLD_REF = "v0.8.1"

INTACT = "INTACT"
STALE = "STALE"
ABSENT = "ABSENT"


# ---- the old version's own resolver, not a paraphrase of it -----------


def _tag_is_present() -> bool:
    """Is ``OLD_REF`` resolvable as a commit in THIS clone?

    Description: separates the two reasons the extraction below can come
      back empty. A shallow or tagless clone (which is what
      ``actions/checkout`` produces by default, and what any
      ``--depth=1`` clone produces) is an ENVIRONMENT shortfall - the
      measurement was not possible. A tag that IS present but whose
      ``src/config.py`` cannot be read or parsed is a REPOSITORY fault -
      that must fail, not skip.
    Inputs: none.
    Output: bool.
    """
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{OLD_REF}^{{commit}}"],
            cwd=str(ROOT),
            capture_output=True,
        ).returncode == 0
    except OSError:
        return False


def _extract_old_resolver() -> Tuple[Optional[Any], str]:
    """Compile ``v0.8.1``'s ``get_session_metadata_path`` from the tag.

    Description: reads the OLD version's ``src/config.py`` out of git and
      compiles that one method verbatim, so the "what would the old
      version see" side of every assertion below is the old version's
      real code rather than this module's memory of it. A rewrite of the
      method in the tag is picked up automatically.
    Inputs: none (reads ``OLD_REF`` from the repo this file lives in).
    Output: (callable | None, reason). The callable takes one object with
      a ``log_directory`` attribute and returns a ``Path``. ``None`` plus
      a human reason when the tag or the method could not be read - the
      CANNOT-DETERMINE outcome, never silently a pass.
    """
    try:
        src = subprocess.check_output(
            ["git", "show", f"{OLD_REF}:src/config.py"],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return None, f"could not read {OLD_REF}:src/config.py from git ({exc})"

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return None, f"{OLD_REF}:src/config.py did not parse ({exc})"

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef)
                and item.name == "get_session_metadata_path"
            ):
                module = ast.Module(body=[item], type_ignores=[])
                ast.fix_missing_locations(module)
                ns: Dict[str, Any] = {"Path": Path}
                exec(compile(module, f"<{OLD_REF}:src/config.py>", "exec"), ns)
                return ns["get_session_metadata_path"], "ok"
    return None, (
        f"{OLD_REF}:src/config.py has no Settings.get_session_metadata_path - "
        "the method this whole comparison rests on could not be located"
    )


_OLD_RESOLVER, _OLD_RESOLVER_REASON = _extract_old_resolver()
# True only when the extraction failed BECAUSE the tag is not in this
# clone. Everything else - tag present, method missing, source will not
# parse - is a repository fault and must fail loudly.
_OLD_RESOLVER_TAG_MISSING = _OLD_RESOLVER is None and not _tag_is_present()


class _OldSettingsStub:
    """The one attribute ``v0.8.1``'s resolver reads. Nothing else."""

    def __init__(self, log_directory: str) -> None:
        self.log_directory = log_directory


def _old_reader(log_directory: Path) -> Path:
    """Where the OLD version would look for session metadata.

    Inputs: log_directory (Path) - the install's ``LOG_DIRECTORY``.
    Output: Path, as computed by ``v0.8.1``'s own method.
    Raises: pytest.skip when the old source could not be compiled - the
      CANNOT-DETERMINE outcome, reported by name.
    """
    if _OLD_RESOLVER is None:
        pytest.skip(f"CANNOT DETERMINE: {_OLD_RESOLVER_REASON}")
    return _OLD_RESOLVER(_OldSettingsStub(str(log_directory)))


# ---- the detector -----------------------------------------------------


def downgrade_verdict(log_directory: Path, live_session_id: str) -> str:
    """What the OLD version gets when it starts after the NEW one ran.

    Description: resolves the metadata file the way ``v0.8.1`` resolves
      it, then classifies the result into the three outcomes this repo
      requires. STALE is called out separately from ABSENT on purpose:
      an absent file makes the app start clean and is visible to the
      user, whereas a stale one silently rehydrates a session that is no
      longer the live one.
    Inputs: log_directory (Path) - the install's ``LOG_DIRECTORY``.
      live_session_id (str) - the id the NEW version last persisted.
    Output: one of ``INTACT`` / ``STALE`` / ``ABSENT``.
    Example: downgrade_verdict(log_dir, "sess-1") == INTACT
    """
    path = _old_reader(log_directory)
    if not path.exists():
        return ABSENT
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return ABSENT
    return INTACT if raw.get("id") == live_session_id else STALE


# ---- fixtures ---------------------------------------------------------


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> Tuple[Path, Path]:
    """A throwaway (old LOG_DIRECTORY, new state dir) pair, wired live.

    Inputs: tmp_path, monkeypatch (pytest fixtures).
    Output: (log_dir, state_dir).
    """
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(log_dir))
    monkeypatch.setattr(settings, "state_dir_override", str(state_dir))
    return log_dir, state_dir


def _session(session_id: str, name: str, working_dir: str = "/tmp/wd") -> Session:
    return Session(
        id=session_id,
        working_dir=working_dir,
        tmux_session=name,
        agent_type="claude",
    )


def _write_old_metadata(
    log_dir: Path, session_id: str, name: str, owned: list
) -> Path:
    """Write metadata where ``v0.8.1`` would have written it.

    The payload shape is produced by the CURRENT ``Session`` model plus
    the ``owned_tmux_sessions`` key, which is exactly what
    ``_save_session_metadata`` writes - the two versions share the file
    format; only the directory differs.
    """
    path = log_dir / "session_metadata.json"
    payload = json.loads(_session(session_id, name).model_dump_json())
    payload["owned_tmux_sessions"] = sorted(owned)
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---- the measurements -------------------------------------------------


def test_old_version_resolver_was_really_loaded():
    """Guard: without this, every comparison below silently becomes a skip.

    Three outcomes, not two. The tag being absent from the clone is a
    declared CANNOT DETERMINE - a shallow clone (``--depth=1``, which is
    what ``actions/checkout`` does by default) has no tags at all, and
    failing there would be reporting a repository fault that does not
    exist. Any OTHER extraction failure - tag present but
    ``Settings.get_session_metadata_path`` missing or unparseable - is a
    real fault and fails.

    The skip is not allowed to become CI's quiet normal state: the tests
    workflow asserts ``git rev-parse v0.8.1`` succeeds BEFORE pytest
    runs, and fails the job on a suite whose skips it did not expect.
    """
    if _OLD_RESOLVER_TAG_MISSING:
        pytest.skip(
            f"CANNOT DETERMINE: {_OLD_RESOLVER_REASON}. Tag {OLD_REF} is not "
            "in this clone - fetch it (`git fetch --depth=1 origin tag "
            f"{OLD_REF}`) to run this comparison."
        )
    assert _OLD_RESOLVER is not None, _OLD_RESOLVER_REASON
    probe = _OLD_RESOLVER(_OldSettingsStub("/some/log/dir"))
    assert probe == Path("/some/log/dir/session_metadata.json")


def test_upgrade_reads_metadata_left_at_the_old_location(dirs):
    """UPGRADE. The new version finds and rehydrates old-location state."""
    log_dir, state_dir = dirs
    _write_old_metadata(log_dir, "sess-upgrade", "work-a", ["work-a", "work-b"])

    mgr = SessionManager()
    mgr._load_session_metadata()

    current = mgr.current_session()
    assert current is not None, "the new version did not rehydrate the old file"
    assert current.id == "sess-upgrade"
    assert current.tmux_session == "work-a"
    assert mgr.owned_tmux_sessions == {"work-a", "work-b"}
    assert not (state_dir / "session_metadata.json").exists(), (
        "reading must not copy the file to the new location - that would "
        "create the both-present ambiguity this test set is about"
    )


def test_plain_upgrade_writes_back_to_the_old_location(dirs):
    """DOWNGRADE, the good path. No relocation, so the old version still sees it."""
    log_dir, state_dir = dirs
    _write_old_metadata(log_dir, "sess-old", "work-a", ["work-a"])

    mgr = SessionManager()
    mgr._load_session_metadata()
    mgr.owned_tmux_sessions = {"work-a", "work-c"}
    mgr._save_session_metadata()

    assert not (state_dir / "session_metadata.json").exists()
    written = json.loads((log_dir / "session_metadata.json").read_text())
    assert written["owned_tmux_sessions"] == ["work-a", "work-c"], (
        "the new version's write did not land at the old path"
    )
    assert downgrade_verdict(log_dir, "sess-old") == INTACT


def test_detach_sequence_keeps_metadata_where_the_install_started_from(dirs):
    """DOWNGRADE, the path one ordinary user action used to break.

    ``SessionManager.detach_session`` unlinks the RESOLVED metadata path
    and then, if another session is still live, calls
    ``_save_session_metadata``. That second call used to RE-RESOLVE from
    disk, and by then the old location was gone, so the file silently
    moved to the state directory and a downgrade could no longer find it.

    The resolver now decides ONCE per (filename, configured locations)
    and keeps returning that decision, so an install that started at
    ``LOG_DIRECTORY`` keeps writing there. This runs the same two real
    methods in the same real order and measures the DISK.
    """
    log_dir, state_dir = dirs
    _write_old_metadata(log_dir, "sess-detached", "work-a", ["work-a", "work-b"])

    mgr = SessionManager()
    mgr._load_session_metadata()
    assert mgr.current_session() is not None

    # the exact pair of statements detach_session runs
    mgr._clear_stale_metadata()
    survivor = _session("sess-survivor", "work-b")
    mgr._register_session(survivor, backend=None)
    mgr.owned_tmux_sessions = {"work-b"}
    mgr._save_session_metadata()

    assert (log_dir / "session_metadata.json").exists(), (
        "the file was expected to STAY at the location this install started from"
    )
    assert not (state_dir / "session_metadata.json").exists(), (
        "the detach sequence relocated the file into the state dir again"
    )
    assert downgrade_verdict(log_dir, "sess-survivor") == INTACT


def test_the_pin_survives_the_file_being_unlinked(dirs):
    """The mechanism under the test above, isolated from SessionManager.

    Resolve once with the file only at the old location, delete it, and
    resolve again. A resolver that recomputes from disk answers the state
    dir the second time; a resolver that decided once answers the same
    path both times.
    """
    log_dir, _state_dir = dirs
    old = log_dir / "session_metadata.json"
    old.write_text("{}")

    first = settings.get_session_metadata_path()
    assert first == old
    old.unlink()
    assert settings.get_session_metadata_path() == first, (
        "the resolution flipped as a side effect of the file being removed"
    )


def test_a_fresh_install_with_no_file_anywhere_pins_the_state_dir(dirs):
    """Nothing on disk in either place: the new location wins and sticks."""
    log_dir, state_dir = dirs
    resolved = settings.get_session_metadata_path()
    assert resolved == state_dir / "session_metadata.json"
    assert settings.get_state_file_location("session_metadata.json") == "state_dir"

    # A file appearing at the OLD location afterwards must not move the
    # decision - this process already committed to the state dir.
    (log_dir / "session_metadata.json").write_text("{}")
    assert settings.get_session_metadata_path() == resolved


def test_callers_read_the_authoritative_location_instead_of_re_deriving(dirs):
    """The decision is published, so no call site has to recompute it."""
    log_dir, _state_dir = dirs
    (log_dir / "session_metadata.json").write_text("{}")
    assert settings.get_state_file_location("session_metadata.json") == "log_directory"
    assert settings.get_session_metadata_path().parent == log_dir


def test_changing_the_configured_locations_invalidates_the_pin(tmp_path, monkeypatch):
    """The pin is about DISK CHANGE, never about configuration change.

    An operator who repoints CLOUDE_STATE_DIR or LOG_DIRECTORY has asked
    a different question, and must get a fresh answer rather than a
    remembered one keyed to directories that are no longer configured.
    """
    a_log, a_state = tmp_path / "a-logs", tmp_path / "a-state"
    b_log, b_state = tmp_path / "b-logs", tmp_path / "b-state"
    for d in (a_log, a_state, b_log, b_state):
        d.mkdir()
    (a_log / "session_metadata.json").write_text("{}")

    monkeypatch.setattr(settings, "log_directory", str(a_log))
    monkeypatch.setattr(settings, "state_dir_override", str(a_state))
    assert settings.get_session_metadata_path() == a_log / "session_metadata.json"

    monkeypatch.setattr(settings, "log_directory", str(b_log))
    monkeypatch.setattr(settings, "state_dir_override", str(b_state))
    assert settings.get_session_metadata_path() == b_state / "session_metadata.json"


def test_metadata_present_in_both_locations_leaves_the_old_copy_stale(dirs):
    """DOWNGRADE, the worst path: the old version reads a wrong answer.

    Once the file exists in BOTH places the resolver prefers the new one
    and leaves the old on disk untouched forever. A downgrade then
    rehydrates a session that is no longer the live one - which is worse
    than finding nothing, because finding nothing is visible.
    """
    log_dir, state_dir = dirs
    _write_old_metadata(log_dir, "sess-stale", "work-a", ["work-a"])
    _write_old_metadata(state_dir, "sess-stale", "work-a", ["work-a"])
    stale_bytes = (log_dir / "session_metadata.json").read_bytes()

    mgr = SessionManager()
    mgr._load_session_metadata()
    live = _session("sess-current", "work-z")
    mgr._register_session(live, backend=None)
    mgr.owned_tmux_sessions = {"work-z"}
    mgr._save_session_metadata()

    assert json.loads((state_dir / "session_metadata.json").read_text())["id"] == "sess-current"
    assert (log_dir / "session_metadata.json").read_bytes() == stale_bytes, (
        "the old copy was expected to be left untouched"
    )
    assert downgrade_verdict(log_dir, "sess-current") == STALE


# ---- proof the detector can go red ------------------------------------


def test_detector_reports_absent_when_pointed_at_an_empty_location(dirs):
    """Red-proof 1: an emptied location must NOT read as continuity."""
    log_dir, _ = dirs
    assert downgrade_verdict(log_dir, "sess-anything") == ABSENT


def test_detector_reports_stale_when_the_old_copy_diverges(dirs):
    """Red-proof 2: a file that exists but names another session is STALE."""
    log_dir, _ = dirs
    _write_old_metadata(log_dir, "sess-yesterday", "work-a", ["work-a"])
    assert downgrade_verdict(log_dir, "sess-today") == STALE
    assert downgrade_verdict(log_dir, "sess-yesterday") == INTACT


# ---- the rollback path, measured rather than read ---------------------


COMMON_SH = ROOT / "scripts" / "upgrade_lib" / "upgrade_rollback_common.sh"


def _run_rollback_cycle(tmp_path: Path, seed_dir_name: str):
    """take_backup then restore_backup, for real, against a throwaway install.

    Inputs: tmp_path (Path) - pytest tmp dir. seed_dir_name (str) -
      "logs" to seed session_metadata.json at the OLD LOG_DIRECTORY,
      "state" to seed it at the CURRENT state dir.
    Output: (install, logs, state, proc, backed_up_meta) - the three
      directories, the CompletedProcess, and whether the manifest
      actually recorded the metadata as BACKED_UP (if not, the rollback
      path was never exercised and the caller must report CANNOT
      DETERMINE rather than a verdict).
    """
    install = tmp_path / "install"
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    for d in (install, logs, state):
        d.mkdir()
    (install / ".env").write_text(
        f"LOG_DIRECTORY={logs}\nCLOUDE_STATE_DIR={state}\n"
    )
    (install / "config.json").write_text("{}")
    seed = logs if seed_dir_name == "logs" else state
    # refresh_tokens.db is a REQUIRED backup file and take_backup copies
    # it with sqlite3's own VACUUM INTO, which REFUSES a file that merely
    # starts with the SQLite magic bytes. It has to be a real database.
    import sqlite3 as _sqlite3
    with contextlib.closing(_sqlite3.connect(str(seed / "refresh_tokens.db"))) as c:
        c.execute("CREATE TABLE t (a)")
        c.commit()
    (seed / "session_metadata.json").write_text(
        json.dumps({"id": "sess-from-old", "owned_tmux_sessions": ["cloude_a"]})
    )

    script = (
        f"source '{COMMON_SH}'; "
        f"take_backup '{install}' '{tmp_path}/bk' >/dev/null 2>&1; "
        f"rm -f '{seed}/session_metadata.json' '{seed}/refresh_tokens.db'; "
        f"restore_backup '{install}' '{tmp_path}/bk' >/dev/null 2>&1"
    )
    # resolve_state_dir() prefers the INHERITED ``CLOUDE_STATE_DIR`` env
    # var over the .env file, and tests/conftest.py sets that var for the
    # whole suite. Left in place, restore_backup would place the files in
    # the conftest temp dir and this test would report a relocation that
    # its own environment caused. Dropped explicitly.
    env = {k: v for k, v in os.environ.items() if k != "CLOUDE_STATE_DIR"}
    proc = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, env=env
    )
    manifest = tmp_path / "bk" / ".manifest"
    backed_up_meta = (
        manifest.exists()
        and "BACKED_UP\tstate\tsession_metadata.json" in manifest.read_text()
    )
    return install, logs, state, proc, backed_up_meta


def test_rollback_restores_a_state_file_to_where_take_backup_found_it(tmp_path: Path):
    """The rollback tool must not be the step that breaks the rollback.

    ``take_backup()`` already locates each state file individually via
    ``resolve_state_file`` and says so when it finds one at the old
    ``LOG_DIRECTORY``. ``restore_backup()`` used to throw that away and
    place EVERY state file at ``resolve_state_dir()``, so running
    scripts/rollback.sh on a pre-state-directory install moved the very
    file the older code reads. Measured by running the two real bash
    functions and reading the DISK, not their exit codes.
    """
    if not COMMON_SH.exists():
        pytest.skip(f"CANNOT DETERMINE: {COMMON_SH} not present in this tree")

    _install, logs, state, proc, backed_up_meta = _run_rollback_cycle(tmp_path, "logs")
    if not backed_up_meta:
        pytest.skip(
            "CANNOT DETERMINE: take_backup did not back up "
            f"session_metadata.json (rc={proc.returncode}, stderr="
            f"{proc.stderr[-300:]!r}) - the rollback path was not exercised"
        )

    assert (logs / "session_metadata.json").exists(), (
        "restore did not put the file back where take_backup found it"
    )
    assert not (state / "session_metadata.json").exists(), (
        "restore RELOCATED the file into the state dir - the defect this "
        "test exists to catch"
    )
    assert downgrade_verdict(logs, "sess-from-old") == INTACT


def test_rollback_restores_a_state_dir_file_to_the_state_dir(tmp_path: Path):
    """The other origin, so the fix cannot be 'always use LOG_DIRECTORY'.

    A file that take_backup found in the CURRENT state directory must go
    back there. Without this, a one-line inversion of the bug would pass
    the test above and break every modern install.
    """
    if not COMMON_SH.exists():
        pytest.skip(f"CANNOT DETERMINE: {COMMON_SH} not present in this tree")

    _install, logs, state, proc, backed_up_meta = _run_rollback_cycle(tmp_path, "state")
    if not backed_up_meta:
        pytest.skip(
            "CANNOT DETERMINE: take_backup did not back up "
            f"session_metadata.json (rc={proc.returncode}, stderr="
            f"{proc.stderr[-300:]!r}) - the rollback path was not exercised"
        )

    assert (state / "session_metadata.json").exists(), (
        "restore did not put the file back in the state dir it came from"
    )
    assert not (logs / "session_metadata.json").exists(), (
        "restore misfiled a state-dir file into the old LOG_DIRECTORY"
    )
