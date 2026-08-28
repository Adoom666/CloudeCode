"""The ``data`` block's trail_status, including intact-but-frozen.

Design 9.7 lists four trail_status values, ``ok | interrupted |
unreadable | paused``. The shipped block had ``absent`` in place of
``paused``, so a readable trail that this install will never add another
line to reported ``ok`` - a client rendering that reads "the history is
current and will stay current", which is exactly the false green the
three-outcome rule exists to kill. This module pins the fifth value and
the boundary between it and ``unreadable``.

It also asserts, at the API surface rather than at the startup verdict,
that code below the database's schema version refuses, degrades, and
names BOTH numbers. tests/test_db_degraded_states.py already asserts that
of the DatastoreState; the point here is that the numbers a person needs
in order to choose between "restore the newer app" and "restore the data"
actually reach the endpoint they would read them from.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from tests.datastore_helpers import ROOT  # noqa: F401  (sets test env vars)

from src.core.db import connect, db_path_for, set_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_state import (
    STATUS_DEGRADED_SCHEMA_AHEAD,
    STATUS_OK,
    TRAIL_STATUS_ABSENT,
    TRAIL_STATUS_OK,
    TRAIL_STATUS_PAUSED,
    TRAIL_STATUS_UNREADABLE,
    DatastoreState,
)


def _state(status: str, trail_status: str) -> DatastoreState:
    """Build a DatastoreState with only the two fields under test set.

    Inputs: status (str) - a STATUS_* constant. trail_status (str) - a
      TRAIL_STATUS_* constant.
    Output: DatastoreState.
    """
    return DatastoreState(
        status=status,
        schema_version=1,
        config_version=4,
        trail_status=trail_status,
        code_schema_version=1,
        message="",
    )


def test_a_healthy_install_reports_what_it_measured() -> None:
    """Nothing is paused, so the measured status is published as-is."""
    for measured in (TRAIL_STATUS_OK, TRAIL_STATUS_ABSENT):
        state = _state(STATUS_OK, measured)
        assert state.to_dict()["trail_status"] == measured
        assert state.to_dict()["trail_status_measured"] == measured


def test_an_intact_trail_on_a_frozen_install_reports_paused() -> None:
    """The trail is readable and will never gain another line."""
    state = _state(STATUS_DEGRADED_SCHEMA_AHEAD, TRAIL_STATUS_OK)
    block = state.to_dict()
    assert state.migrations_paused is True
    assert block["trail_status"] == TRAIL_STATUS_PAUSED
    # The measurement is still published, unchanged: paused is a verdict
    # about the install, not a claim that the file changed.
    assert block["trail_status_measured"] == TRAIL_STATUS_OK


def test_unreadable_always_wins_over_paused() -> None:
    """A corrupt trail is the more specific fact and must not be hidden."""
    state = _state(STATUS_DEGRADED_SCHEMA_AHEAD, TRAIL_STATUS_UNREADABLE)
    assert state.to_dict()["trail_status"] == TRAIL_STATUS_UNREADABLE


def test_restore_is_not_offered_while_the_trail_cannot_be_read() -> None:
    """RESTORE selects its target BY READING THE TRAIL."""
    assert _state(STATUS_OK, TRAIL_STATUS_UNREADABLE).restore_offered is False
    assert _state(STATUS_OK, TRAIL_STATUS_OK).restore_offered is True


def test_code_below_schema_reaches_the_data_block_with_both_numbers(
    tmp_path: Path,
) -> None:
    """The endpoint carries the two numbers a rollback decision needs."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    ahead = CURRENT_SCHEMA_VERSION + 3
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            set_meta(conn, "schema_version", str(ahead))

    block = ensure_db_migrated(tmp_path, 4, "0.8.2").to_dict()

    assert block["status"] == STATUS_DEGRADED_SCHEMA_AHEAD
    assert block["readonly"] is True
    assert block["healthy"] is False
    assert block["migrations_paused"] is True
    # Both numbers, in named fields, never one inferred from the other.
    assert block["schema_version"] == ahead
    assert block["code_schema_version"] == CURRENT_SCHEMA_VERSION
    assert block["schema_version_state"] == "known"
    # And in the sentence, so a person reading the banner sees them too.
    assert f"v{ahead}" in block["message"]
    assert f"v{CURRENT_SCHEMA_VERSION}" in block["message"]
    # The trail file itself is fine here; the install is what is frozen.
    assert block["trail_status"] == TRAIL_STATUS_PAUSED
    assert block["trail_status_measured"] != TRAIL_STATUS_UNREADABLE
    # No backward migration was attempted.
    assert block["detail"] is not None
    assert "Migrating backward is not attempted" in block["detail"]


def test_the_data_block_still_carries_every_field_the_design_names(
    tmp_path: Path,
) -> None:
    """schema_version, config_version, trail_status, last_migration_at."""
    block = ensure_db_migrated(tmp_path, 4, "0.8.2").to_dict()
    for field in (
        "schema_version", "config_version", "trail_status",
        "last_migration_at",
    ):
        assert field in block, field
    assert block["schema_version"] == CURRENT_SCHEMA_VERSION
    assert block["config_version"] == 4
    # A fresh bootstrap DID record a migration, so this is a timestamp and
    # not the null a never-migrated install would show.
    assert block["last_migration_at"] is not None
