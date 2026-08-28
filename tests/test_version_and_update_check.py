"""Tests for the release-tag version resolver and the update self check.

The behaviour that matters most here is the THIRD OUTCOME: when the checker
cannot look at the remote it must say so, and must never report "current".
Every test that exercises a failure asserts the status is ``unknown`` and
that a reason was recorded.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.core import update_check
from src.core.update_check import (
    STATUS_CURRENT,
    STATUS_UNKNOWN,
    STATUS_UPDATE_AVAILABLE,
    UpdateCheckError,
    UpdateChecker,
    parse_version,
    read_configured_remote,
)
from src.core.version import (
    VERSION_FILE_HEADER,
    describe_git_tag,
    is_git_root,
    normalize_tag,
    read_version_file,
    resolve_version,
    write_version_file,
)


# --- version resolution ---------------------------------------------------


def test_normalize_tag_strips_v_and_whitespace() -> None:
    assert normalize_tag(" v1.2.3 ") == "1.2.3"
    assert normalize_tag("1.2.3") == "1.2.3"
    assert normalize_tag("") == ""


def test_version_file_round_trip(tmp_path: Path) -> None:
    write_version_file("v2.3.4", root=tmp_path)
    text = (tmp_path / "VERSION").read_text(encoding="utf-8")
    assert text.startswith(VERSION_FILE_HEADER)
    assert read_version_file(tmp_path) == "2.3.4"


def test_version_file_missing_returns_empty(tmp_path: Path) -> None:
    assert read_version_file(tmp_path) == ""


def test_env_var_wins_over_version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_version_file("1.0.0", root=tmp_path)
    monkeypatch.setenv("CLOUDE_APP_VERSION", "v9.9.9")
    assert resolve_version(tmp_path) == "9.9.9"


def test_version_file_used_when_env_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.0.0", root=tmp_path)
    assert resolve_version(tmp_path) == "1.0.0"


def test_version_file_outranks_legacy_package_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated stamp beats macOS/package.json, which is the LEGACY source.

    They disagree exactly when it matters: package.json is hand-bumped in the
    repo, the stamp is written from the tag at release time. If the legacy
    literal won, a release built from a tag would still report whatever number
    someone last typed into package.json.
    """
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    (tmp_path / "macOS").mkdir()
    (tmp_path / "macOS" / "package.json").write_text(
        json.dumps({"version": "0.0.1"}), encoding="utf-8"
    )
    write_version_file("0.8.1", root=tmp_path)
    assert resolve_version(tmp_path) == "0.8.1"


def test_package_json_is_still_the_fallback_without_a_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no stamp and no git, the legacy literal is better than blank."""
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    (tmp_path / "macOS").mkdir()
    (tmp_path / "macOS" / "package.json").write_text(
        json.dumps({"version": "0.0.1"}), encoding="utf-8"
    )
    assert resolve_version(tmp_path) == "0.0.1"


def test_resolve_returns_empty_when_nothing_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty directory resolves to "" so callers render nothing.

    Blank is the correct answer; a wrong literal is not.
    """
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    assert resolve_version(tmp_path) == ""


# --- the production layout ------------------------------------------------
#
# THIS IS THE CASE THAT ACTUALLY SHIPS. The Electron app copies src/ + client/
# into ~/Library/Application Support/cloude-code-menubar/server/. There is no
# .git there and no macOS/package.json, so three of the five sources in the
# resolution order are absent BY CONSTRUCTION. Only the env var and the
# VERSION file can answer, and a resolver that quietly works in the repo and
# returns "" in production is the easiest possible thing to ship by accident.


def _production_layout(tmp_path: Path) -> Path:
    """Build a serverDir shaped like the one bootstrap.js provisions.

    Args:
        tmp_path: pytest temp directory.

    Returns:
        The serverDir path: src/ + client/ + config.json, no .git, no macOS/.
    """
    server_dir = tmp_path / "server"
    (server_dir / "src" / "core").mkdir(parents=True)
    (server_dir / "client").mkdir()
    (server_dir / "config.json").write_text("{}", encoding="utf-8")
    return server_dir


