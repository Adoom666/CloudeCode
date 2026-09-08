"""The local secret gate and the CI secret gate must not disagree.

WHY THIS FILE EXISTS. On 2026-08-31 a commit passed the local pre-commit
hook and would have failed CI. The hook ran scripts/scan_secrets.py; CI ran
gitleaks. Only one of them ran before the commit, so the local gate
green-lit work the remote gate rejected - which is worse than having no
local gate, because it produces a record of having checked.

These tests pin the three invariants that failure depended on:

  1. .gitleaks.toml actually LOADS. gitleaks 8.30.1 refuses a config that
     mixes the legacy [allowlist] table with the [[allowlists]] array, and
     that refusal is an exit 1 that looks exactly like "leaks found".
  2. The allowlist is scoped BY VALUE, never by path. A `paths` entry
     suppresses the whole FILE, so a path-scoped exemption for a test
     fixture would silently blind the scanner to a real credential pasted
     into that same file.
  3. The hook runs BOTH scanners, with the same config CI uses.

Nothing here contains credential material. The positive control builds its
sample at runtime from `secrets`, so this file has no high-entropy literal
of its own for either scanner to trip on.
"""

from __future__ import annotations

import re
import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"
HOOK = REPO_ROOT / "scripts" / "hooks" / "pre-commit-secret-scan.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "secret-scan.yml"

#: The expression the hook uses to read the pinned version out of the
#: workflow. Duplicated here on purpose: if someone reformats that line,
#: this test fails rather than the hook silently losing parity checking.
_PIN_RE = re.compile(r"^\s*GITLEAKS_VERSION:\s*([0-9][0-9.]*)", re.M)

_HAS_GITLEAKS = shutil.which("gitleaks") is not None
_needs_gitleaks = pytest.mark.skipif(
    not _HAS_GITLEAKS, reason="gitleaks is not installed on this machine"
)


def _allowlist_regexes() -> list[str]:
    """Every literal in the SYNTHETIC-FIXTURE allowlist block of .gitleaks.toml.

    Description: reads the triple-quoted entries out of the fixture
      block's own ``regexes = [...]``, located by its marker rather than
      by "the last regexes block in the file". A later allowlist block can
      legitimately exist for a different reason (see the
      key_order_digest block, added 2026-08-31, which matches a derived
      hash pattern rather than a literal fixture value) - if this helper
      kept assuming "last block = the fixture registry" it would silently
      start reading that other block's entries as if they were dead
      literals from tests/*.py, which is a different invariant checked
      separately (test_jsonl_manifest_allowlist_pattern_matches_live_data
      below).
    Inputs: none (reads the repo config).
    Output: list of str literals.
    Example: _allowlist_regexes() -> ["AKIA...", ...]
    """
    text = CONFIG.read_text()
    marker = "Synthetic credential fixtures in the secret-scanner test suite"
    assert marker in text, "fixture allowlist block is missing"
    tail = text[text.index(marker):]
    next_block = re.search(r"\n\[\[allowlists\]\]", tail)
    block_text = tail[:next_block.start()] if next_block else tail
    blocks = re.findall(r"regexes = \[(.*?)\n\]", block_text, re.S)
    assert blocks, "no regexes block found in the fixture allowlist"
    return re.findall(r"'''(.*?)'''", blocks[-1], re.S)


def test_config_uses_only_the_array_allowlist_form() -> None:
    """A mixed [allowlist] / [[allowlists]] config refuses to load at all."""
    text = CONFIG.read_text()
    legacy = re.search(r"^\[allowlist\]\s*$", text, re.M)
    array = re.search(r"^\[\[allowlists\]\]\s*$", text, re.M)
    assert array, "expected at least one [[allowlists]] block"
    assert legacy is None, (
        "`[allowlist]` (the legacy table) cannot coexist with `[[allowlists]]`. "
        "gitleaks 8.30.1 fails to load the config entirely, and that exits 1, "
        "which is indistinguishable from 'leaks found' unless you read stderr."
    )


def test_fixture_allowlist_is_scoped_by_value_not_by_path() -> None:
    """No allowlist block from the fixture block onward carries a `paths` KEY.

    A `paths` entry suppresses every finding in the whole file, not just the
    listed values, so it would blind the scanner to a REAL credential pasted
    into one of the fixture files. That is the exact accident the scanner
    exists to catch.

    Checks for the actual TOML key assignment (``paths =`` at the start of a
    line), not the bare English word "paths" anywhere in the text. The
    looser substring check used to false-fail on ordinary prose: the
    key_order_digest allowlist block (added 2026-08-31) documents, in a
    comment, why a `paths` exemption was rejected for that block - the word
    "paths" appears there deliberately, as an explanation, with no `paths =`
    key anywhere near it. A test that cannot tell "the key is present" from
    "the word appears in a sentence about why the key is absent" is not
    verifying the invariant it claims to; this scopes it to what actually
    weakens the scanner, and it still covers the whole marker-to-EOF span,
    so a real `paths =` key added to any future block here is still caught.
    """
    text = CONFIG.read_text()
    marker = "Synthetic credential fixtures in the secret-scanner test suite"
    assert marker in text, "fixture allowlist block is missing"
    block = text[text.index(marker):]
    assert re.search(r"^\s*paths\s*=", block, re.M) is None, (
        "a `paths =` key was found from the synthetic-fixture allowlist "
        "onward. Value-scoped allowlists in this region must match by "
        "VALUE only - a `paths` entry suppresses the entire file, "
        "including a real credential."
    )


