"""D4: does any production path write an instance row with NULL tmux_session_id?
D6: can the sessions import be latched shut without going through _latch_sessions_stage?
"""
import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import json
import sqlite3
import sys

_add_repo_root()
from verify.harness import fresh_state_dir
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.session_import import run_first_run_import, sessions_stage_done
from src.core.tmux_listing import TmuxListing

print("=== D4: NULL tmux_session_id via the step-5 (persisted, not live) path ===")
d = fresh_state_dir("t7")
ensure_db_migrated(d, 4, "0.8.3")

# Exactly the shape of the live mini's session_metadata.json: sessions the
# app knew about that are NOT currently live in tmux.
(d / "session_metadata.json").write_text(json.dumps({
    "cloude_work": {"session_id": "abc", "created_at": "2026-08-01T00:00:00Z"},
    "cloude_other": {"session_id": "def", "tmux_created_epoch": 1755000000},
}))

listing = TmuxListing.answered([], reason=None, detail="no live sessions")
conn = connect(db_path_for(d))
try:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        res = run_first_run_import(
            conn, projects=[], listing=listing,
            owned_tmux_names={"cloude_work"},
            persisted_sessions=[
                {"tmux_session": "cloude_work", "session_id": "abc"},
                {"tmux_session": "cloude_other", "session_id": "def",
                 "tmux_created_epoch": 1755000000},
            ],
            socket="cloude")
        conn.execute("COMMIT")
    print("  import outcome:", getattr(res, "outcome", res))
    rows = conn.execute(
        "SELECT tmux_name, tmux_created_epoch, origin, lifecycle, tmux_session_id "
        "FROM sessions ORDER BY tmux_name").fetchall()
    for r in rows:
        null = "  <<< NULL tmux_session_id" if r[4] is None else ""
        print(f"    name={r[0]!r} epoch={r[1]} origin={r[2]} lifecycle={r[3]} "
              f"tmux_session_id={r[4]!r}{null}")
    n_null = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE tmux_session_id IS NULL").fetchone()[0]
    print(f"  ROWS WITH NULL tmux_session_id: {n_null}")
    print("  D4 CLAIM ('production can never reach record_instance without one'):",
          "REFUTED" if n_null else "held")

    print("\n=== D3: do those rows feed the negative tier? ===")
    from src.core.session_store import owned_instances
    from src.core.tmux_listing_parse import resolve_ownership
    oi = owned_instances(conn, socket="cloude")
    print("  owned_instances:", oi)
    # the user later runs a REAL cloude_work that the app did not record
    print("  live cloude_work @ epoch 1787000000 -> owned =",
          resolve_ownership("cloude_work", 1787000000, oi, None, prefix="cloude_"))
    print("  (nothing in src/ ever DELETEs a sessions row, so this is permanent)")
finally:
    conn.close()

print("\n=== D6: latch the import shut WITHOUT _latch_sessions_stage ===")
d2 = fresh_state_dir("t7b")
ensure_db_migrated(d2, 4, "0.8.3")
(d2 / "session_metadata.json").write_text(json.dumps({"cloude_a": {"session_id": "z"}}))
conn = connect(db_path_for(d2))
try:
    # Write the TOP-LEVEL projects-era latch key directly. Does the sessions
    # stage still run, or does it read as already-done?
    conn.execute("INSERT INTO meta (key,value) VALUES ('imported_from_json_at','2026-01-01T00:00:00Z') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.execute("INSERT INTO meta (key,value) VALUES ('imported_from_json_result','{}') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.execute("BEGIN IMMEDIATE")
    res = run_first_run_import(
        conn, projects=[], listing=TmuxListing.answered([], reason=None, detail=""),
        owned_tmux_names=set(),
        persisted_sessions=[{"tmux_session": "cloude_a", "session_id": "z"}],
        socket="cloude")
    conn.execute("COMMIT")
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"  with top-level latch keys pre-set: outcome={getattr(res,'outcome',res)} sessions_imported={n}")
    print("  (expected: sessions stage still runs -> the stage key is separate)")
finally:
    conn.close()
