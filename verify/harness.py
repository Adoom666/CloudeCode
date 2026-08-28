"""Shared harness: build a state dir from the PRISTINE copy of the live DB."""
import shutil, sqlite3, sys, os
from pathlib import Path

DATA = Path("/Users/jsugamele/Scratch/llmScratch/cc-s4-fixes2-data")
PRISTINE_DB = DATA / "cloude-live-PRISTINE.db"
PRISTINE_TRAIL = DATA / "migration_trail-PRISTINE.jsonl"

def fresh_state_dir(name):
    d = DATA / "runs" / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    shutil.copy2(PRISTINE_DB, d / "cloude.db")
    shutil.copy2(PRISTINE_TRAIL, d / "migration_trail.jsonl")
    return d

def snap(db):
    """Return a comparable fingerprint of user data."""
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
    out = {}
    out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
    out["tables"] = sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
    out["version"] = c.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    out["version"] = out["version"][0] if out["version"] else None
    out["projects"] = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id")]
    out["meta"] = {r[0]: r[1] for r in c.execute("SELECT key,value FROM meta")}
    try:
        out["sessions_cols"] = [r[1] for r in c.execute("PRAGMA table_info(sessions)")]
        out["sessions_n"] = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    except sqlite3.Error:
        out["sessions_cols"] = None; out["sessions_n"] = None
    c.close()
    return out
