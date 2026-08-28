"""CLI for cloude.db: ``python -m src.core.db_cli {backup|status} [dir]``.

Exists so scripts/upgrade.sh and scripts/rollback.sh can snapshot
cloude.db through the app's OWN VACUUM INTO path instead of standing up a
second, parallel copy mechanism - which, left to itself, sooner or later
becomes a plain ``cp`` and silently drops every commit sitting in the
-wal sidecar.

EXIT CODES follow this repo's shell convention, where 3 is a distinct
COULD-NOT-EVALUATE and NOT folded into 1:

  0  the operation succeeded.
  2  usage error (unknown or missing command).
  3  could not evaluate - no database, a database that will not open, or
     a backup that was written and could not be verified. A caller can
     therefore tell "the backup is bad" from "there was nothing here",
     which matters because only one of those should stop an upgrade.
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path
from typing import List, Optional

from src.core.db import (
    DatastoreUnreadableError,
    connect,
    db_path_for,
    get_schema_version,
)
from src.core.db_backup import take_backup
from src.core.db_migration import ensure_db_migrated


def _resolve_state_dir(args: List[str]) -> Path:
    """Pick the state directory from argv, else ask the app's resolver.

    Description: the explicit argument exists for the shell scripts,
      which have already resolved the state dir themselves and must not
      get a second, possibly different answer here.
    Inputs: args (list[str]) - argv after the command word.
    Output: Path.
    """
    if args:
        return Path(args[0]).expanduser()
    from src.config import settings

    return settings.get_state_dir()


def main(argv: Optional[List[str]] = None) -> int:
    """Run one CLI command and return a process exit code.

    Inputs: argv (list[str] | None) - defaults to sys.argv[1:]. Accepted
      commands: "backup [state_dir]", "status [state_dir]".
    Output: int - see this module's docstring for the code meanings.
    Example: main(["backup", "/tmp/state"]) -> 0, printing the path.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("backup", "status"):
        print(
            "usage: python -m src.core.db_cli {backup|status} [state_dir]",
            file=sys.stderr,
        )
        return 2

    command, rest = args[0], args[1:]
    state_dir = _resolve_state_dir(rest)
    db_path = db_path_for(state_dir)

    if command == "status":
        state = ensure_db_migrated(state_dir)
        print(
            f"status={state.status} schema_version={state.schema_version} "
            f"trail_status={state.trail_status}"
        )
        return 0 if state.healthy else 3

    if not db_path.exists():
        print(
            f"CANNOT DETERMINE: {db_path} does not exist - nothing to back up",
            file=sys.stderr,
        )
        return 3
    try:
        with closing(connect(db_path, create=False)) as conn:
            version = get_schema_version(conn)
    except DatastoreUnreadableError as exc:
        print(f"CANNOT DETERMINE: {exc}", file=sys.stderr)
        return 3
    result = take_backup(db_path, state_dir, version)
    if not result.verified:
        print(f"BACKUP NOT VERIFIED: {result.reason}", file=sys.stderr)
        return 3
    print(str(result.path))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
