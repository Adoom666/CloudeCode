"""S7 - the WIRING that feeds the attribution rule, and the cwd probe.

Split out of tests/test_project_attribution.py, which keeps the RULE.
The split follows the bug rather than the file: the rule was never
broken, and no amount of testing it would have found the defect. What
was broken is that nothing ever handed it a working directory, so this
file tests the collection path - the tmux probe, the import call site,
and the backfill that repairs rows the one-way import already froze.

WHY THE PROBE TESTS DRIVE REAL TMUX. A mocked subprocess agrees with
whatever argv the probe happens to build, including a broken one - and
the argv is where this actually went wrong. The first version of the
probe asked for ``#{session_path}`` with the target ``=<name>``, which
returns an EMPTY STRING on tmux 3.7b, because display-message's ``-t``
is a target-PANE and ``=<name>`` asks for an exact pane of that name.
Every probe in production would have answered nothing, every row would
have stayed ``unknown``, and a mock would have passed. Found by the
mutation suite, fixed to ``=<name>:``, and pinned by the tests below.

Every tmux call here runs against THIS run's isolated socket, which
``tests/socket_guard.py`` enforces - no live session is reachable.
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
from src.core.session_attribution import backfill_attribution  # noqa: E402
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_store import needs_attention  # noqa: E402
from tests.s7_helpers import (  # noqa: E402
    TEST_SOCKET,
    insert_project,
    migrated_connection,
    session_row,
)


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


# --- the wiring, against a fixture shaped like his real data ---------------


def _seed_nine_and_nine(conn):
    """Build a projects/sessions fixture shaped like the live install.

    Description: nine projects and nine sessions, reproducing the three
      properties of his real data that matter: nested roots (``~``,
      ``~/Development`` and ``~/Development/<repo>`` all exist), roots
      stored expanded while ``raw_path`` is still tilde-form, and one
      session whose directory genuinely sits outside every project.
    Inputs: conn (sqlite3.Connection).
    Output: dict - tmux name -> the working directory seeded for it.
    """
    home = str(Path.home())
    projects = {
        f"{home}": "console-msw4z3m5",
        f"{home}/Development": "Development",
        f"{home}/Development/CloudeCode": "CloudeCode",
        f"{home}/Development/ai-setup": "ai-setup",
        f"{home}/Development/claude-config-sync": "claude-config-sync-2",
        f"{home}/Development/scrolltest": "fs2",
        f"{home}/Development/ses_ec5bf2a3": "test pause",
        f"{home}/Development/ses_8704e610": "asd",
        f"{home}/Development/ses_c3737fbe": "Test",
    }
    ids = {}
    for root, name in projects.items():
        raw = root.replace(home, "~", 1) if root != home else home
        ids[root] = insert_project(conn, root, name, raw_path=raw)

    dirs = {
        "cloude_Test": f"{home}/Development/ses_c3737fbe",
        "cloude_asd": f"{home}/Development/ses_8704e610",
        "cloude_claude-config-sync-2": f"{home}/Development/claude-config-sync",
        "cloude_console-msw4z3m5": home,
        "cloude_fs2": f"{home}/Development/scrolltest",
        # A SUBDIRECTORY, and one contained by three nested roots.
        "cloude_fstest": f"{home}/Development/CloudeCode/src/core",
        # TILDE FORM, as a probe or a hand-edited value may well answer.
        "cloude_scrolltest": "~/Development/scrolltest",
        "cloude_ses_ec5bf2a3": f"{home}/Development/ses_ec5bf2a3",
        # Genuinely outside every project root.
        "cloude_test pause": "/opt/somewhere-else",
    }
    for index, (name, working_dir) in enumerate(dirs.items(), start=1):
        record_instance(
            conn, socket=TEST_SOCKET, name=name, epoch=1786900000 + index,
            origin=SESSION_ORIGIN_OBSERVED,
            project_attribution=SESSION_ATTRIBUTION_UNKNOWN,
            working_dir=working_dir,
        )
    return ids, dirs


def test_a_realistic_nine_and_nine_fixture_actually_resolves(conn):
    """The claim the live database failed: attribution RESOLVES.

    Eight of nine sessions land on a project, one lands definitively on
    no project, and NONE of them defaults to unknown. Before this change
    every single one of these rows came out unknown.
    """
    ids, _dirs = _seed_nine_and_nine(conn)
    home = str(Path.home())

    result = backfill_attribution(conn)

    assert result.considered == 9
    assert result.attributed_project == 8
    assert result.attributed_none == 1
    assert result.still_unknown == 0, (
        "a session defaulted to unknown over a fully-probed fixture, "
        "which is the exact state the live database was already in"
    )

    # The nested case: fstest is inside CloudeCode, which is inside
    # Development, which is inside home. It must land on CloudeCode.
    assert session_row(conn, "cloude_fstest")["project_id"] == (
        ids[f"{home}/Development/CloudeCode"])
    # The tilde case matches the expanded root.
    assert session_row(conn, "cloude_scrolltest")["project_id"] == (
        ids[f"{home}/Development/scrolltest"])
    # The home-rooted session lands on the home project, not on nothing.
    assert session_row(conn, "cloude_console-msw4z3m5")["project_id"] == (
        ids[home])
    # And the one genuinely outside is NONE, with no project id.
    outside = session_row(conn, "cloude_test pause")
    assert outside["project_attribution"] == SESSION_ATTRIBUTION_NONE
    assert outside["project_id"] is None

    # NEEDS ATTENTION is empty of attribution problems, which is the point.
    unknowns = [
        row for row in needs_attention(conn)
        if row["project_attribution"] == SESSION_ATTRIBUTION_UNKNOWN
    ]
    assert unknowns == []


def test_the_backfill_probes_when_the_row_has_no_stored_directory(conn):
    """His rows have working_dir NULL, so the repair must go and look."""
    home = str(Path.home())
    project_id = insert_project(conn, f"{home}/Development/CloudeCode", "CC")
    record_instance(
        conn, socket=TEST_SOCKET, name="cloude_needsprobe", epoch=99,
        origin=SESSION_ORIGIN_OBSERVED,
        project_attribution=SESSION_ATTRIBUTION_UNKNOWN,
    )
    assert session_row(conn, "cloude_needsprobe")["working_dir"] is None

    probed = {"cloude_needsprobe": f"{home}/Development/CloudeCode/src"}
    result = backfill_attribution(
        conn, working_dir_probe=lambda name: probed.get(name)
    )

    assert result.attributed_project == 1
    row = session_row(conn, "cloude_needsprobe")
    assert row["project_id"] == project_id
    assert row["working_dir"] == probed["cloude_needsprobe"], (
        "the probed directory must be stored so the next pass needs no "
        "tmux call"
    )


def test_the_backfill_never_writes_unknown_over_anything(conn):
    """An unprobeable row is LEFT ALONE, not restamped every boot.

    Recording the absence of an answer as a fresh write would make a
    permanently-broken probe look like activity, and would churn
    updated_at on every single start.
    """
    record_instance(
        conn, socket=TEST_SOCKET, name="cloude_blind", epoch=7,
        origin=SESSION_ORIGIN_OBSERVED,
        project_attribution=SESSION_ATTRIBUTION_UNKNOWN,
        now="2026-01-01T00:00:00Z",
    )
    before = session_row(conn, "cloude_blind")

    result = backfill_attribution(
        conn, working_dir_probe=lambda _name: None,
        now="2026-12-12T00:00:00Z",
    )

    assert result.still_unknown == 1
    assert result.written == 0
    after = session_row(conn, "cloude_blind")
    assert after["updated_at"] == before["updated_at"]
    assert after["project_attribution"] == SESSION_ATTRIBUTION_UNKNOWN


def test_the_backfill_never_overwrites_a_measured_attribution(conn):
    """'none' is an answer somebody measured; re-deriving it is not free.

    A transient probe must not be able to move a settled fact, in either
    direction.
    """
    home = str(Path.home())
    insert_project(conn, f"{home}/Development", "Development")
    record_instance(
        conn, socket=TEST_SOCKET, name="cloude_settled", epoch=8,
        origin=SESSION_ORIGIN_OBSERVED,
        project_attribution=SESSION_ATTRIBUTION_NONE,
        working_dir=f"{home}/Development/thing",
    )
    result = backfill_attribution(conn)
    assert result.considered == 0
    row = session_row(conn, "cloude_settled")
    assert row["project_attribution"] == SESSION_ATTRIBUTION_NONE
    assert row["project_id"] is None


def test_the_probe_returns_none_rather_than_a_home_directory_guess():
    """The cwd probe must never fall back the way _resolve_external_cwd does.

    ``session_manager._resolve_external_cwd`` falls back to ``~``, which
    is right for ITS purpose and catastrophic here: his projects table
    HAS a project rooted at the home directory, so a home fallback would
    have attributed every failed probe to a real project and looked
    entirely plausible.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    # An empty name never shells out at all.
    assert probe_session_working_dir("", socket="nosuchsocket") is None
    # A real tmux call, against THIS run's isolated test socket (the
    # guard in tests/socket_guard.py refuses any other), for a session
    # that does not exist there. tmux exits non-zero and the probe must
    # answer None rather than inventing a directory.
    assert probe_session_working_dir(
        "definitely-not-a-session-s7", socket=TEST_SOCKET_NAME
    ) is None


