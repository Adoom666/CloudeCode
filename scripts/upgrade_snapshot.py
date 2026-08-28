#!/usr/bin/env python3
"""Baseline and verify a CloudeCode upgrade, with three outcomes per check.

WHY THIS EXISTS. ``scripts/upgrade.sh`` moves the code and takes a backup.
Neither of those answers the question the user actually has afterwards,
which is "did my data survive". Answering it needs a record of what the
data was BEFORE, and nothing was writing one.

THE OUTCOME MODEL, WHICH IS THE POINT OF THE FILE. Every check reports
PASS, FAIL, or CANNOT DETERMINE, and the third never collapses into
either of the others. ``src/core/db.py::get_schema_version`` documents
what that collapse already cost this project once: it folds "no version
recorded" and "a version that will not parse" onto the same ``0``, and a
populated nine-project database migrated with zero backups because a
gate read that ``0`` as a measurement.

Read-only by construction. The database is opened with
``PRAGMA query_only=ON``, which forbids every content write while still
letting a WAL reader create the ``-shm`` index it legitimately needs -
a ``mode=ro`` URI cannot promise the second half.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PASS = "pass"
FAIL = "fail"
UNKNOWN = "cannot-determine"


def _utc_stamp() -> str:
    """Current UTC time as a compact filesystem-safe stamp.

    Output: str, e.g. '20260826T191455Z'.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _install_dir() -> Path:
    """The checkout being inspected.

    Output: Path - from CLOUDE_BASELINE_INSTALL_DIR, else this file's parent.
    """
    env = os.environ.get("CLOUDE_BASELINE_INSTALL_DIR") or ""
    return Path(env) if env else Path(__file__).resolve().parents[1]


def _state_dir(install: Path) -> Tuple[Optional[Path], str]:
    """Resolve the state directory holding cloude.db.

    Description: asks the APPLICATION to resolve it rather than restating
      the precedence here, so this script cannot drift from the thing it
      is checking. Falls back to the documented macOS default only when
      the app will not import, and says which it used.
    Inputs: install (Path) - the checkout.
    Output: (Path | None, str) - the directory and how it was resolved.
    """
    override = os.environ.get("CLOUDE_BASELINE_STATE_DIR") or ""
    if override:
        return Path(override).expanduser(), "explicit --state-dir"

    # Ask the APPLICATION, so this script cannot drift from the thing it
    # is checking. Two hazards make that import less innocent than it
    # looks, and both were met while writing this:
    #
    #   1. src.config calls sys.exit() when .env is missing or partial,
    #      which raises SystemExit - NOT an Exception subclass, so a bare
    #      `except Exception` does not catch it and this read-only script
    #      would die mid-inspection.
    #   2. It prints a multi-line CONFIGURATION ERROR banner to stdout on
    #      the way out, which would land in the middle of the JSON this
    #      command emits and make the output unparseable.
    #
    # So: catch BaseException, and capture stdout for the duration.
    import contextlib
    import io

    sys.path.insert(0, str(install))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            from src.config import settings  # noqa: E402

            resolved = Path(settings.get_state_dir())
        return resolved, "resolved by src.config.settings"
    except BaseException as exc:  # noqa: BLE001 - SystemExit is the point
        why = f"{type(exc).__name__}: {exc}".strip().splitlines()[0][:120]
        default = Path.home() / "Library" / "Application Support" / "CloudeCode"
        if default.exists():
            return default, f"documented default (src.config unavailable, {why})"
        return None, f"UNRESOLVED (src.config unavailable, {why})"


