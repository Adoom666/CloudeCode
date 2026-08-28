import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys, json, sqlite3
_add_repo_root()
from verify.harness import fresh_state_dir, snap
from src.core.db_migration import ensure_db_migrated
import src.core.db_models as dbm

# ---- T4a: DB at v3, code at v2 (rollback / downgrade scenario) ----
print("=== T4a DB AHEAD OF CODE (db v3, code v2) ===")
d = fresh_state_dir("t4a"); ensure_db_migrated(d,4,"0.8.3")   # bring to v3
import src.core.db_migration as dbmig
real = dbm.CURRENT_SCHEMA_VERSION
dbm.CURRENT_SCHEMA_VERSION = 2; dbmig.CURRENT_SCHEMA_VERSION = 2
st = ensure_db_migrated(d,4,"0.8.2")
dbm.CURRENT_SCHEMA_VERSION = real; dbmig.CURRENT_SCHEMA_VERSION = real
print("  status:", st.status)
print("  schema_version reported:", st.schema_version, " code_schema_version:", st.code_schema_version)
print("  message:", st.message)
print("  NAMES BOTH NUMBERS:", "v3" in st.message and "v2" in st.message)
s=snap(d/"cloude.db"); print("  data untouched, still v3:", s["version"]=="3" and len(s["projects"])==9)

# ---- T4b: unparseable schema_version -> does the backup gate still fire? ----
print("\n=== T4b UNPARSEABLE meta.schema_version ===")
for bad in ("", "3-dirty", "v1", "1.0", " 1 ", "01", "+1", "1\n"):
    d = fresh_state_dir("t4b")
    c=sqlite3.connect(str(d/"cloude.db")); c.execute("UPDATE meta SET value=? WHERE key='schema_version'",(bad,)); c.commit(); c.close()
    st = ensure_db_migrated(d,4,"0.8.3")
    s = snap(d/"cloude.db")
    baks = list(d.glob("*.bak-*"))
    print(f"  value={bad!r:12} -> status={st.status:26} final_ver={s['version']} projects={len(s['projects'])} backups={len(baks)}")

# ---- T4c: backup unverifiable -> must abort BEFORE touching data ----
print("\n=== T4c BACKUP CANNOT BE VERIFIED ===")
import src.core.db_backup as dbk
d = fresh_state_dir("t4c")
class R: basename="fake.bak"; verified=False; reason="INJECTED: backup readback mismatch"
orig = dbk.take_backup
dbmig.take_backup = lambda *a,**k: R()
st = ensure_db_migrated(d,4,"0.8.3")
dbmig.take_backup = orig
s = snap(d/"cloude.db")
print("  status:", st.status)
print("  message:", st.message[:110])
print("  DATA UNTOUCHED AT v1:", s["version"]=="1" and len(s["projects"])==9 and "sessions" not in s["tables"])
