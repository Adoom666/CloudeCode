"""S7 - matching a session's working directory onto a project.

THE BUG THIS SUITE PINS DOWN, stated as it was measured rather than as
it was guessed. On the live install all nine sessions carried
``project_id NULL`` and ``project_attribution='unknown'`` while all nine
projects existed in the same database. The matching rule was not broken.
The INPUT was never collected: ``LISTING_FORMAT`` carries no path field,
and the one real caller of the first-run import passed no
``working_dir_probe``, so every row's ``working_dir`` was NULL and
``attribute(None, roots)`` correctly answered "I could not read it".

So this file proves two separate things, and both matter:

  THE RULE   exact, subdirectory, tilde-vs-absolute and symlink cases
             resolve the way the design says, including the deepest-root
             tie-break and the component-boundary rule that stops
             ``/a/bc`` matching ``/a/b``.
  THE WIRING a realistic fixture shaped like his actual nine-and-nine
             data ACTUALLY RESOLVES rather than defaulting to unknown.
             A rule that works on paper while nothing feeds it is
             exactly the state the live database was already in, and no
             amount of unit testing on the rule alone would have caught
             it.

AND THE THIRD OUTCOME, which is the crux. ``none`` means the working
directory was READ and belongs to no known project - a complete,
actionable answer. ``unknown`` means it could not be read or could not
be situated - not an answer, and the only one that lands the row in
NEEDS ATTENTION. A session is NEVER guessed onto the nearest project,
and a failed probe NEVER renders as ``none``.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import (  # noqa: E402
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_ATTRIBUTION_NONE,
    SESSION_ATTRIBUTION_UNKNOWN,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.project_attribution import (  # noqa: E402
    attribute,
    attribution_is_determined,
    normalize_path_for_match,
    normalize_roots,
    unresolved_roots,
)
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_store import needs_attention  # noqa: E402
from tests.s7_helpers import TEST_SOCKET, migrated_connection  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    with closing(migrated_connection(tmp_path)):
        pass
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


# --- the rule --------------------------------------------------------------


def test_an_exact_match_attributes_to_that_project():
    """working_dir IS the project root."""
    assert attribute("/Users/j/Development/CloudeCode",
                     {"/Users/j/Development/CloudeCode": 7}) == (
        7, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_a_subdirectory_attributes_to_the_project_above_it():
    """working_dir sits under the project root."""
    assert attribute("/Users/j/Development/CloudeCode/src/api",
                     {"/Users/j/Development/CloudeCode": 7}) == (
        7, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_the_deepest_containing_root_wins_not_the_first_or_the_shortest():
    """His real data has BOTH ~/Development and ~/Development/CloudeCode.

    A session inside CloudeCode is contained by both. Attributing it to
    ``Development`` would be technically true and useless - the tree
    would show it under the wrong node.
    """
    roots = {
        "/Users/j/Development": 8,
        "/Users/j/Development/CloudeCode": 7,
        "/Users/j": 5,
    }
    assert attribute("/Users/j/Development/CloudeCode/src", roots) == (
        7, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_deepest_is_measured_in_components_not_string_length():
    """A longer STRING is not a deeper PATH.

    The old rule ranked by ``len(root)``, which happens to order two
    NESTED roots correctly and says nothing intelligible about any other
    pair. Here the shallower root has the longer name.
    """
    roots = {
        "/averyveryverylongtopleveldirectoryname": 1,
        "/a/b/c": 2,
    }
    assert attribute("/a/b/c/d", roots) == (
        2, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_a_sibling_sharing_a_name_prefix_does_not_match():
    """/a/bc must not match a project rooted at /a/b.

    A string-prefix comparison passes this wrongly, which is why every
    comparison walks path components.
    """
    assert attribute("/a/bc/session", {"/a/b": 3}) == (
        None, SESSION_ATTRIBUTION_NONE)


def test_a_tilde_working_dir_matches_an_expanded_root():
    """~/Development and /Users/you/Development are one path, named twice.

    His projects table stores expanded roots while several ``raw_path``
    values are still ``~``-form. A probe that answers in tilde form must
    still match.
    """
    home = str(Path.home())
    assert attribute("~/s7project/sub", {f"{home}/s7project": 4}) == (
        4, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_a_tilde_root_matches_an_absolute_working_dir():
    """The same equivalence in the other direction.

    normalize_roots expands the ROOT side too, so a hand-edited config
    entry cannot become a project that silently never matches anything.
    """
    home = str(Path.home())
    assert attribute(f"{home}/s7project/sub", {"~/s7project": 4}) == (
        4, SESSION_ATTRIBUTION_DERIVED_DEEPEST)


def test_a_symlinked_path_is_matched_as_written_and_never_resolved(tmp_path):
    """S3's rule, at the layer that would be tempted to break it.

    The project is DECLARED at the symlink. A session inside the symlink
    must attribute to it, and the path must not be rewritten to the
    symlink's target - resolving would relocate the user's session into
    a directory he never typed and detach it from the project he
    declared.
    """
    real = tmp_path / "real-project"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "linked-project"
    link.symlink_to(real, target_is_directory=True)

    declared_root = str(link)
    working_dir = str(link / "src")

    project_id, attribution = attribute(working_dir, {declared_root: 11})
    assert (project_id, attribution) == (
        11, SESSION_ATTRIBUTION_DERIVED_DEEPEST)
    # The path is preserved verbatim: no component was rewritten.
    assert normalize_path_for_match(working_dir) == working_dir
    assert "real-project" not in normalize_path_for_match(working_dir)


def test_a_symlinked_session_does_not_match_the_targets_project(tmp_path):
    """The documented, deliberate miss - asserted so nobody "fixes" it.

    Matching the target would require resolve(), and resolve() rewrites
    the user's own path. The honest answer here is ``none``: no DECLARED
    root contains that path AS WRITTEN. If this test ever starts
    failing, somebody added a resolve().
    """
    real = tmp_path / "real-project"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "linked-project"
    link.symlink_to(real, target_is_directory=True)

    project_id, attribution = attribute(str(link / "src"), {str(real): 11})
    assert (project_id, attribution) == (None, SESSION_ATTRIBUTION_NONE)


def test_no_call_in_the_attribution_path_resolves_a_path():
    """A source-level guard on the one call that must never appear.

    Checked by parsing rather than by grep: grep here is a shell
    function behaving like -I that silently skips files it deems binary
    and exits 1, so an empty grep is not evidence of absence.
    """
    import ast

    for module in ("project_attribution", "session_attribution",
                   "session_adopt_persist", "tmux_session_cwd"):
        source = (ROOT / "src" / "core" / f"{module}.py").read_text()
        tree = ast.parse(source)
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("resolve", "realpath")
        ]
        assert offenders == [], (
            f"{module}.py calls resolve()/realpath() at lines {offenders}. "
            "That collapses symlinks and rewrites the path the user chose"
        )


# --- the third outcome -----------------------------------------------------


def test_a_directory_in_no_project_is_NONE_and_that_is_a_real_answer():
    """Read it, matched nothing. Complete, actionable, not an error."""
    project_id, attribution = attribute("/somewhere/else", {"/a/b": 1})
    assert project_id is None
    assert attribution == SESSION_ATTRIBUTION_NONE
    assert attribution_is_determined(attribution) is True


def test_an_unreadable_working_dir_is_UNKNOWN_and_never_none():
    """The probe did not answer. Not the same claim at all."""
    for unreadable in (None, "", "   "):
        project_id, attribution = attribute(unreadable, {"/a/b": 1})
        assert project_id is None
        assert attribution == SESSION_ATTRIBUTION_UNKNOWN
        assert attribution_is_determined(attribution) is False


def test_a_path_that_cannot_be_situated_is_UNKNOWN_not_none():
    """Relative paths and .. segments cannot be placed without resolving.

    Calling them ``none`` would assert they belong to no project, which
    is a claim nothing measured.
    """
    for unsituatable in ("relative/path", "/a/../b", "../up"):
        _, attribution = attribute(unsituatable, {"/a": 1, "/b": 2})
        assert attribution == SESSION_ATTRIBUTION_UNKNOWN, unsituatable


def test_none_and_unknown_are_different_values_at_the_store_layer(conn):
    """NEEDS ATTENTION must contain the unknown and NOT the none.

    Asserted through session_store.needs_attention, which is what the
    home screen's fourth group actually reads - not through the enum.
    """
    record_instance(
        conn, socket=TEST_SOCKET, name="knows", epoch=1,
        origin=SESSION_ORIGIN_OBSERVED,
        project_attribution=SESSION_ATTRIBUTION_NONE,
    )
    record_instance(
        conn, socket=TEST_SOCKET, name="cannot-tell", epoch=2,
        origin=SESSION_ORIGIN_OBSERVED,
        project_attribution=SESSION_ATTRIBUTION_UNKNOWN,
    )
    flagged = {row["tmux_name"] for row in needs_attention(conn)}
    assert "cannot-tell" in flagged
    assert "knows" not in flagged, (
        "'belongs to no project' is a complete answer and must not be "
        "surfaced as something the user has to look at"
    )


def test_a_root_that_cannot_be_situated_is_dropped_and_named():
    """A silent drop is how this class of bug hides for a whole install."""
    roots = {"/good": 1, "relative-root": 2}
    assert normalize_roots(roots) == {"/good": 1}
    assert unresolved_roots(roots.keys()) == ("relative-root",)


def test_duplicate_roots_resolve_to_the_lowest_project_id():
    """Two roots normalising to one path must pick the same survivor
    the import's keep-the-first rule picked, or attribution and the
    projects list disagree about which project a session belongs to.
    """
    home = str(Path.home())
    assert normalize_roots({f"{home}/dup": 9, "~/dup": 3}) == {
        f"{home}/dup": 3}