@pytest.fixture()
def tmux_session(tmp_path):
    """Create a real tmux session on THIS run's isolated test socket.

    Description: the probe tests below need tmux to actually answer, and
      a mocked subprocess would prove nothing about the argv the probe
      builds - which is where the exact-match target operator lives.
      ``tests/socket_guard.py`` refuses any socket but this run's, so
      none of the user's live ``cloude`` sessions can be reached.
    Inputs: tmp_path (Path) - used as the session's working directory.
    Output: callable(name, directory) -> None, creating a session.
      Every session created is killed on teardown.
    """
    import shutil
    import subprocess

    from tests.socket_guard import TEST_SOCKET_NAME

    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed on this machine")

    created = []

    def _make(name: str, directory: Path) -> None:
        """Create one detached tmux session rooted at a directory.

        Inputs: name (str), directory (Path).
        Output: None.
        """
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["tmux", "-L", TEST_SOCKET_NAME, "new-session", "-d",
             "-s", name, "-c", str(directory)],
            check=True, capture_output=True,
        )
        created.append(name)

    yield _make

    for name in created:
        subprocess.run(
            ["tmux", "-L", TEST_SOCKET_NAME, "kill-session", "-t", f"={name}"],
            check=False, capture_output=True,
        )


def test_the_probe_reads_a_real_tmux_sessions_directory(tmux_session, tmp_path):
    """The happy path, against real tmux rather than a mocked subprocess.

    A mock would agree with whatever argv the probe happened to build,
    including a broken one, which is the whole reason this shells out.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    workdir = tmp_path / "s7probe-dir"
    tmux_session("s7probe", workdir)

    answer = probe_session_working_dir("s7probe", socket=TEST_SOCKET_NAME)
    assert answer is not None, "tmux answered but the probe returned None"
    assert Path(answer).name == "s7probe-dir", answer


def test_the_probe_will_not_prefix_match_a_different_session(
    tmux_session, tmp_path
):
    """The ``=`` exact-match target operator, proved against real tmux.

    Without it tmux treats the target as a PATTERN, so asking for a
    session that does not exist returns a DIFFERENT session's directory
    whenever one shares the prefix. That is an identity error and it is
    completely silent: the probe returns a real, plausible path for the
    wrong session, and attribution files the row under the wrong project.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    # Only the LONGER name exists. The shorter one is a prefix of it.
    tmux_session("s7prefix2", tmp_path / "wrong-session-dir")

    answer = probe_session_working_dir("s7prefix", socket=TEST_SOCKET_NAME)
    assert answer is None, (
        "the probe prefix-matched a different session and returned "
        f"{answer!r}. tmux targets are patterns unless prefixed with '='"
    )