def _read_schema_version(conn: sqlite3.Connection) -> Dict[str, Any]:
    """meta.schema_version as three outcomes, never as a bare number.

    Description: mirrors ``src.core.db.read_schema_version``'s contract
      deliberately, rather than calling ``get_schema_version``, whose own
      docstring forbids using it for anything that decides something.
    Inputs: conn (sqlite3.Connection).
    Output: dict with 'outcome' and, when readable, 'value'.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", ("schema_version",)
        ).fetchone()
    except sqlite3.Error as exc:
        return {"outcome": UNKNOWN, "detail": f"meta unreadable: {exc}"}
    if row is None:
        return {"outcome": UNKNOWN, "detail": "no schema_version row in meta"}
    raw = str(row[0])
    try:
        return {"outcome": PASS, "value": int(raw.strip()), "raw": raw}
    except (TypeError, ValueError):
        return {"outcome": UNKNOWN, "detail": f"unparseable schema_version: {raw!r}"}


def _row_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """A row count for every user table.

    Output: dict mapping table name to int, or an 'error' key.
    """
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        return {"error": f"could not list tables: {exc}"}
    counts: Dict[str, Any] = {}
    for name in names:
        try:
            counts[name] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
        except sqlite3.Error as exc:
            counts[name] = f"ERROR: {exc}"
    return counts


def _session_import_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """What the one-time session import did, or that it has not run.

    Description: row counts alone cannot answer the question an upgrading
      user actually asks - "did my existing sessions come across?" - and
      the reason is specific. A user coming from a version with no
      datastore has NO sessions rows in the baseline, so the count goes
      0 -> 0 and the generic check reports "no table lost rows", which is
      true and useless. Zero because there was nothing to import and zero
      because the import never ran are different facts.

      The import records both: ``meta.imported_from_json_at`` is a
      one-way latch written ONLY after a successful tmux listing, and
      ``meta.session_import_pending_reason`` names why a run bailed. This
      reads both and never infers one from the other.
    Inputs: conn (sqlite3.Connection) - read-only datastore handle.
    Output: dict with ``ran`` (bool), ``at``, ``imported``,
      ``listing_reason``, ``pending_reason``, or an ``error`` key.
    """
    try:
        meta = dict(
            conn.execute("SELECT key, value FROM meta").fetchall()
        )
    except sqlite3.Error as exc:
        return {"error": f"meta unreadable: {exc}"}
    at = meta.get("imported_from_json_at")
    detail: Dict[str, Any] = {}
    try:
        blob = json.loads(meta.get("imported_from_json_result") or "{}")
        detail = blob.get("sessions_import_detail") or {}
    except ValueError:
        detail = {}
    return {
        "ran": bool(at),
        "at": at,
        "imported": detail.get("sessions_imported"),
        "listing_reason": detail.get("listing_reason"),
        "pending_reason": meta.get("session_import_pending_reason"),
    }


def _trail_tail(conn: sqlite3.Connection, limit: int = 5) -> Any:
    """The most recent migration_trail entries, newest first."""
    try:
        rows = conn.execute(
            "SELECT kind, from_version, to_version, status, started_at "
            "FROM migration_trail ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    return [
        {
            "kind": r[0],
            "from_version": r[1],
            "to_version": r[2],
            "status": r[3],
            "started_at": r[4],
        }
        for r in rows
    ]


def _git_version(install: Path) -> str:
    """The checkout's current tag or sha, or an explicit unknown."""
    for args in (["describe", "--tags", "--exact-match"], ["rev-parse", "--short", "HEAD"]):
        try:
            out = subprocess.run(
                ["git", "-C", str(install)] + args,
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return "UNKNOWN"


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open cloude.db so it can be read and provably not written.

    Description: a normal read-write connection with ``query_only=ON``,
      NOT a ``mode=ro`` URI. query_only forbids every content write while
      still permitting the ``-shm`` shared-memory index a WAL reader
      legitimately needs; ``mode=ro`` cannot promise the second half and
      fails at the first real read rather than at connect.
    Inputs: db_path (Path).
    Output: sqlite3.Connection.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _snapshot() -> Dict[str, Any]:
    """Everything worth recording about the install right now."""
    install = _install_dir()
    state, how = _state_dir(install)
    snap: Dict[str, Any] = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "install_dir": str(install),
        "state_dir": str(state) if state else None,
        "state_dir_resolution": how,
        "version": _git_version(install),
    }
    if state is None:
        snap["database"] = {"outcome": UNKNOWN, "detail": "state dir unresolved"}
        return snap
    db_path = state / "cloude.db"
    if not db_path.exists():
        snap["database"] = {"outcome": UNKNOWN, "detail": f"no database at {db_path}"}
        return snap
    try:
        conn = _open_readonly(db_path)
    except sqlite3.Error as exc:
        snap["database"] = {"outcome": UNKNOWN, "detail": f"could not open: {exc}"}
        return snap
    try:
        snap["database"] = {
            "outcome": PASS,
            "path": str(db_path),
            "schema_version": _read_schema_version(conn),
            "row_counts": _row_counts(conn),
            "session_import": _session_import_status(conn),
            "migration_trail_tail": _trail_tail(conn),
        }
    finally:
        conn.close()
    return snap


def cmd_baseline() -> int:
    """Write a pre-upgrade baseline. Exit 2 when it is not usable."""
    snap = _snapshot()
    out_dir = Path(os.environ.get("CLOUDE_BASELINE_OUT_DIR") or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_utc_stamp()}.json"
    path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    print(json.dumps(snap, indent=2, sort_keys=True))
    print(f"\nbaseline written: {path}")
    db = snap.get("database", {})
    if db.get("outcome") != PASS:
        print(
            "\nCANNOT DETERMINE: no usable database baseline "
            f"({db.get('detail')}). Do NOT start an upgrade you will not be "
            "able to verify.",
            file=sys.stderr,
        )
        return 2
    return 0


