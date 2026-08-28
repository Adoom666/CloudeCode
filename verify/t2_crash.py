import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys, json, os, sqlite3, subprocess, shutil
_add_repo_root()
from verify.harness import fresh_state_dir, snap
from src.core.db_migration import ensure_db_migrated
import src.core.db_steps as db_steps

def show(tag, d):
    s = snap(d/"cloude.db")
    print(f"  [{tag}] version={s['version']} tables={s['tables']} projects={len(s['projects'])} integrity={s['integrity']} sessions_cols={'yes' if s['sessions_cols'] else 'no'}")
    return s

# ---------- T2a: idempotence, migrate twice ----------
print("=== T2a IDEMPOTENCE (migrate twice) ===")
d = fresh_state_dir("t2a")
st1 = ensure_db_migrated(d, 4, "0.8.3"); a=show("after 1st", d)
st2 = ensure_db_migrated(d, 4, "0.8.3"); b=show("after 2nd", d)
print("  status1=%s status2=%s" % (st1.status, st2.status))
print("  applied2=%s (should be None/empty)" % getattr(st2,'migrations_applied',None))
print("  IDENTICAL DATA:", a["projects"]==b["projects"] and a["meta"]==b["meta"])
print("  extra backup created on 2nd run:", len(list(d.glob("*.bak-*"))), "(expect 1)")
print("  trail lines:", len((d/'migration_trail.jsonl').read_text().strip().splitlines()), "(expect 4 - no new entry)")

# ---------- T2b: exception injected mid-chain (after v1->v2, during v2->v3) ----------
print("\n=== T2b EXCEPTION mid-chain (v2->v3 raises) ===")
d = fresh_state_dir("t2b")
orig = db_steps.STEPS[2]
def boom(conn):
    conn.execute("SELECT COUNT(*) FROM sessions")   # prove v1->v2 already applied in-txn
    raise RuntimeError("INJECTED mid-step failure")
db_steps.STEPS[2] = boom
st = ensure_db_migrated(d, 4, "0.8.3")
db_steps.STEPS[2] = orig
print("  status=%s schema=%s" % (st.status, st.schema_version))
print("  message:", st.message)
s=show("after failure", d)
print("  ROLLED BACK TO v1 WITH DATA:", s["version"]=="1" and len(s["projects"])==9 and s["tables"]==['meta','migration_trail','projects'])
print("  --- retry now ---")
st2 = ensure_db_migrated(d, 4, "0.8.3")
s2=show("after retry", d)
print("  RETRY SUCCEEDED:", st2.status=="ok" and s2["version"]=="3" and len(s2["projects"])==9)
for line in (d/"migration_trail.jsonl").read_text().splitlines():
    e=json.loads(line); print("   trail:", e["kind"], e["from_version"],"->",e["to_version"], e["status"], (e["error"] or "")[:40])
