"""The archive status object: what the app can honestly say about itself.

THIS IS THE SURFACE, NOT THE UI. It returns a plain dict that
``src/api/corpus_routes.py`` serves and a browser can render later. It
is built as its own module so the answer is testable without a running
server, and so the rule below is enforced in one place.

THE RULE THIS MODULE EXISTS TO ENFORCE. Every block it returns carries
its own named status, and no block is allowed to render a
could-not-evaluate as a healthy zero:

  * ``freshness`` is ``current`` / ``stale`` / ``never_ran`` /
    ``cannot_determine``. A status object that cannot say "I have not
    run since X" is useless, so the age of the on-disk liveness artifact
    is the signal and its absence is a named state, never silence.
  * ``archive`` reports ``measured`` or ``cannot_determine`` with a
    reason. A datastore that will not open produces zeros in neither
    direction; it produces no counts at all and says why.
  * ``gate_findings`` distinguishes THREE things that all look like an
    empty table: the schema predates the message model, the model exists
    but holds no transcripts on this install, and the model holds
    transcripts and genuinely raised no findings. Only the third is a
    clean bill of health.
  * ``scheduler`` reports whether the loop is alive IN THIS PROCESS,
    which is a different question from whether the archive is current.
    A server restarted a minute ago has a healthy loop and may still
    have a week-old artifact. Merging the two would hide exactly that.

WHY THE ENDPOINT DOES NOT WALK THE CORPUS. Counting files on disk here
would make a status read cost a directory walk, and would invite the
reader to compare "files on disk right now" against "files ingested at
the last run" as though the difference were a defect - it is normally
just files that arrived since. Coverage is reported from the last run's
own numbers, stamped with that run's time, so a reader can see how old
the claim is instead of getting a fresh-looking number derived from two
different moments.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

from src.core import corpus_ingest_state as state_io
from src.core.corpus_ingest_service import (
    HEALTHY_STATUSES,
    MIN_ARCHIVE_SCHEMA,
    resolve_corpus_root,
)
from src.core.db import DatastoreError, connect, db_path_for, read_schema_version

#: Lowest schema carrying ``message_ingest_findings``.
MIN_MODEL_SCHEMA = 16

#: Overall verdicts. Three, never two.
OVERALL_OK = "ok"
OVERALL_ATTENTION = "attention"
OVERALL_CANNOT_DETERMINE = "cannot_determine"


def _archive_block(state_dir: Path) -> Dict[str, Any]:
    """Count what the archive currently holds, or say why it cannot.

    Description: one read-only connection, four counts, no writes. A
      datastore that cannot be opened or whose schema predates the
      archive tables returns ``cannot_determine`` with a reason - never
      a zero, because zero archived transcripts and an unopenable
      database are opposite findings that would render identically.
    Inputs: state_dir (Path).
    Output: dict carrying "status" plus counts when measured.
    Example: _archive_block(Path("/nonexistent"))["status"]
      -> 'cannot_determine'
    """
    try:
        conn = connect(db_path_for(state_dir), create=False)
    except DatastoreError as exc:
        return {"status": OVERALL_CANNOT_DETERMINE, "reason": str(exc)}
    with closing(conn):
        version = read_schema_version(conn)
        if not version.readable or version.value is None:
            return {
                "status": OVERALL_CANNOT_DETERMINE,
                "reason": "meta.schema_version is unreadable",
            }
        if version.value < MIN_ARCHIVE_SCHEMA:
            return {
                "status": OVERALL_CANNOT_DETERMINE,
                "schema_version": version.value,
                "reason": (
                    f"schema v{version.value} predates the transcript archive "
                    f"tables (v{MIN_ARCHIVE_SCHEMA}), so there is nothing to "
                    "count yet"
                ),
            }
        try:
            rows = conn.execute(
                "SELECT COUNT(*) AS archives,"
                " COUNT(DISTINCT source_path) AS sources,"
                " SUM(CASE WHEN root_state = 'unrooted' THEN 1 ELSE 0 END)"
                "   AS unrooted,"
                " MAX(ingested_at) AS newest"
                " FROM transcript_archives"
            ).fetchone()
        except sqlite3.Error as exc:
            return {
                "status": OVERALL_CANNOT_DETERMINE,
                "schema_version": version.value,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        return {
            "status": "measured",
            "schema_version": version.value,
            "archive_rows": int(rows["archives"] or 0),
            "distinct_source_paths": int(rows["sources"] or 0),
            "unrooted_archives": int(rows["unrooted"] or 0),
            "newest_ingested_at": rows["newest"],
        }


def _gate_findings_block(state_dir: Path) -> Dict[str, Any]:
    """Roll up the message model's gate findings by condition code.

    Description: read-only. Distinguishes the three ways this can be
      empty - see the module docstring. The archive ingester does not
      write to this table, so on an install where only the archive runs
      the honest answer is ``model_not_populated``, and saying "0
      findings" there would be a green light nobody earned.
    Inputs: state_dir (Path).
    Output: dict carrying "status", and "by_condition" when measured.
    Example: _gate_findings_block(Path("/nonexistent"))["status"]
      -> 'cannot_determine'
    """
    try:
        conn = connect(db_path_for(state_dir), create=False)
    except DatastoreError as exc:
        return {"status": OVERALL_CANNOT_DETERMINE, "reason": str(exc)}
    with closing(conn):
        version = read_schema_version(conn)
        if not version.readable or version.value is None:
            return {
                "status": OVERALL_CANNOT_DETERMINE,
                "reason": "meta.schema_version is unreadable",
            }
        if version.value < MIN_MODEL_SCHEMA:
            return {
                "status": "schema_too_old",
                "schema_version": version.value,
                "reason": (
                    f"schema v{version.value} predates the message model "
                    f"(v{MIN_MODEL_SCHEMA}); no findings table exists"
                ),
            }
        try:
            transcripts = int(conn.execute(
                "SELECT COUNT(*) FROM message_transcripts"
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT condition_code, severity, COUNT(*) AS n"
                " FROM message_ingest_findings"
                " GROUP BY condition_code, severity"
                " ORDER BY n DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            return {
                "status": OVERALL_CANNOT_DETERMINE,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        by_condition = [
            {
                "condition_code": str(row["condition_code"]),
                "severity": str(row["severity"]),
                "count": int(row["n"]),
            }
            for row in rows
        ]
        if transcripts == 0:
            return {
                "status": "model_not_populated",
                "message_transcripts": 0,
                "by_condition": by_condition,
                "reason": (
                    "the v16 message model holds no transcripts on this "
                    "datastore, so an empty findings list is evidence of "
                    "nothing. The background ingester maintains the "
                    "transcript ARCHIVE, not this model - see "
                    "src/core/corpus_ingest_service.py"
                ),
            }
        return {
            "status": "measured",
            "message_transcripts": transcripts,
            "by_condition": by_condition,
            "total_findings": sum(item["count"] for item in by_condition),
        }


def _overall(
    freshness: str, archive: Dict[str, Any], last_run: Optional[dict],
) -> Dict[str, str]:
    """Reduce the blocks to one verdict without inventing certainty.

    Description: ``cannot_determine`` wins over everything, because a
      verdict assembled from a measurement that was not taken is the
      defect this whole file guards against. Otherwise a stale or
      never-run archive, or a last run whose status is not in
      HEALTHY_STATUSES, is ``attention``.
    Inputs: freshness (str), archive (dict), last_run (dict | None).
    Output: dict with "verdict" and "reason".
    Example: _overall("never_ran", {}, None)["verdict"] -> 'attention'
    """
    if freshness == state_io.FRESHNESS_CANNOT_DETERMINE:
        return {
            "verdict": OVERALL_CANNOT_DETERMINE,
            "reason": "the age of the last run could not be measured",
        }
    if archive.get("status") == OVERALL_CANNOT_DETERMINE:
        return {
            "verdict": OVERALL_CANNOT_DETERMINE,
            "reason": str(archive.get("reason") or "archive not countable"),
        }
    if freshness == state_io.FRESHNESS_NEVER_RAN:
        return {
            "verdict": OVERALL_ATTENTION,
            "reason": "the ingester has never published a run here",
        }
    if freshness == state_io.FRESHNESS_STALE:
        return {
            "verdict": OVERALL_ATTENTION,
            "reason": "the last run is older than the freshness window",
        }
    status = str((last_run or {}).get("status") or "")
    if status not in HEALTHY_STATUSES:
        return {
            "verdict": OVERALL_ATTENTION,
            "reason": f"the last run finished with status {status or 'unknown'}",
        }
    if int((last_run or {}).get("could_not_read") or 0) > 0:
        return {
            "verdict": OVERALL_ATTENTION,
            "reason": "the last run could not read at least one file",
        }
    return {"verdict": OVERALL_OK, "reason": "archive is current"}


def build_status(
    state_dir: Path, *, scheduler: Any = None,
    stale_after_seconds: int = state_io.DEFAULT_STALE_AFTER_SECONDS,
) -> Dict[str, Any]:
    """Assemble the whole archive status object.

    Description: pure read. Never writes, never triggers an ingest, and
      never raises - a block that cannot be measured says so and the
      others are still returned, because a status endpoint that fails
      whole is a status endpoint nobody can use during an incident.
    Inputs: state_dir (Path), scheduler (CorpusIngestScheduler | None -
      None means the loop was never constructed in this process, which
      is reported rather than assumed to be "off"), stale_after_seconds
      (int).
    Output: dict with keys: overall, freshness, last_run, scheduler,
      archive, gate_findings, corpus.
    Example: build_status(Path("/nonexistent"))["overall"]["verdict"]
      -> 'cannot_determine'
    """
    record = state_io.read_liveness(state_dir)
    verdict, age, why = state_io.classify_freshness(
        record, stale_after_seconds=stale_after_seconds,
    )
    archive = _archive_block(state_dir)
    if scheduler is None:
        scheduler_block: Dict[str, Any] = {
            "enabled": None,
            "running": False,
            "reason": (
                "no scheduler was constructed in this process, so nothing is "
                "keeping the archive current here"
            ),
        }
    else:
        scheduler_block = scheduler.status()
    root = resolve_corpus_root()
    return {
        "overall": _overall(verdict, archive, record),
        "freshness": {
            "verdict": verdict,
            "age_seconds": None if age is None else round(age, 1),
            "stale_after_seconds": stale_after_seconds,
            "reason": why,
        },
        "last_run": record,
        "scheduler": scheduler_block,
        "archive": archive,
        "gate_findings": _gate_findings_block(state_dir),
        "corpus": {
            "root": str(root),
            "exists": root.is_dir(),
            "coverage_note": (
                "file counts come from the last run's own numbers, stamped "
                "with that run's time; this endpoint does not walk the "
                "corpus, so it never mixes two moments into one figure"
            ),
        },
        "liveness_artifact": {
            "path": str(state_io.latest_path(state_dir)),
            "dated_records_retained": len(state_io.dated_records(state_dir)),
        },
    }