def _newest_baseline(out_dir: Path) -> Optional[Path]:
    """The most recent baseline file, or None."""
    named = os.environ.get("CLOUDE_BASELINE_FILE") or ""
    if named:
        p = Path(named)
        return p if p.exists() else None
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("*.json"))
    return files[-1] if files else None


def _served_version(url: str) -> Dict[str, Any]:
    """Ask the running server what version it is serving."""
    try:
        with urllib.request.urlopen(f"{url}/api/v1/health", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"outcome": PASS, "version": body.get("version"), "body": body}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"outcome": UNKNOWN, "detail": f"server not answering at {url}: {exc}"}


def _trail_verdict(status: str, install: Path) -> str:
    """Map a migration_trail status onto one of the three outcomes.

    Description: reads the vocabulary out of ``src.core.db_models`` rather
      than restating it here, so this cannot drift from the writer. That
      matters: the first version of this function GUESSED the accepted
      set as ("ok", "success", "complete") and reported a perfectly
      healthy ``completed`` entry as a FAIL - a false alarm manufactured
      inside the verification step, about an upgrade that was fine.
      ``db_models`` imports without ``.env``, unlike ``src.config``.
    Inputs: status (str) - lowercased status from the trail row.
      install (Path) - the checkout, for sys.path.
    Output: str - PASS, FAIL or UNKNOWN.
    """
    sys.path.insert(0, str(install))
    try:
        from src.core.db_models import (  # noqa: E402
            TRAIL_STATUS_COMPLETED,
            TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT,
            TRAIL_STATUS_FAILED,
            TRAIL_STATUS_INTERRUPTED,
            TRAIL_STATUS_STARTED,
        )
    except BaseException:  # noqa: BLE001
        # Cannot read the vocabulary, so cannot classify against it. Say
        # so rather than falling back to a guess - the guess is the bug
        # this function's docstring describes.
        return UNKNOWN
    if status in (TRAIL_STATUS_COMPLETED, TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT):
        return PASS
    if status in (TRAIL_STATUS_FAILED, TRAIL_STATUS_INTERRUPTED):
        return FAIL
    if status == TRAIL_STATUS_STARTED:
        # Announced and not yet closed. In flight, or the process died
        # between the two writes. Either way we cannot call it.
        return UNKNOWN
    return UNKNOWN


def _emit(results: List[Tuple[str, str, str]]) -> int:
    """Print one line per check and derive the exit code.

    Inputs: results - (check name, outcome, detail) triples.
    Output: int - 0 all pass, 1 any fail, 2 any cannot-determine.
    """
    width = max(len(name) for name, _, _ in results)
    for name, outcome, detail in results:
        print(f"{name.ljust(width)}  {outcome.upper():<17} {detail}")
    if any(o == FAIL for _, o, _ in results):
        return 1
    if any(o == UNKNOWN for _, o, _ in results):
        return 2
    return 0