def test_no_dead_allowlist_entries() -> None:
    """Every allowlisted literal is still used by the suite.

    An exemption for a value nobody uses any more is furniture: it protects
    nothing and quietly widens the set of strings the scanner ignores.

    Scans both ``*.py`` and ``*.node.mjs`` fixtures under ``tests/``: the
    masking-test literal (``AKIA7Q2W9E4R6T8Y0U1I3O5P7A9S1D3F5G7H9J1K``, see
    .gitleaks.toml) lives only in the three node suites that share it, so a
    Python-only scan would call it dead and tell someone to remove a live
    exemption.
    """
    hay = "\n".join(
        p.read_text(errors="replace")
        for pattern in ("*.py", "*.node.mjs")
        for p in (REPO_ROOT / "tests").rglob(pattern)
    )
    for literal in _allowlist_regexes():
        assert literal in hay, (
            f"allowlisted fixture literal (len {len(literal)}) is no longer "
            "used by any test. Remove it from .gitleaks.toml rather than "
            "leaving a standing exemption behind."
        )


def test_jsonl_manifest_allowlist_pattern_matches_live_data() -> None:
    """The key_order_digest allowlist entry has its OWN dead-entry risk.

    This block does not fit test_no_dead_allowlist_entries above: it does
    not exempt a literal copied from a tests/*.py fixture, it exempts a
    STRUCTURAL regex over a field in tests/fixtures/jsonl_shape_manifest.json
    (see .gitleaks.toml for the full derivation and the 2026-08-31 positive
    control). "Dead" means something different for a pattern than for a
    literal: not "unused by any test file", but "matches nothing in the
    fixture it was written for". A pattern that stopped matching (the field
    got renamed, the digest length changed) would silently become an
    allowlist entry that protects nothing while still claiming to - the
    same furniture problem test_no_dead_allowlist_entries exists to catch,
    checked the way this entry's class actually needs it checked.
    """
    text = CONFIG.read_text()
    marker = "Derived key-order digests in the JSONL shape manifest fixture"
    assert marker in text, "jsonl manifest allowlist block is missing"
    block = text[text.index(marker):]
    match = re.search(r"regexes = \[\s*'''(.*?)'''", block, re.S)
    assert match, "no regex literal found in the jsonl manifest allowlist block"
    pattern = match.group(1)

    manifest = REPO_ROOT / "tests" / "fixtures" / "jsonl_shape_manifest.json"
    assert manifest.exists(), f"{manifest} is missing"
    hits = re.findall(pattern, manifest.read_text())
    assert hits, (
        "the jsonl manifest allowlist regex matches nothing in "
        f"{manifest}. Either the field it targets was renamed or removed "
        "(remove the dead allowlist entry) or the pattern drifted from the "
        "field's real shape (fix the pattern to match it again)."
    )


def test_hook_runs_both_scanners_with_the_repo_config() -> None:
    """The pre-commit hook must invoke gitleaks, not only the python scanner."""
    text = HOOK.read_text()
    assert "scan_secrets.py" in text or "$SCANNER" in text
    assert "gitleaks git --staged" in text, (
        "the hook must run gitleaks too, or it will keep passing commits "
        "that CI rejects"
    )
    assert ".gitleaks.toml" in text, "the hook must use the same config as CI"


def test_hook_can_read_the_pinned_version_from_the_workflow() -> None:
    """Version parity is only checkable if the pin is still parseable."""
    match = _PIN_RE.search(WORKFLOW.read_text())
    assert match, (
        "could not parse GITLEAKS_VERSION out of the workflow. The hook uses "
        "the same expression, so it has silently lost its parity check."
    )
    assert match.group(1).count(".") >= 1


@_needs_gitleaks
def test_config_loads_in_the_real_gitleaks() -> None:
    """A config that fails to load exits 1 and reads as 'leaks found'."""
    done = subprocess.run(
        ["gitleaks", "dir", "--config", str(CONFIG), "--no-banner", "."],
        cwd=REPO_ROOT / "docs",
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "failed to load config" not in done.stderr.lower(), done.stderr


@_needs_gitleaks
def test_gitleaks_still_reports_a_fresh_secret_in_an_allowlisted_file(
    tmp_path: Path,
) -> None:
    """POSITIVE CONTROL: the gate must still be able to fail.

    Plants a freshly generated high-entropy credential into a copy of an
    ALLOWLISTED fixture file. If the allowlist ever regresses to path
    scoping, this finding disappears and the test fails - which is the whole
    point. A gate that cannot fail is worse than the bug it was hiding.
    """
    alphabet = string.ascii_letters + string.digits
    planted = "".join(secrets.choice(alphabet) for _ in range(40))

    target = tmp_path / "tests"
    target.mkdir()
    src = REPO_ROOT / "tests" / "test_secret_detectors.py"
    (target / src.name).write_text(
        src.read_text() + f'\nAPI_SECRET_KEY = "{planted}"\n'
    )

    done = subprocess.run(
        [
            "gitleaks", "dir", ".",
            "--config", str(CONFIG),
            "--redact", "--no-banner",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 1, (
        "gitleaks did NOT report a freshly planted credential inside an "
        "allowlisted fixture file. The allowlist has been widened to cover "
        f"the file rather than the value.\nstdout: {done.stdout}\n"
        f"stderr: {done.stderr}"
    )