def test_production_layout_resolves_from_the_injected_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server-manager.js injects CLOUDE_APP_VERSION at spawn. That path wins."""
    server_dir = _production_layout(tmp_path)
    monkeypatch.setenv("CLOUDE_APP_VERSION", "v0.8.1")
    assert resolve_version(server_dir) == "0.8.1"


def test_production_layout_resolves_from_the_stamped_version_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no env var and no .git, the VERSION stamp is the only answer."""
    server_dir = _production_layout(tmp_path)
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    assert not (server_dir / ".git").exists()
    assert not (server_dir / "macOS" / "package.json").exists()
    write_version_file("0.8.1", root=server_dir)
    assert resolve_version(server_dir) == "0.8.1"


def test_a_source_root_inside_a_foreign_checkout_does_not_borrow_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION. `git -C <dir>` walks UPWARDS until it finds a repository.

    Caught for real: a production-shaped copy dropped inside an unrelated
    checkout resolved to that checkout's `git describe` output. A version
    belonging to a different project is worse than no version at all, because
    it looks like an answer. The resolver must return "" here.
    """
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)

    outer = tmp_path / "someone-elses-repo"
    outer.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "x"],
        ["git", "tag", "v42.0.0"],
    ):
        subprocess.run(cmd, cwd=outer, check=True, capture_output=True)

    # Sanity: the foreign repo really does describe as v42.0.0.
    assert resolve_version(outer) == "42.0.0"

    nested = outer / "server"
    (nested / "src" / "core").mkdir(parents=True)
    assert not (nested / ".git").exists()
    assert resolve_version(nested) == ""
    assert not is_git_root(nested)
    # And the self check must not query the stranger's remote either.
    assert update_check.discover_origin_remote(nested) == ""


def test_describe_never_degrades_to_a_bare_commit_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION. `--always` turns an untagged repo into a hex sha.

    A sha is not a version, and it would have been stamped into the header
    chip as though it were one. An untagged checkout resolves to "".
    """
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    repo = tmp_path / "untagged"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "x"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    assert is_git_root(repo)
    assert describe_git_tag(repo) == ""
    assert resolve_version(repo) == ""


def test_production_layout_with_nothing_is_blank_not_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var, no stamp: blank, and NEVER the enclosing repo's version.

    The temp dir is not a git work tree, so `git describe` must fail rather
    than describing some ancestor checkout the user happens to be inside.
    """
    server_dir = _production_layout(tmp_path)
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    assert resolve_version(server_dir) == ""


def test_production_layout_unresolved_version_is_unknown_not_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self check must say "could not check", not "up to date"."""
    server_dir = _production_layout(tmp_path)
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: ["9.9.9"])
    checker = UpdateChecker(
        config_path=server_dir / "config.json",
        root=server_dir,
        cache_path=server_dir / "cache.json",
    )
    status = checker.refresh()
    assert status.status == STATUS_UNKNOWN
    assert status.status != STATUS_CURRENT
    assert status.reason


def test_cache_defaults_inside_the_source_root_not_a_new_state_dir(
    tmp_path: Path,
) -> None:
    """No fourth state root. The cache lives beside config.json.

    A private ~/.cloude-code/ would be a second place the app keeps state
    about itself, which is how two installs start disagreeing.
    """
    server_dir = _production_layout(tmp_path)
    checker = UpdateChecker(
        config_path=server_dir / "config.json", root=server_dir
    )
    checker.refresh()
    assert (server_dir / update_check.CACHE_FILENAME).is_file()
    # Asserted against the module rather than the filesystem: a real
    # ~/.cloude-code left over from something else must not make this pass
    # or fail for the wrong reason.
    assert update_check.default_cache_path(server_dir).parent == server_dir
    assert ".cloude-code" not in Path(update_check.CACHE_FILENAME).parts


