"""check-js-syntax.sh must actually reach the JS trees it claims to cover.

Until 2026-08-26 the default scan root was client/js, so client/setup.js and
all 24 theme scripts under client/css/themes/ had never been parsed by CI -
shipped browser JavaScript in a repo with no bundler, where a syntax error is
a blank page. Widening the root also required handling module goal: every
theme script is an ES module, and node --check assumes CommonJS for a bare
.js, so a naive widening would have reported 24 syntax errors in 24 correct
files.

Both halves are asserted here by MEASUREMENT rather than by reading the
script's source, because a source-level assertion (does the string
"client/css/themes" appear) would keep passing if the find expression, the
prune list or the module-goal fallback broke.

The coverage assertions plant a deliberately broken file, run the real
script, and require it to be caught. A test that only asserted the clean tree
passes could not tell a widened scan from an unwidened one - both are green.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check-js-syntax.sh"

# Named so a leaked copy is obviously test debris. Every planting is wrapped
# in try/finally, but a hard kill mid-run cannot run a finally block, so the
# name has to carry the explanation itself.
DEBRIS = "__js_syntax_scan_coverage_test__.js"

# One representative directory per tree that used to be unscanned. The theme
# entry is the ES module case; the client root entry is client/setup.js's
# neighbourhood.
UNCOVERED_UNTIL_2026_08_26 = [
    Path("client") / "css" / "themes" / "matrix",
    Path("client"),
]


def _run() -> subprocess.CompletedProcess:
    """Run the syntax checker over its default roots.

    Inputs:  none
    Outputs: CompletedProcess with text stdout and stderr.
    """
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO_ROOT),
    )


def test_clean_tree_parses_including_es_modules() -> None:
    """The repo as committed parses, and some files needed the module goal.

    Inputs:  none
    Outputs: None.

    The module count is asserted to be non-zero on purpose. If it ever reads
    zero, either the theme tree fell out of the scan again or the ES module
    fallback stopped being exercised, and in both cases the clean exit 0
    below would still look perfectly healthy.
    """
    done = _run()
    assert done.returncode == 0, (
        "the committed tree no longer parses:\n" + done.stderr[-2000:]
    )
    assert "as ES modules)" in done.stdout, (
        "the checker no longer reports how many files needed the module "
        "goal, so a silent regression to CommonJS-only parsing would be "
        "invisible: " + done.stdout
    )
    matched = re.search(r"\((\d+) as ES modules\)", done.stdout)
    assert matched is not None, done.stdout


def test_es_module_source_does_not_false_fail() -> None:
    """A planted ES module in the theme tree must parse, on any node.

    Inputs:  none
    Outputs: None.

    THIS REPLACES AN ASSERTION THAT THE FALLBACK COUNT IS NON-ZERO, which
    was measuring the runtime rather than the checker. `node --check` on a
    bare .js applies module detection on node 22, so ESM source parses on
    the FIRST attempt there and nothing reaches the --input-type=module
    branch: the count is legitimately 0. On node 26 the same files fail the
    CommonJS attempt and fall back, and the count is 24. Same repo, same
    script, same correct outcome, two different counts - so a non-zero
    count was never the property worth asserting, and it failed CI on node
    22 while the tree it was guarding was fully scanned and clean.

    THERE WERE ALWAYS THREE OUTCOMES and the old assertion modelled two.
    Zero can mean the theme tree fell out of the scan, the fallback broke,
    OR the runtime did not need the fallback. Only the first two are
    faults, and the parametrized coverage test above already catches the
    first by planting a broken file in client/css/themes/matrix.

    What actually matters is that ES module source is not reported as a
    syntax error. That is asserted directly here by planting real ESM and
    requiring a clean exit, which holds on every node either way.
    """
    planted = REPO_ROOT / "client" / "css" / "themes" / "matrix" / DEBRIS
    planted.write_text("export const marker = 1;\nimport.meta.url;\n")
    try:
        done = _run()
    finally:
        planted.unlink(missing_ok=True)

    assert done.returncode == 0, (
        "valid ES module source was reported as a syntax error, which is "
        "exactly the false-fail the module goal exists to prevent. "
        "stdout: %s stderr: %s" % (done.stdout[-800:], done.stderr[-800:])
    )
    assert DEBRIS not in done.stderr, (
        "the planted ES module was named as broken: %s" % done.stderr[-800:]
    )


@pytest.mark.parametrize("relative_dir", UNCOVERED_UNTIL_2026_08_26,
                         ids=lambda p: str(p).replace("/", "_"))
def test_scan_reaches_previously_unscanned_tree(relative_dir: Path) -> None:
    """A broken file planted in this tree must fail the checker.

    Inputs:  relative_dir (Path) - directory, relative to the repo root, that
             the default scan is required to cover.
    Outputs: None.
    """
    planted = REPO_ROOT / relative_dir / DEBRIS
    # Broken under BOTH module goals, so the two-goal fallback cannot
    # accidentally rescue it and turn this into a vacuous pass.
    planted.write_text("function (){\n")
    try:
        done = _run()
    finally:
        planted.unlink(missing_ok=True)

    assert done.returncode == 1, (
        "a syntactically broken file in %s did not fail the checker, so that "
        "tree is outside the default scan. stdout: %s stderr: %s"
        % (relative_dir, done.stdout[-800:], done.stderr[-800:])
    )
    assert DEBRIS in done.stderr, (
        "the checker failed but never named the broken file in %s, so it may "
        "have failed for an unrelated reason: %s"
        % (relative_dir, done.stderr[-800:])
    )