def test_a_failed_probe_returns_none_and_not_a_directory(
    tmux_session, tmp_path
):
    """Every failure path answers None, with a real session on the socket.

    The socket is live and has a session on it, so a non-answer here is
    genuinely "that session is not there" rather than "tmux is down" -
    which is the case a fallback would be most tempted to paper over.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    tmux_session("s7alive", tmp_path / "alive")

    answer = probe_session_working_dir("s7absent", socket=TEST_SOCKET_NAME)
    assert answer is None
    assert answer != str(Path.home()), (
        "the probe fell back to the home directory. His projects table "
        "has a project rooted at the home directory, so that guess would "
        "attribute every failed probe to a real project"
    )


def test_a_name_carrying_a_tmux_target_separator_is_refused(
    tmux_session, tmp_path
):
    """A ``:`` in the name makes tmux target a DIFFERENT session.

    Measured, not reasoned about: with a session ``s7sep`` on the socket
    and nothing named ``s7sep:0``, the target ``=s7sep:0:`` resolves to
    ``s7sep`` and hands back its directory. So a session whose name
    happens to contain a colon would be attributed the working directory
    of an unrelated session, silently, and the row would be filed under
    the wrong project. Same rule and same reason as
    ``tmux_backend._safe_target``: refuse the name, do not format it.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    tmux_session("s7sep", tmp_path / "some-other-sessions-dir")

    answer = probe_session_working_dir("s7sep:0", socket=TEST_SOCKET_NAME)
    assert answer is None, (
        "a name containing a tmux target separator was formatted into a "
        f"target and resolved to another session, returning {answer!r}"
    )


