"""The upgrade fixture the design doc demands: this machine's exact state.

docs/session-attribution-import.md, "Test obligations":

    An upgrade fixture reproducing this machine's exact state: 9 live
    tmux sessions, no session_metadata.json, empty owned set. Today that
    fixture produces 9 EXTERNAL. It must produce 0 EXTERNAL and 9
    UNKNOWN-prompted, or better where pipe evidence exists.

"Better where pipe evidence exists" is the operative clause. Two of the
nine have a created pipe on that machine, so the correct outcome is 2
proved OURS and 7 asked about, not 9 asked about.

WHAT "0 EXTERNAL" MEANS AND WHAT IT DOES NOT. The seven unattributed rows
still carry origin='observed' in the table, because writing anything else
would be inventing the verdict this whole exercise exists to stop. What
must be zero is the number of sessions written external WITHOUT THE USER
BEING ASKED. Every observed row here appears in
meta.session_import_unattributed, so the count that matters is
"observed and silent", and this file asserts that it is 0.

Every fact below is taken from the design doc's read-only measurement of
the live mac-mini-m4 install on 2026-08-21. Nothing here touched it.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, get_meta
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    META_SESSION_IMPORT_UNATTRIBUTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_import import run_first_run_import
from src.core.session_store import list_sessions
from src.core.tmux_listing import TmuxListing

#: The live tmux sessions on the mini's `cloude` socket, per the design
#: doc. Five carry a user-typed name; two carry the app's auto-generated
#: form; two more are the later console/test sessions.
LIVE_NAMES = [
    "cloude_Test",
    "cloude_asd",
    "cloude_claude-config-sync-2",
    "cloude_scrolltest",
    "cloude_test pause",
    "cloude_fs2",
    "cloude_fstest",
    "cloude_ses_ec5bf2a3",
    "cloude_ses_3529a738",
]

#: Exactly the files in ~/Library/Logs/cloude-code/ on that install.
#: Two CREATED pipes and seven ext_ pipes. Note tmux_ses_ec5bf2a3.pipe
#: sitting alongside tmux_ext_cloude_ses_ec5bf2a3.pipe: that pair is the
#: re-adopt case, and it is decided by the CREATED pipe.
PIPE_FILES = [
    "tmux_ses_3529a738.pipe",
    "tmux_ses_ec5bf2a3.pipe",
    "tmux_ext_cloude_ses_ec5bf2a3.pipe",
    "tmux_ext_cloude_console-msw4z3m5.pipe",
    "tmux_ext_cloude_fs2.pipe",
    "tmux_ext_cloude_fstest.pipe",
    "tmux_ext_cloude_Test.pipe",
    "tmux_ext_cloude_asd.pipe",
    "tmux_ext_cloude_claude-config-sync-2.pipe",
    "tmux_ext_cloude_scrolltest.pipe",
    "tmux_ext_cloude_test_pause.pipe",
]


@pytest.fixture()
def upgrade(tmp_path):
    """The mini's state: migrated db, its pipe files, no owned set."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in PIPE_FILES:
        (logs / name).write_text("")
    with closing(connect(db_path_for(tmp_path))) as conn:
        yield conn, logs


#: The socket every row in this file is keyed on. Named EXPLICITLY rather
#: than left to the module default: tests/conftest.py rewrites the default
#: socket name in three separate places to keep the suite off the live
#: server, so a fixture that seeds rows under one name and imports under
#: another sees an empty table and cheerfully "proves" an empty prompt.
FIXTURE_SOCKET = "cloudefixture"


def _run(conn, logs):
    """Run the import exactly as main.py does on an upgrading install."""
    return run_first_run_import(
        conn,
        socket=FIXTURE_SOCKET,
        listing=TmuxListing.answered(
            [
                {"name": n, "created_at_epoch": 1755000000 + i, "window_count": 1}
                for i, n in enumerate(LIVE_NAMES)
            ]
        ),
        # session_metadata.json is GONE on that machine, so the legacy
        # owned set the import is handed is empty. That is the input that
        # made all nine land external.
        owned_tmux_names=set(),
        log_dir=logs,
    )


