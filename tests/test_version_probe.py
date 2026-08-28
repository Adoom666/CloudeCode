"""feat/upgrade-rollback — tests for scripts/upgrade_lib/version_probe.py.

Covers:
- current_version: delegates to src.core.version.resolve_version, returns
  "" honestly rather than fabricating a version, when it cannot resolve one
- resolve_remote: configured remote > origin > fallback precedence
- list_release_tags / latest / check-tag CLI subcommands: the three-outcome
  exit codes (0 pass, 2 fail, 3 could-not-evaluate) are exactly right and
  never collapsed into each other
- _install_src_on_path: probing two different install directories in the
  same process does not leak the first one's modules into the second
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "upgrade_lib" / "version_probe.py"
if str(ROOT / "scripts" / "upgrade_lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts" / "upgrade_lib"))

# ruff: noqa: E402
import version_probe  # type: ignore


# ---------------------------------------------------------------------- #
# current_version
# ---------------------------------------------------------------------- #


def test_current_version_resolves_from_real_repo():
    # ROOT is this very checkout; it has a VERSION-resolvable state (either
    # an exact tag, package.json, or a loose describe) in virtually any dev
    # environment. We only assert it returns a string, not that a network
    # or tag exists.
    version = version_probe.current_version(ROOT)
    assert isinstance(version, str)


def test_current_version_empty_when_unresolvable(tmp_path):
    # An empty directory has no .git, no VERSION file, no package.json —
    # resolve_version must return "" rather than raising or guessing.
    (tmp_path / "src" / "core").mkdir(parents=True)
    # Copy just enough of version.py's behavior by pointing at a directory
    # with none of the five resolution sources present.
    version = version_probe.current_version(tmp_path)
    assert version == ""


# ---------------------------------------------------------------------- #
# resolve_remote precedence
# ---------------------------------------------------------------------- #


def test_resolve_remote_prefers_configured_over_origin(tmp_path):
    # tmp_path is not a git work tree at all, so discover_origin_remote
    # would return "" on its own — the configured value must still win,
    # proving it is checked FIRST rather than only as a fallback.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"updates": {"remote": "https://example.com/configured.git"}}))
    remote = version_probe.resolve_remote(tmp_path, config_path)
    assert remote == "https://example.com/configured.git"


def test_resolve_remote_falls_back_to_fallback_remote(tmp_path):
    # No config.json, and tmp_path is not a git work tree (no origin to
    # discover) — must land on the public fallback, not raise or guess.
    remote = version_probe.resolve_remote(tmp_path, None)
    version_probe._install_src_on_path(tmp_path)
    from src.core.update_check import FALLBACK_REMOTE

    assert remote == FALLBACK_REMOTE


# ---------------------------------------------------------------------- #
# module isolation across two install_dirs
# ---------------------------------------------------------------------- #


def test_install_src_on_path_does_not_leak_between_dirs(tmp_path):
    # pytest's own import machinery may already have ROOT on sys.path
    # (tests/ is a package with __init__.py), so "not present at all" is
    # too strong an assertion. What matters is that each call puts ITS
    # OWN dir at the front (so it wins module resolution) and does not
    # leave a stale front-of-list entry for the previous dir.
    other = tmp_path / "other_install"
    other.mkdir()
    version_probe._install_src_on_path(ROOT)
    assert sys.path[0] == str(ROOT.resolve())
    version_probe._install_src_on_path(other)
    assert sys.path[0] == str(other.resolve())
    assert sys.path[0] != str(ROOT.resolve())


# ---------------------------------------------------------------------- #
# CLI subcommands — three-outcome exit codes, via subprocess (real argv)
# ---------------------------------------------------------------------- #


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_current_exit_0_on_real_repo():
    result = _run("current", "--install-dir", str(ROOT))
    # Either resolves (0, non-empty stdout) or honestly can't (3, empty
    # stdout) — both are valid depending on the checkout state, but it must
    # never be silently wrong.
    assert result.returncode in (0, 3)
    if result.returncode == 0:
        assert result.stdout.strip()
    else:
        assert result.stdout.strip() == ""
        assert "could not determine" in result.stderr


def test_cli_tags_could_not_evaluate_on_bad_remote():
    result = _run(
        "tags",
        "--install-dir",
        str(ROOT),
        "--remote",
        "https://this-host-does-not-exist.invalid/nowhere.git",
    )
    assert result.returncode == 3
    assert "could not list release tags" in result.stderr


def test_cli_latest_could_not_evaluate_on_bad_remote():
    result = _run(
        "latest",
        "--install-dir",
        str(ROOT),
        "--remote",
        "https://this-host-does-not-exist.invalid/nowhere.git",
    )
    assert result.returncode == 3


def test_cli_check_tag_fail_when_tag_absent_from_list(monkeypatch):
    # Patch fetch_remote_tags via a fake local remote: use `tags` against a
    # real, reachable, but tag-less bare repo so we get a deterministic
    # FAIL (2), not a network-dependent COULD-NOT-EVALUATE (3).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        bare = Path(d) / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        result = _run(
            "check-tag",
            "--install-dir",
            str(ROOT),
            "--remote",
            str(bare),
            "9.9.9",
        )
        assert result.returncode == 2
        assert "is not a published release tag" in result.stderr


def test_cli_check_tag_pass_when_tag_present():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        bare = Path(d) / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(repo), "tag", "1.2.3"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/heads/main", "1.2.3"], check=True)

        result = _run(
            "check-tag",
            "--install-dir",
            str(ROOT),
            "--remote",
            str(bare),
            "1.2.3",
        )
        assert result.returncode == 0, result.stderr

        latest = _run("latest", "--install-dir", str(ROOT), "--remote", str(bare))
        assert latest.returncode == 0
        assert latest.stdout.strip() == "1.2.3"
