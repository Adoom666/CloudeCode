import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys, json; _add_repo_root()
from verify.harness import fresh_state_dir, snap
from src.core.db_migration import ensure_db_migrated

d = fresh_state_dir("t1_real")
before = snap(d / "cloude.db")
print("BEFORE: version=%s tables=%s projects=%d integrity=%s" % (
    before["version"], before["tables"], len(before["projects"]), before["integrity"]))

st = ensure_db_migrated(d, config_version=4, app_version="0.8.3")
print("STATE: status=%s schema=%s applied=%s backup=%s trail=%s" % (
    st.status, st.schema_version, getattr(st,'migrations_applied',None),
    st.backup_path, st.trail_status))
print("MESSAGE:", st.message)

after = snap(d / "cloude.db")
print("AFTER : version=%s tables=%s projects=%d integrity=%s" % (
    after["version"], after["tables"], len(after["projects"]), after["integrity"]))
print("sessions cols:", after["sessions_cols"])
print("PROJECTS PRESERVED EXACTLY:", before["projects"] == after["projects"])
print("META PRESERVED (minus version):",
      {k:v for k,v in before["meta"].items() if k!="schema_version"} ==
      {k:v for k,v in after["meta"].items() if k!="schema_version"})
print("--- backups on disk ---")
for p in sorted(d.glob("*")): print("  ", p.name, p.stat().st_size)
print("--- trail ---")
for line in (d/"migration_trail.jsonl").read_text().splitlines():
    e=json.loads(line); print("  ", e["kind"], e["from_version"],"->",e["to_version"], e["status"], "backup_verified=",e["backup_verified"])