def test_the_upgrade_fixture_leaves_ZERO_sessions_silently_external(upgrade):
    conn, logs = upgrade
    _run(conn, logs)
    rows = {r["tmux_name"]: r for r in list_sessions(conn)}
    assert len(rows) == 9

    records = json.loads(get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED))
    asked = {r["tmux_name"] for r in records}

    silently_external = [
        name
        for name, row in rows.items()
        if row["origin"] == SESSION_ORIGIN_OBSERVED and name not in asked
    ]
    assert silently_external == [], (
        "these sessions were written external and the user was never asked "
        f"about them: {silently_external}"
    )


def test_the_two_sessions_with_a_created_pipe_are_proved_OURS(upgrade):
    """Better than nine prompts, exactly where the evidence supports it."""
    conn, logs = upgrade
    _run(conn, logs)
    rows = {r["tmux_name"]: r for r in list_sessions(conn)}
    ours = {n for n, r in rows.items() if r["origin"] == SESSION_ORIGIN_CREATED}
    assert ours == {"cloude_ses_3529a738", "cloude_ses_ec5bf2a3"}
    for name in ours:
        assert rows[name]["lifecycle_source"] == "import:created_pipe"


def test_the_readopted_session_is_decided_by_the_CREATED_pipe(upgrade):
    """cloude_ses_ec5bf2a3 has BOTH pipes. The ext_ one explains history
    and decides nothing; the created one carries the proof."""
    conn, logs = upgrade
    _run(conn, logs)
    row = [r for r in list_sessions(conn) if r["tmux_name"] == "cloude_ses_ec5bf2a3"][0]
    assert row["origin"] == SESSION_ORIGIN_CREATED
    assert row["lifecycle_source"] == "import:created_pipe"


def test_the_other_seven_are_ASKED_ABOUT_not_decided(upgrade):
    """The five with only an ext_ pipe included. Their ext_ pipe is the
    app's own verdict, produced by the bug, so it can neither corroborate
    nor contradict the user - which is precisely a could-not-evaluate."""
    conn, logs = upgrade
    _run(conn, logs)
    records = json.loads(get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED))
    assert sorted(r["tmux_name"] for r in records) == sorted(
        [
            "cloude_Test",
            "cloude_asd",
            "cloude_claude-config-sync-2",
            "cloude_scrolltest",
            "cloude_test pause",
            "cloude_fs2",
            "cloude_fstest",
        ]
    )
    for rec in records:
        assert set(rec) == {"tmux_name", "epoch", "hints", "reason"}
        assert rec["reason"] == "no_admissible_evidence"


def test_what_the_OLD_import_did_to_the_same_fixture(upgrade):
    """The measured before, so the fix is a difference and not a claim.

    The pre-ladder resolver saw an empty owned set and answered
    'observed' for all nine, with nothing recorded anywhere that a user
    would ever be shown. That is what the sessions_import_evidence_version
    bump exists to re-open.
    """
    from src.core.session_store import observed_origin_for

    old = [observed_origin_for(n, set()) for n in LIVE_NAMES]
    assert old == [SESSION_ORIGIN_OBSERVED] * 9

    conn, logs = upgrade
    _run(conn, logs)
    rows = list_sessions(conn)
    new_observed = [r for r in rows if r["origin"] == SESSION_ORIGIN_OBSERVED]
    assert len(new_observed) == 7
    assert len(rows) - len(new_observed) == 2


# ===========================================================================
# The install as it ACTUALLY stands: the latch is already stamped
# ===========================================================================


