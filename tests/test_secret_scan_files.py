"""Tests for the file scanner, the staged-diff scan and the hook itself.

THE END-TO-END TESTS RUN IN A THROWAWAY REPOSITORY. They create a git
repo under tmp_path, install the real hook with the real installer, and
run real commits through it. Nothing here touches this repository's own
git state.

THE CENTRAL ASSERTION IS AN ABSENCE, SO IT NEEDS A POSITIVE CONTROL.
"the secret was not printed" passes trivially against a hook that printed
nothing at all, so every such test also asserts that the detector NAME
and the file PATH did appear. That pins the output to "it reported the
finding and withheld the value" rather than "it said nothing".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core.secret_scan import (
    FILE_DETECTORS,
    MASK,
    FileFinding,
    read_text_or_none,
    scan_content,
    should_skip,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scripts" / "scan_secrets.py"
INSTALLER = REPO_ROOT / "scripts" / "install-secret-hook.sh"
UNINSTALLER = REPO_ROOT / "scripts" / "uninstall-secret-hook.sh"

FAKE_GITHUB = "ghp_R7kQ2mVx9TbnLcJ6wZaHy8FdSpU3gEoNrKq1"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_CLOUDFLARE = "v1Qk7XmZr9TdNbLcJ6wZaHy8FdSpU3gEoNrKq"  # secret-scan: allow synthetic fixture, not an issued credential
SAFE_LINES = (
    "GITHUB_TOKEN=op://Claude/GitHub/personal_access_token\n"
    "CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN}\n"
    "GOOGLE_API_KEY=AIzaSyYOUR_GOOGLE_API_KEY_HERE_00000000\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in a repo and return the completed process.

    Inputs: repo (Path), args (str) - the git subcommand and flags.
    Output: subprocess.CompletedProcess with text output.
    Example: _git(tmp, "init").returncode -> 0
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture()
def throwaway_repo(tmp_path: Path) -> Path:
    """A real git repository with our scripts reachable, for hook tests.

    Description: ``scripts`` is symlinked rather than copied so the hook
      under test is the file in this repository, not a stale duplicate of
      it. scan_secrets.py resolves its own path, so the symlink still
      lands its imports on the real source tree.
    Inputs: tmp_path (pytest fixture).
    Output: Path to the repository root.
    Example: throwaway_repo / ".git" -> exists
    """
    repo = tmp_path / "throwaway"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "scan@example.invalid")
    _git(repo, "config", "user.name", "scan test")
    (repo / "scripts").symlink_to(REPO_ROOT / "scripts")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _commit(repo: Path, message: str) -> subprocess.CompletedProcess:
    """Attempt a commit and return the process, hook and all.

    Inputs: repo (Path), message (str).
    Output: subprocess.CompletedProcess.
    Example: _commit(repo, "x").returncode -> 0
    """
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        capture_output=True, text=True, check=False, env=env,
    )


# --------------------------------------------------------------------
# The file scanner
# --------------------------------------------------------------------

def test_a_finding_reports_the_line_and_column_of_the_match():
    text = f"alpha\nbeta\nGITHUB_TOKEN={FAKE_GITHUB}\n"
    found, _ = scan_content("a.env", text)
    assert len(found) == 1
    assert (found[0].line, found[0].column) == (3, len("GITHUB_TOKEN=") + 1)


def test_the_excerpt_masks_the_value_and_still_names_the_detector():
    text = f"GITHUB_TOKEN={FAKE_GITHUB}\n"
    found = scan_content("a.env", text)[0][0]
    assert MASK in found.excerpt
    assert FAKE_GITHUB not in found.excerpt
    assert "GITHUB_TOKEN" in found.excerpt, "positive control: context survives"


def test_two_credentials_on_one_line_are_both_masked():
    """Masking only the target would print the other one in its excerpt."""
    text = f"a={FAKE_GITHUB} b=CLOUDFLARE_TOKEN={FAKE_CLOUDFLARE}\n"
    found, _ = scan_content("a.env", text)
    assert len(found) == 2
    for finding in found:
        assert FAKE_GITHUB not in finding.excerpt
        assert FAKE_CLOUDFLARE not in finding.excerpt


def test_no_field_of_a_file_finding_carries_the_value():
    found = scan_content("a.env", f"GITHUB_TOKEN={FAKE_GITHUB}\n")[0][0]
    for value in vars(found).values():
        assert FAKE_GITHUB not in str(value)
    assert FAKE_GITHUB not in found.render()
    assert "github_token" in found.render(), "positive control"


def test_excerpts_can_be_turned_off_entirely():
    found, _ = scan_content(
        "a.env", f"GITHUB_TOKEN={FAKE_GITHUB}\n", excerpts=False
    )
    assert found[0].excerpt is None
    assert FAKE_GITHUB not in found[0].render()


def test_the_safe_lines_produce_nothing():
    assert scan_content("a.env", SAFE_LINES) == ([], 0)


# --------------------------------------------------------------------
# The inline pragma
# --------------------------------------------------------------------

def test_the_pragma_suppresses_a_finding_and_the_count_says_so():
    """A suppression that is not counted is a path allowlist with extra
    steps. The count is what keeps it visible."""
    line = f'KEY = "{FAKE_GITHUB}"  # secret-scan: allow synthetic fixture\n'
    found, suppressed = scan_content("a.py", line)
    assert found == []
    assert suppressed == 1


def test_the_pragma_needs_a_written_reason():
    """A bare 'secret-scan: allow' suppresses nothing. If it is worth
    silencing, it is worth one sentence saying why."""
    line = f'KEY = "{FAKE_GITHUB}"  # secret-scan: allow\n'
    found, suppressed = scan_content("a.py", line)
    assert len(found) == 1
    assert suppressed == 0


def test_the_pragma_is_per_line_not_per_file():
    """The whole reason it is a pragma and not a path allowlist."""
    text = (
        f'A = "{FAKE_GITHUB}"  # secret-scan: allow fixture\n'
        f'CLOUDFLARE_API_TOKEN = "{FAKE_CLOUDFLARE}"\n'
    )
    found, suppressed = scan_content("a.py", text)
    assert [f.line for f in found] == [2]
    assert suppressed == 1


def test_the_pragma_can_be_disabled_entirely():
    line = f'KEY = "{FAKE_GITHUB}"  # secret-scan: allow fixture\n'
    found, suppressed = scan_content("a.py", line, honour_pragma=False)
    assert len(found) == 1
    assert suppressed == 0


def test_the_pragma_does_not_reach_the_transcript_detector():
    """message_model_secrets knows nothing about pragmas, deliberately: a
    transcript body quoting the comment must not be able to suppress its
    own flagging."""
    from src.core.message_model_secrets import scan_text as raw_scan
    assert raw_scan(f'KEY = "{FAKE_GITHUB}"  # secret-scan: allow fixture')


def test_should_skip_rejects_binary_suffixes_and_vendored_paths():
    assert should_skip(Path("client/vendor/xterm/xterm.js"))
    assert should_skip(Path("logo.png"))
    assert not should_skip(Path("src/config.py")), "positive control"


def test_reading_a_binary_file_returns_none_rather_than_garbage(tmp_path):
    binary = tmp_path / "b.dat"
    binary.write_bytes(b"\x00\x01\x02")
    assert read_text_or_none(binary) is None
    text = tmp_path / "t.txt"
    text.write_text("ok")
    assert read_text_or_none(text) == "ok", "positive control"


def test_the_file_detector_set_is_the_vendor_set():
    assert "high_entropy_assignment" not in FILE_DETECTORS


def test_render_is_stable_for_a_hand_built_finding():
    finding = FileFinding("a.py", 2, 3, "github_token", 40, "abcdef123456", None)
    assert finding.render() == "a.py:2:3 github_token (40 chars, sha256 abcdef123456)"


# --------------------------------------------------------------------
# The staged scan, through real git
# --------------------------------------------------------------------

def test_the_staged_scan_reports_the_real_line_in_the_new_file(throwaway_repo):
    """The diff is read with -U0, so line numbers have to come from the
    hunk header rather than from position within the diff."""
    (throwaway_repo / "conf.env").write_text(
        f"one\ntwo\nthree\nGITHUB_TOKEN={FAKE_GITHUB}\n"
    )
    _git(throwaway_repo, "add", "conf.env")
    done = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", "--repo", str(throwaway_repo),
         "--json"],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 1
    assert '"line": 4' in done.stdout
    assert FAKE_GITHUB not in done.stdout + done.stderr


@pytest.mark.parametrize("cwd_is_repo", [True, False])
def test_the_staged_scan_works_from_any_working_directory(
    throwaway_repo, tmp_path, cwd_is_repo,
):
    """Measured bug: should_skip() stat'ed the repository-relative path
    git reports, which does not resolve from an unrelated directory, so
    the scan silently skipped every staged file and reported clean. The
    parametrisation is the point - the passing half was hiding it."""
    (throwaway_repo / "conf.env").write_text(f"GITHUB_TOKEN={FAKE_GITHUB}\n")
    _git(throwaway_repo, "add", "conf.env")
    done = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", "--repo", str(throwaway_repo)],
        cwd=throwaway_repo if cwd_is_repo else tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "conf.env:1" in done.stderr


def test_a_secret_already_committed_does_not_block_a_later_commit(
    throwaway_repo,
):
    """The hook scans what the commit ADDS. Blocking on a line the commit
    is not introducing is how a hook gets uninstalled."""
    target = throwaway_repo / "conf.env"
    target.write_text(f"GITHUB_TOKEN={FAKE_GITHUB}\n")
    _git(throwaway_repo, "add", "conf.env")
    _git(throwaway_repo, "commit", "-q", "-m", "pre-existing")

    target.write_text(f"GITHUB_TOKEN={FAKE_GITHUB}\nunrelated = 1\n")
    _git(throwaway_repo, "add", "conf.env")
    done = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", "--repo", str(throwaway_repo)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr


def test_the_staged_scan_cannot_determine_outside_a_repository(tmp_path):
    """Exit 2 is not exit 0. A scanner that passes because it never ran is
    the false green this whole layer exists to avoid."""
    done = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", "--repo", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2
    assert "CANNOT DETERMINE" in done.stderr


def test_auditing_a_missing_path_cannot_determine(tmp_path):
    done = subprocess.run(
        [sys.executable, str(SCANNER), str(tmp_path / "nope")],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2


def test_auditing_a_clean_directory_exits_zero_and_says_what_it_scanned(
    tmp_path,
):
    (tmp_path / "a.py").write_text(SAFE_LINES)
    done = subprocess.run(
        [sys.executable, str(SCANNER), str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0
    assert "1 files scanned" in done.stdout


# --------------------------------------------------------------------
# The hook, installed and running for real
# --------------------------------------------------------------------

def test_the_installer_installs_and_the_hook_refuses_a_real_secret(
    throwaway_repo,
):
    installed = subprocess.run(
        ["sh", str(INSTALLER)], cwd=throwaway_repo,
        capture_output=True, text=True, check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert (throwaway_repo / ".git" / "hooks" / "pre-commit").exists()

    (throwaway_repo / "deploy.env").write_text(
        f"# deploy config\nCLOUDFLARE_API_TOKEN={FAKE_CLOUDFLARE}\n"
    )
    _git(throwaway_repo, "add", "deploy.env")
    done = _commit(throwaway_repo, "add deploy config")

    output = done.stdout + done.stderr
    assert done.returncode != 0, "the commit must be refused"
    assert FAKE_CLOUDFLARE not in output, "the value must never be printed"
    assert "cloudflare_api_token" in output, "positive control: detector named"
    assert "deploy.env:2" in output, "positive control: file and line named"
    assert _git(throwaway_repo, "log", "--oneline").stdout.count("\n") == 1


def test_the_installed_hook_allows_references_and_placeholders(
    throwaway_repo,
):
    subprocess.run(["sh", str(INSTALLER)], cwd=throwaway_repo, check=True,
                   capture_output=True)
    (throwaway_repo / "example.env").write_text(SAFE_LINES)
    _git(throwaway_repo, "add", "example.env")
    done = _commit(throwaway_repo, "add example config")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _git(throwaway_repo, "log", "--oneline").stdout.count("\n") == 2


def test_the_uninstaller_removes_the_hook_and_commits_flow_again(
    throwaway_repo,
):
    subprocess.run(["sh", str(INSTALLER)], cwd=throwaway_repo, check=True,
                   capture_output=True)
    removed = subprocess.run(
        ["sh", str(UNINSTALLER)], cwd=throwaway_repo,
        capture_output=True, text=True, check=False,
    )
    assert removed.returncode == 0, removed.stderr
    assert not (throwaway_repo / ".git" / "hooks" / "pre-commit").exists()

    (throwaway_repo / "deploy.env").write_text(
        f"CLOUDFLARE_API_TOKEN={FAKE_CLOUDFLARE}\n"
    )
    _git(throwaway_repo, "add", "deploy.env")
    assert _commit(throwaway_repo, "now permitted").returncode == 0


def test_the_installer_refuses_to_clobber_a_foreign_hook(throwaway_repo):
    hooks = throwaway_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho someone elses hook\n")
    (hooks / "pre-commit").chmod(0o755)
    done = subprocess.run(
        ["sh", str(INSTALLER)], cwd=throwaway_repo,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 1
    assert "REFUSING" in done.stderr
    assert "someone elses hook" in (hooks / "pre-commit").read_text()


def test_force_backs_up_the_foreign_hook_and_the_uninstaller_restores_it(
    throwaway_repo,
):
    hooks = throwaway_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    original = "#!/bin/sh\necho someone elses hook\n"
    (hooks / "pre-commit").write_text(original)
    subprocess.run(["sh", str(INSTALLER), "--force"], cwd=throwaway_repo,
                   check=True, capture_output=True)
    assert "cloudecode-secret-scan" in (hooks / "pre-commit").read_text()
    subprocess.run(["sh", str(UNINSTALLER)], cwd=throwaway_repo, check=True,
                   capture_output=True)
    assert (hooks / "pre-commit").read_text() == original


def test_the_uninstaller_refuses_to_delete_a_foreign_hook(throwaway_repo):
    hooks = throwaway_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    done = subprocess.run(
        ["sh", str(UNINSTALLER)], cwd=throwaway_repo,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 1
    assert (hooks / "pre-commit").exists()


def test_the_hook_refuses_rather_than_passing_when_the_scanner_is_missing(
    tmp_path,
):
    """A hook that waves a commit through because it could not run is
    worse than no hook: it leaves a record of having checked."""
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    hook = REPO_ROOT / "scripts" / "hooks" / "pre-commit-secret-scan.sh"
    done = subprocess.run(
        ["sh", str(hook)], cwd=repo, capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2
    assert "refusing" in done.stderr