def test_a_probe_that_times_out_answers_none_rather_than_guessing(
    tmux_session, tmp_path
):
    """The timeout branch, driven for real rather than asserted by reading.

    A wedged tmux server must produce CANNOT DETERMINE. Falling back to
    any directory here would attribute the session to whatever project
    contains that directory - and on his machine the home directory IS a
    project.
    """
    from src.core.tmux_session_cwd import probe_session_working_dir
    from tests.socket_guard import TEST_SOCKET_NAME

    tmux_session("s7timeout", tmp_path / "timeout-dir")

    # A budget no process can meet forces the TimeoutExpired path.
    answer = probe_session_working_dir(
        "s7timeout", socket=TEST_SOCKET_NAME, timeout=0.000001
    )
    assert answer is None, (
        f"a timed-out probe returned {answer!r} instead of admitting it "
        "could not determine the working directory"
    )
    assert answer != str(Path.home())


def test_the_import_call_site_actually_passes_a_working_dir_probe():
    """The WIRING, at the only place that can be wrong about it.

    This is the defect itself: the rule was fine and nothing fed it. No
    behavioural test of the rule can catch a caller that passes None, and
    src/main.py's lifespan is not reachable from a unit test, so the call
    site is asserted by parsing it. The assertion is deliberately narrow -
    the keyword must be PRESENT and must not be a literal None - so it
    cannot be satisfied by the argument merely existing.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "main.py").read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "run_first_run_import"
    ]
    assert calls, "run_first_run_import is not called from src/main.py at all"
    for call in calls:
        probe = [kw for kw in call.keywords if kw.arg == "working_dir_probe"]
        assert probe, (
            "run_first_run_import is called without working_dir_probe. "
            "That is the live bug: every session imports with working_dir "
            "NULL and project_attribution 'unknown'"
        )
        value = probe[0].value
        assert not (isinstance(value, ast.Constant) and value.value is None), (
            "working_dir_probe is passed as a literal None, which is the "
            "same as not passing it"
        )


def test_the_import_attributes_nothing_without_a_probe_and_resolves_with_one(
    conn, tmp_path
):
    """The two halves of the bug, side by side, through the real import.

    Same projects, same tmux listing, same code - the ONLY difference is
    whether a working-directory probe is supplied. Without one every row
    is 'unknown', which is exactly the state of the live database.
    """
    from src.core.session_import import run_first_run_import
    from tests.s7_helpers import listing_of, listing_row

    home = str(Path.home())
    insert_project(conn, f"{home}/Development/CloudeCode", "CloudeCode")
    listing = listing_of([listing_row("cloude_cc", 4242)])

    result = run_first_run_import(
        conn, listing=listing, socket=TEST_SOCKET,
        working_dir_probe=None,
    )
    assert result.sessions_imported == 1
    blind = session_row(conn, "cloude_cc")
    assert blind["project_attribution"] == SESSION_ATTRIBUTION_UNKNOWN
    assert blind["project_id"] is None
    assert blind["working_dir"] is None

    # Now the same row, repaired by the same rule once it has an input.
    repaired = backfill_attribution(
        conn,
        working_dir_probe=lambda _n: f"{home}/Development/CloudeCode/src",
    )
    assert repaired.attributed_project == 1
    seeing = session_row(conn, "cloude_cc")
    assert seeing["project_attribution"] == SESSION_ATTRIBUTION_DERIVED_DEEPEST
    assert seeing["project_id"] is not None