def test_the_live_installs_re_run_promotes_nothing_and_asks_about_five(upgrade):
    """What this code will do to the ten rows measured on 2026-08-21.

    That install is NOT a first-run import. Its latch was stamped
    2026-08-18 with sessions_imported: 9, so the version bump takes the
    PROMOTE-ONLY re-run path, and the outcome is decided by which rows
    are still eligible:

      * the five ``adopted`` rows are already OURS. Never re-examined,
        never downgraded, and absent from the prompt.
      * the five ``observed`` rows are the ones with only an ``ext_``
        pipe. No admissible tier can reach them - their names were typed
        by the user, so no created pipe maps back, and tier 2 is not
        evidence. They stay ``observed`` and are ASKED ABOUT.

    So the honest expected number is 0 promoted and 5 prompted, not 5
    promoted. Recording it here so nobody reads a promotion count of zero
    as a failure: there is no evidence on that machine that would justify
    a different number, which is exactly what the design doc predicted
    under "What this does not fix".
    """
    from src.core.db import set_meta, transaction
    from src.core.db_models import (
        META_IMPORTED_FROM_JSON_RESULT,
        SESSION_ORIGIN_ADOPTED,
    )
    from src.core.session_identity import record_instance
    from src.core.session_import import (
        IMPORT_RERUN_COMPLETED,
        RESULT_KEY_SESSIONS_STAGE,
    )

    conn, logs = upgrade
    observed_names = [
        "cloude_Test",
        "cloude_asd",
        "cloude_claude-config-sync-2",
        "cloude_scrolltest",
        "cloude_test pause",
    ]
    with transaction(conn):
        for i, name in enumerate(LIVE_NAMES):
            record_instance(
                conn,
                # THE SAME SOCKET THE IMPORT QUERIES. See FIXTURE_SOCKET.
                socket=FIXTURE_SOCKET,
                name=name,
                epoch=1755000000 + i,
                origin=(
                    SESSION_ORIGIN_OBSERVED
                    if name in observed_names
                    else SESSION_ORIGIN_ADOPTED
                ),
                now="2026-08-18T18:41:13Z",
            )
        # The latch as it stands there: stamped, with no version key.
        set_meta(
            conn,
            META_IMPORTED_FROM_JSON_RESULT,
            json.dumps({RESULT_KEY_SESSIONS_STAGE: "2026-08-18T18:41:13.152941Z"}),
        )

    result = _run(conn, logs)
    assert result.outcome == IMPORT_RERUN_COMPLETED
    assert result.promoted == 0

    rows = {r["tmux_name"]: r for r in list_sessions(conn)}
    assert len(rows) == 9, "the re-run must not INSERT a row"
    assert {n for n, r in rows.items() if r["origin"] == SESSION_ORIGIN_ADOPTED} == (
        set(LIVE_NAMES) - set(observed_names)
    )

    records = json.loads(get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED))
    assert sorted(r["tmux_name"] for r in records) == sorted(observed_names)


def test_POSITIVE_CONTROL_the_same_re_run_DOES_promote_when_evidence_exists(
    upgrade,
):
    """The control for the test above, and it is not optional.

    A re-run that promotes nothing because there is no evidence, and a
    re-run that promotes nothing because its eligibility query is broken,
    produce identical output. This one seeds the SAME shape with one of
    the observed rows renamed to a session that HAS a created pipe, and
    requires the promotion to happen - so a promotion count of zero above
    is a measured absence of evidence rather than an unmeasured absence
    of function.
    """
    from src.core.db import set_meta, transaction
    from src.core.db_models import (
        META_IMPORTED_FROM_JSON_RESULT,
        SESSION_ORIGIN_ADOPTED,
    )
    from src.core.session_identity import record_instance
    from src.core.session_import import (
        IMPORT_RERUN_COMPLETED,
        RESULT_KEY_SESSIONS_STAGE,
    )

    conn, logs = upgrade
    # cloude_ses_3529a738 has tmux_ses_3529a738.pipe. Seeded OBSERVED here
    # rather than adopted, which is the only difference from the test above.
    observed_names = [
        "cloude_Test",
        "cloude_asd",
        "cloude_claude-config-sync-2",
        "cloude_scrolltest",
        "cloude_ses_3529a738",
    ]
    with transaction(conn):
        for i, name in enumerate(LIVE_NAMES):
            record_instance(
                conn,
                socket=FIXTURE_SOCKET,
                name=name,
                epoch=1755000000 + i,
                origin=(
                    SESSION_ORIGIN_OBSERVED
                    if name in observed_names
                    else SESSION_ORIGIN_ADOPTED
                ),
                now="2026-08-18T18:41:13Z",
            )
        set_meta(
            conn,
            META_IMPORTED_FROM_JSON_RESULT,
            json.dumps({RESULT_KEY_SESSIONS_STAGE: "2026-08-18T18:41:13.152941Z"}),
        )

    result = _run(conn, logs)
    assert result.outcome == IMPORT_RERUN_COMPLETED
    assert result.promoted == 1

    rows = {r["tmux_name"]: r for r in list_sessions(conn)}
    assert rows["cloude_ses_3529a738"]["origin"] == SESSION_ORIGIN_CREATED
    assert rows["cloude_ses_3529a738"]["lifecycle_source"] == (
        "import:rerun:created_pipe"
    )

    records = json.loads(get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED))
    assert sorted(r["tmux_name"] for r in records) == sorted(
        set(observed_names) - {"cloude_ses_3529a738"}
    )