# --- tag parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("v0.10.0", (0, 10, 0)),
        ("v1.2.3-rc1", None),
        ("0.8.1-3-gabc1234", None),
        ("nightly", None),
        ("", None),
    ],
)
def test_parse_version(tag: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(tag) == expected


def test_read_configured_remote(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"updates": {"remote": "https://example.test/x.git"}}))
    assert read_configured_remote(config) == "https://example.test/x.git"


def test_read_configured_remote_absent_or_malformed(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert read_configured_remote(missing) == ""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_configured_remote(bad) == ""
    no_block = tmp_path / "plain.json"
    no_block.write_text(json.dumps({"projects": []}))
    assert read_configured_remote(no_block) == ""


# --- the three outcomes ---------------------------------------------------


def _checker(tmp_path: Path) -> UpdateChecker:
    """Build a checker wired entirely at temp paths.

    Args:
        tmp_path: pytest temp directory.

    Returns:
        An UpdateChecker whose config, repo root and cache all live under
        tmp_path so no test touches the real install.
    """
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"updates": {"remote": "https://example.test/x.git"}}))
    return UpdateChecker(
        config_path=config,
        root=tmp_path,
        cache_path=tmp_path / "cache.json",
    )


def test_outcome_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.2.3", root=tmp_path)
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: ["1.2.2", "1.2.3"])
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_CURRENT
    assert status.latest_version == "1.2.3"
    assert status.reason == ""


def test_outcome_update_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.2.3", root=tmp_path)
    monkeypatch.setattr(
        update_check, "fetch_remote_tags", lambda remote: ["1.2.3", "1.10.0", "1.9.0"]
    )
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_UPDATE_AVAILABLE
    # 1.10.0 beats 1.9.0: the sort is numeric, not lexical.
    assert status.latest_version == "1.10.0"
    assert status.upgrade_command


def test_outcome_unknown_when_remote_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable remote is UNKNOWN, never CURRENT. This is the whole point."""
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.2.3", root=tmp_path)

    def boom(remote: str) -> list[str]:
        raise UpdateCheckError("could not reach the release remote: offline")

    monkeypatch.setattr(update_check, "fetch_remote_tags", boom)
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_UNKNOWN
    assert status.status != STATUS_CURRENT
    assert "offline" in status.reason
    assert status.latest_version == ""


def test_outcome_unknown_when_version_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: ["9.9.9"])
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_UNKNOWN
    assert status.reason


def test_outcome_unknown_when_not_on_a_release_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOUDE_APP_VERSION", "1.2.3-4-gdeadbee")
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: ["1.2.3"])
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_UNKNOWN
    assert "release tag" in status.reason


def test_outcome_unknown_when_remote_has_no_release_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.2.3", root=tmp_path)
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: [])
    status = _checker(tmp_path).refresh()
    assert status.status == STATUS_UNKNOWN


def test_status_is_cached_and_reloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDE_APP_VERSION", raising=False)
    write_version_file("1.2.3", root=tmp_path)
    monkeypatch.setattr(update_check, "fetch_remote_tags", lambda remote: ["2.0.0"])
    first = _checker(tmp_path)
    first.refresh()
    # A brand new checker over the same cache path sees the stored answer,
    # with its timestamp, without touching the network.
    reloaded = _checker(tmp_path).status()
    assert reloaded.status == STATUS_UPDATE_AVAILABLE
    assert reloaded.checked_at > 0


def test_malformed_cache_is_not_a_passing_check(tmp_path: Path) -> None:
    """A corrupt cache must degrade to "no check has run yet", not to current."""
    (tmp_path / "cache.json").write_text("{ garbage")
    assert _checker(tmp_path).status().status == STATUS_UNKNOWN


def test_configured_remote_overrides_origin(tmp_path: Path) -> None:
    assert _checker(tmp_path).resolve_remote() == "https://example.test/x.git"