def cmd_verify() -> int:
    """Compare live state against the newest baseline."""
    install = _install_dir()
    out_dir = Path(os.environ.get("CLOUDE_BASELINE_OUT_DIR") or ".")
    base_path = _newest_baseline(out_dir)
    results: List[Tuple[str, str, str]] = []

    if base_path is None:
        print(
            "CANNOT DETERMINE: no baseline found. Nothing to compare against, "
            "so this upgrade cannot be verified. Take a baseline BEFORE the "
            "next one.",
            file=sys.stderr,
        )
        return 2

    before = json.loads(base_path.read_text())
    now = _snapshot()
    print(f"baseline: {base_path}\n")

    results.append((
        "version",
        PASS if now["version"] != "UNKNOWN" else UNKNOWN,
        f"{before.get('version')} -> {now.get('version')}",
    ))

    db_before = before.get("database", {})
    db_now = now.get("database", {})
    if db_before.get("outcome") != PASS or db_now.get("outcome") != PASS:
        results.append((
            "database", UNKNOWN,
            f"before={db_before.get('outcome')} now={db_now.get('outcome')} "
            f"({db_now.get('detail') or db_before.get('detail')})",
        ))
        return _emit(results)

    sv_b, sv_n = db_before["schema_version"], db_now["schema_version"]
    if sv_b.get("outcome") != PASS or sv_n.get("outcome") != PASS:
        results.append((
            "schema version", UNKNOWN,
            f"before={sv_b.get('outcome')} now={sv_n.get('outcome')}: "
            f"{sv_n.get('detail') or sv_b.get('detail')}",
        ))
    elif sv_n["value"] < sv_b["value"]:
        results.append((
            "schema version", FAIL,
            f"WENT BACKWARDS: {sv_b['value']} -> {sv_n['value']}",
        ))
    else:
        moved = "no migration on this path" if sv_n["value"] == sv_b["value"] else "migrated"
        results.append((
            "schema version", PASS,
            f"{sv_b['value']} -> {sv_n['value']} ({moved})",
        ))

    rc_b, rc_n = db_before["row_counts"], db_now["row_counts"]
    if "error" in rc_b or "error" in rc_n:
        results.append(("row counts", UNKNOWN, "table list unreadable on one side"))
    else:
        lost, missing, changed = [], [], []
        for table, count in rc_b.items():
            if table not in rc_n:
                missing.append(table)
                continue
            if isinstance(count, int) and isinstance(rc_n[table], int):
                if rc_n[table] < count:
                    lost.append(f"{table} {count}->{rc_n[table]}")
                elif rc_n[table] != count:
                    changed.append(f"{table} {count}->{rc_n[table]}")
            else:
                missing.append(table)
        if lost:
            results.append(("row counts", FAIL, "ROWS LOST: " + ", ".join(lost)))
        elif missing:
            results.append((
                "row counts", UNKNOWN,
                "table absent or uncountable on one side: " + ", ".join(missing),
            ))
        else:
            detail = "no table lost rows"
            if changed:
                detail += "; grew: " + ", ".join(changed)
            results.append(("row counts", PASS, detail))

    # SESSION IMPORT - the question an upgrading user actually asks.
    #
    # Separate from row counts on purpose. A user coming from a version
    # with no datastore has no sessions rows in the baseline, so the count
    # goes 0 -> 0 and the generic check says "no table lost rows" - true,
    # and useless. This distinguishes "nothing to import" from "the import
    # never ran", which look identical in a count.
    imp = db_now.get("session_import")
    if not isinstance(imp, dict) or "error" in (imp or {}):
        results.append((
            "session import", UNKNOWN,
            (imp or {}).get("error", "not recorded in this snapshot - "
                            "the baseline predates this check"),
        ))
    elif imp.get("pending_reason"):
        # The latch is deliberately NOT set when the tmux probe fails, so
        # the next start retries. That is a real state, not a failure, and
        # it must not read as success.
        results.append((
            "session import", UNKNOWN,
            f"has not run yet: {imp['pending_reason']}. It retries on the "
            "next start; existing sessions are NOT imported until it does",
        ))
    elif not imp.get("ran"):
        results.append((
            "session import", UNKNOWN,
            "no import record at all - either this install predates the "
            "import or it has never completed a start with a readable "
            "datastore",
        ))
    else:
        n = imp.get("imported")
        reason = imp.get("listing_reason")
        if reason == "no_server":
            # ok=True with a real answer of zero: tmux was not running, so
            # there was genuinely nothing to import. Named rather than
            # counted, because "0 imported" alone reads like a loss.
            results.append((
                "session import", PASS,
                "ran; imported 0 because no tmux server was running - a "
                "real answer of zero, not a failed probe",
            ))
        else:
            results.append((
                "session import", PASS,
                f"ran at {imp.get('at')}; imported {n} session(s)"
                + (f" (listing: {reason})" if reason else ""),
            ))

    tail = db_now.get("migration_trail_tail")
    if isinstance(tail, dict) or not tail:
        results.append(("migration trail", UNKNOWN, "no trail entries readable"))
    else:
        newest = tail[0]
        status = str(newest.get("status", "")).lower()
        desc = (
            f"kind={newest.get('kind')} from={newest.get('from_version')} "
            f"to={newest.get('to_version')} status={newest.get('status')}"
        )
        results.append(("migration trail", _trail_verdict(status, install), desc))

    served = _served_version(os.environ.get("CLOUDE_BASELINE_URL") or "http://127.0.0.1:8000")
    if served["outcome"] != PASS:
        results.append(("served version", UNKNOWN, served["detail"]))
    else:
        sv = served.get("version")
        on_disk = now.get("version", "").lstrip("v")
        results.append((
            "served version",
            PASS if sv and on_disk and str(sv).lstrip("v") == on_disk else UNKNOWN,
            f"server reports {sv!r}, checkout is {now.get('version')!r}",
        ))

    return _emit(results)


def main(argv: List[str]) -> int:
    """Dispatch on the single positional command."""
    if len(argv) != 2 or argv[1] not in ("baseline", "verify"):
        print("usage: upgrade_snapshot.py {baseline|verify}", file=sys.stderr)
        return 64
    return cmd_baseline() if argv[1] == "baseline" else cmd_verify()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
