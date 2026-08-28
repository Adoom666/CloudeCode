import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys, os
_add_repo_root()
from pathlib import Path
import src.core.db_steps as db_steps
from src.core.db_migration import ensure_db_migrated
WHERE = sys.argv[2]
def killer(conn):
    if WHERE == "mid":                     # after v1->v2 landed in-txn, before v2->v3
        conn.execute("SELECT COUNT(*) FROM sessions")
    os._exit(9)
if WHERE == "mid":
    db_steps.STEPS[2] = killer
else:                                       # kill during the very first step
    db_steps.STEPS[1] = killer
ensure_db_migrated(Path(sys.argv[1]), 4, "0.8.3")
print("SHOULD NOT REACH HERE")
