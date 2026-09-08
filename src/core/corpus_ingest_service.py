"""The app-side incremental corpus ingester: one run, start to finish.

WHAT CHANGED AND WHY THIS MODULE EXISTS. Everything below this file was
already built and proven: ``transcript_corpus_discover`` walks the
corpus by structure, ``transcript_corpus_ingest.ingest_one`` applies the
``(source_path, content_sha256)`` idempotency key,
``transcript_prefix_dedupe`` stores a grown live transcript once rather
than twice, and ``transcript_corpus_ingest.root_pending_archives``
applies only the decisive rooting rules. All of it sat BESIDE the app as
a library nothing called, which meant the archive was a proof taken once
on a Tuesday rather than a property the install maintains. This module
is the layer that makes the app maintain it.

THREE THINGS IT ADDS OVER CALLING ``ingest_corpus`` DIRECTLY, and it
adds nothing else - the storage decisions are all inherited:

  1. IT IS INCREMENTAL IN THE COST SENSE, NOT ONLY THE WRITE SENSE.
     ``ingest_one`` is already idempotent, but it establishes that by
     hashing the file, and hashing 10 GB every few minutes is not a
     background task, it is a disk-saturating loop. The scan pass here
     stats each file first and consults the scan cache
     (:mod:`src.core.corpus_ingest_state`) so an unchanged file costs one
     ``stat`` and nothing else. The measured steady-state pass over the
     real 19,000-file corpus is well under a second. THE CACHE CAN ONLY
     CAUSE EXTRA WORK: a path is skipped only when size and mtime are
     both unchanged AND the hash the cache holds still matches the
     newest archive row for that path, so a database that lost the row
     re-ingests it regardless of what the cache believes.

  2. IT IS CANCELLABLE, mid-corpus, between files. The first run on a
     large corpus takes hours; a server shutdown must not wait for it,
     and the next run must resume rather than restart. It resumes for
     free, because the idempotency key is content-addressed: every file
     already written is ``already_present`` on the next pass.

  3. IT NEVER RAISES. Every outcome - a corpus directory that does not
     exist, a datastore that cannot be opened, a schema too old for the
     archive tables, an unreadable file, an unexpected sqlite error - is
     a NAMED status on the report, and the report is published to disk
     as the liveness artifact either way. See STATUS_* below: there is
     no code path that returns "ok" without having measured a run.

WHAT THIS DOES NOT DO, STATED SO THE ABSENCE IS NOT MISREAD. It does not
populate the v16 message model (``message_transcripts`` /
``message_bodies`` / ``message_appearances``). That model has no
re-ingest path for a file that GREW - ``ingest_lines`` refuses a
``source_ref`` it has already seen, deliberately, so that nothing is
ever silently overwritten - which makes it the wrong layer for a live
corpus whose transcripts grow while the app watches them. The archive
layer used here is the one built for exactly that case. The status
surface still REPORTS the message model's gate findings, read-only, and
says out loud when that model holds nothing on this datastore rather
than rendering an empty table as a clean bill of health.

HOST ATTRIBUTION IS RECORDED, WITH ITS EVIDENCE NAMED. When the schema
carries the v17 host dimension, each run interns this machine and this
corpus root via :mod:`src.core.message_host_dimension`. There is no
collection manifest for a corpus being read on the machine that owns it,
so ``manifest_sha`` is NULL and the provenance is ``declared``, not
``manifest_verified``. That distinction is the whole point of the host
dimension and is not papered over here: cross-host collection goes
through the manifest path in ``scripts/message_model_host_run.py``, and
this ingester never reaches across a host boundary.

WHERE THE REST OF IT LIVES. The decisions taken BEFORE any file is read
- the scan plan, the two database fingerprints, and the gate that lets a
steady-state run skip the rooting pass - are in
:mod:`src.core.corpus_ingest_scan`, split out purely for the 500-line
cap. On-disk state (the scan cache and the liveness artifact) is
:mod:`src.core.corpus_ingest_state`; the background loop is
:mod:`src.core.corpus_ingest_task`; the read-only status object is
:mod:`src.core.corpus_status`, served by ``src/api/corpus_routes.py``.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Dict, List, Optional

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:  # pragma: no cover - matches transcript_archive's guard
    class _NoOpLogger:
        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    logger = _NoOpLogger()

from src.core import corpus_ingest_state as state_io
from src.core.corpus_ingest_scan import (
    _current_hash,
    _db_signature,
    _resolve_db_hashes,
    _rooting_needed,
    _stat_key,
    plan_scan,
)
from src.core.db import DatastoreError, connect, db_path_for, read_schema_version
from src.core.transcript_corpus_discover import (
    default_corpus_root,
    discover_corpus_detailed,
)
from src.core.transcript_corpus_ingest import (
    ingest_one,
    root_pending_archives,
)
from src.core.transcript_prefix_dedupe import verify_stored_hash
from src.core.transcript_project_root import root_pending_archives_by_project

#: Environment override for the corpus root, so a test or a relocated
#: install does not have to monkeypatch a function. Absent means the
#: default ``~/.claude/projects``.
CORPUS_ROOT_ENV = "CLOUDE_CORPUS_ROOT"

#: Lowest schema version carrying the transcript archive tables. Below
#: this the ingester refuses rather than creating anything.
MIN_ARCHIVE_SCHEMA = 14

#: Lowest schema version carrying the v17 host dimension. Between this
#: and MIN_ARCHIVE_SCHEMA the archive still works; only attribution is
#: skipped, and the report says which.
MIN_HOST_SCHEMA = 17

#: The corpus_key this machine's own ``~/.claude/projects`` is interned
#: under, matching the key the cross-host manifest path already uses.
LOCAL_CORPUS_KEY = "claude-projects"

#: Run statuses. Every one of these is a measured outcome; there is no
#: status meaning "did not look".
STATUS_OK = "ok"
STATUS_CANCELLED = "cancelled"
STATUS_CORPUS_ABSENT = "corpus_absent"
STATUS_DATASTORE_UNAVAILABLE = "datastore_unavailable"
STATUS_SCHEMA_TOO_OLD = "schema_too_old"
STATUS_FAILED = "failed"

#: Statuses in which the archive was actually advanced or confirmed. A
#: status outside this set must never be rendered as a healthy archive.
HEALTHY_STATUSES = frozenset({STATUS_OK, STATUS_CANCELLED})


@dataclass
class CorpusIngestReport:
    """Everything one run measured, in a shape a status route can render.

    Description: counts partition the discovered files exactly -
      ``ingested + already_present + skipped_unchanged + could_not_read
      + not_reached`` equals ``discovered`` - so a file can never fall
      out of the accounting silently. ``skipped_unchanged`` is broken
      out from ``already_present`` on purpose: the first was decided
      from a stat and the cache, the second from a full content hash,
      and calling them one number would hide which evidence was used.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    status: str = STATUS_OK
    reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    wall_clock_seconds: float = 0.0
    corpus_root: str = ""
    schema_version: Optional[int] = None
    discovered: int = 0
    ingested: int = 0
    already_present: int = 0
    skipped_unchanged: int = 0
    could_not_read: int = 0
    not_reached: int = 0
    bytes_ingested: int = 0
    growth_kinds: Dict[str, int] = field(default_factory=dict)
    could_not_read_detail: List[Dict[str, str]] = field(default_factory=list)
    #: "full_scan" or "incremental" - which route _resolve_db_hashes took
    #: to establish what the archive already holds. Reported because a
    #: run that quietly fell back to the slow route is worth seeing.
    scan_mode: str = ""
    #: DISCOVERY'S OWN THIRD OUTCOME, kept separate from ``discovered``.
    #: A path the walk reached and refused to classify is not a file it
    #: found, and a directory it could not list is neither. Collapsing
    #: either into silence is what hid 442 workflow transcripts.
    discovery_unrecognised: int = 0
    discovery_unreadable: int = 0
    discovery_unrecognised_sample: List[str] = field(default_factory=list)
    discovery_unreadable_sample: List[Dict[str, str]] = field(
        default_factory=list
    )
    rooting: Dict[str, object] = field(default_factory=dict)
    project_rooting: Dict[str, object] = field(default_factory=dict)
    host_attribution: Dict[str, object] = field(default_factory=dict)
    byte_verify: Dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict:
        """Render this report as the JSON object published to disk.

        Description: the liveness artifact and the API payload are the
          SAME object, so a reader of one cannot see a field the other
          does not have.
        Inputs: none.
        Output: dict.
        Example: CorpusIngestReport().to_record()["status"] -> 'ok'
        """
        return {
            "status": self.status,
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "corpus_root": self.corpus_root,
            "schema_version": self.schema_version,
            "discovered": self.discovered,
            "ingested": self.ingested,
            "already_present": self.already_present,
            "skipped_unchanged": self.skipped_unchanged,
            "could_not_read": self.could_not_read,
            "not_reached": self.not_reached,
            "bytes_ingested": self.bytes_ingested,
            "scan_mode": self.scan_mode,
            "discovery_unrecognised": self.discovery_unrecognised,
            "discovery_unreadable": self.discovery_unreadable,
            "discovery_unrecognised_sample": list(
                self.discovery_unrecognised_sample
            ),
            "discovery_unreadable_sample": list(
                self.discovery_unreadable_sample
            ),
            "growth_kinds": dict(self.growth_kinds),
            "could_not_read_detail": list(self.could_not_read_detail[:50]),
            "rooting": dict(self.rooting),
            "project_rooting": dict(self.project_rooting),
            "host_attribution": dict(self.host_attribution),
            "byte_verify": dict(self.byte_verify),
        }


def resolve_corpus_root() -> Path:
    """Return the corpus root this install ingests from.

    Description: the environment override exists so a test, or an
      install whose transcripts live somewhere unusual, does not have to
      patch a function at import time. Existence is NOT checked here -
      a missing root is a named run status, not an exception at
      resolution time.
    Inputs: none (reads ``CLOUDE_CORPUS_ROOT``).
    Output: Path, expanded.
    Example: resolve_corpus_root().name  # 'projects'
    """
    override = os.environ.get(CORPUS_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return default_corpus_root()


def _record_host(
    conn: sqlite3.Connection, corpus_root: Path, schema_version: int,
) -> Dict[str, object]:
    """Intern this machine and this corpus root in the host dimension.

    Description: the local half of the v17 host dimension. There is no
      collection manifest for a corpus read on its own machine, so the
      corpus row carries ``manifest_sha = NULL`` and the provenance is
      reported as ``declared`` - deliberately NOT ``manifest_verified``,
      which would claim evidence that was never gathered. Never raises:
      a failure here degrades attribution, not the archive.
    Inputs: conn, corpus_root (Path), schema_version (int).
    Output: dict describing what was recorded, always carrying
      "provenance" (one of "declared", "skipped_schema", "failed").
    Example: _record_host(conn, Path("/r"), 13)["provenance"]
      -> 'skipped_schema'
    """
    if schema_version < MIN_HOST_SCHEMA:
        return {
            "provenance": "skipped_schema",
            "detail": (
                f"schema v{schema_version} predates the v{MIN_HOST_SCHEMA} "
                "host dimension, so host attribution was not recorded"
            ),
        }
    try:
        from src.core.message_host_identity import capture_identity
        from src.core.message_host_dimension import upsert_corpus, upsert_host

        identity = capture_identity()
        conn.execute("BEGIN IMMEDIATE")
        try:
            host_id = upsert_host(conn, identity)
            corpus_id = upsert_corpus(
                conn, host_id, LOCAL_CORPUS_KEY, str(corpus_root), None,
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except (sqlite3.Error, OSError, ValueError, ImportError) as exc:
        logger.warning(
            "corpus_ingest_host_attribution_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "provenance": "failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "provenance": "declared",
        "machine_id": identity.machine_id,
        "machine_id_scheme": identity.machine_id_scheme,
        "host_id": host_id,
        "corpus_id": corpus_id,
        "corpus_key": LOCAL_CORPUS_KEY,
        "detail": (
            "corpus read on the machine that owns it, so provenance is "
            "declared; manifest_sha is NULL because no collection manifest "
            "exists for a local read"
        ),
    }


def _sample_byte_verify(
    conn: sqlite3.Connection, sample: int,
) -> Dict[str, object]:
    """Reconstruct up to ``sample`` newest archives and check their hashes.

    Description: the three-outcome regression guard from
      :func:`~src.core.transcript_prefix_dedupe.verify_stored_hash`,
      applied to a bounded sample so it can run inside an ordinary
      background pass. ``sample <= 0`` returns the NAMED "not_run"
      state, never an empty pass.
    Inputs: conn, sample (int).
    Output: dict with "status" plus per-outcome counts.
    Example: _sample_byte_verify(conn, 0)["status"] -> 'not_run'
    """
    if sample <= 0:
        return {
            "status": "not_run",
            "detail": "byte verification was not requested on this run",
        }
    counts = {"hash_verified": 0, "hash_mismatch": 0, "could_not_evaluate": 0}
    mismatches: List[int] = []
    try:
        rows = conn.execute(
            "SELECT id FROM transcript_archives ORDER BY id DESC LIMIT ?",
            (int(sample),),
        ).fetchall()
    except sqlite3.Error as exc:
        return {
            "status": "could_not_run",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    for row in rows:
        result = verify_stored_hash(conn, int(row["id"]))
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        if result.outcome == "hash_mismatch":
            mismatches.append(int(row["id"]))
    return {
        "status": "ran",
        "sampled": len(rows),
        "mismatch_archive_ids": mismatches[:20],
        **counts,
    }


def run_ingest_once(
    state_dir: Path,
    *,
    corpus_root: Optional[Path] = None,
    cancel: Optional[Event] = None,
    byte_verify_sample: int = 0,
    publish: bool = True,
) -> CorpusIngestReport:
    """Perform exactly one incremental ingest pass. Never raises.

    Description: the single entry point the scheduler and the manual
      trigger both call. Order: resolve the root, open the datastore,
      check the schema, discover, plan the cheap scan, ingest what needs
      it (checking ``cancel`` between files), root what became rootable,
      record host attribution, optionally byte-verify a sample, then
      publish the liveness artifact. The artifact is published on EVERY
      terminating path including failures, because an ingester whose
      failures are silent is indistinguishable from one that is dead.
    Inputs: state_dir (Path - Settings.get_state_dir()), corpus_root
      (Path | None - defaults to :func:`resolve_corpus_root`), cancel
      (threading.Event | None - set it to stop between files),
      byte_verify_sample (int - 0 means do not verify), publish (bool -
      False suppresses the liveness write, for callers that are probing).
    Output: CorpusIngestReport.
    Example: run_ingest_once(Path("/nonexistent")).status
      -> 'datastore_unavailable'
    """
    started = time.monotonic()
    report = CorpusIngestReport(started_at=state_io.utc_now_iso())
    root = Path(corpus_root) if corpus_root is not None else resolve_corpus_root()
    report.corpus_root = str(root)
    try:
        _run_inner(state_dir, root, cancel, byte_verify_sample, report)
    except (sqlite3.Error, OSError, ValueError, DatastoreError) as exc:
        report.status = STATUS_FAILED
        report.reason = f"{type(exc).__name__}: {exc}"
        logger.warning("corpus_ingest_failed", error=report.reason)
    report.wall_clock_seconds = time.monotonic() - started
    report.finished_at = state_io.utc_now_iso()
    if publish:
        state_io.write_liveness(state_dir, report.to_record())
    logger.info(
        "corpus_ingest_run", status=report.status, discovered=report.discovered,
        ingested=report.ingested, skipped_unchanged=report.skipped_unchanged,
        already_present=report.already_present,
        could_not_read=report.could_not_read,
        seconds=round(report.wall_clock_seconds, 3),
    )
    return report


def _run_inner(
    state_dir: Path, root: Path, cancel: Optional[Event],
    byte_verify_sample: int, report: CorpusIngestReport,
) -> None:
    """Body of :func:`run_ingest_once`, split out to keep it readable.

    Description: mutates ``report`` in place. Raises only what
      :func:`run_ingest_once` catches and turns into STATUS_FAILED.
    Inputs: state_dir (Path), root (Path), cancel (Event | None),
      byte_verify_sample (int), report (CorpusIngestReport, mutated).
    Output: None.
    Example: _run_inner(Path("/s"), Path("/nope"), None, 0, report)
    """
    if not root.is_dir():
        report.status = STATUS_CORPUS_ABSENT
        report.reason = (
            f"corpus root {root} does not exist or is not a directory; "
            "nothing was ingested and nothing is claimed about coverage"
        )
        return

    db_file = db_path_for(state_dir)
    try:
        conn = connect(db_file, create=False)
    except DatastoreError as exc:
        report.status = STATUS_DATASTORE_UNAVAILABLE
        report.reason = str(exc)
        return

    with closing(conn):
        version = read_schema_version(conn)
        report.schema_version = version.value
        if not version.readable or version.value is None:
            report.status = STATUS_DATASTORE_UNAVAILABLE
            report.reason = (
                "schema_version could not be read, so the archive tables "
                "cannot be assumed present"
            )
            return
        if version.value < MIN_ARCHIVE_SCHEMA:
            report.status = STATUS_SCHEMA_TOO_OLD
            report.reason = (
                f"datastore is at schema v{version.value}; the transcript "
                f"archive needs v{MIN_ARCHIVE_SCHEMA} or later"
            )
            return

        _ingest_pass(state_dir, root, conn, cancel, report)
        report.host_attribution = _record_host(conn, root, version.value)
        report.byte_verify = _sample_byte_verify(conn, byte_verify_sample)


def _ingest_pass(
    state_dir: Path, root: Path, conn: sqlite3.Connection,
    cancel: Optional[Event], report: CorpusIngestReport,
) -> None:
    """Discover, plan, ingest and root, updating the scan cache as it goes.

    Description: the cancellation check sits between files, never inside
      one, so a cancelled run leaves no partially written archive - each
      file's write is its own transaction inside ``ingest_one``. Files
      not reached are counted as ``not_reached`` rather than folded into
      any other bucket.
    Inputs: state_dir (Path), root (Path), conn, cancel (Event | None),
      report (CorpusIngestReport, mutated).
    Output: None.
    Example: _ingest_pass(Path("/s"), Path("/r"), conn, None, report)
    """
    discovery = discover_corpus_detailed(root)
    entries = discovery.entries
    report.discovered = len(entries)
    report.discovery_unrecognised = discovery.unrecognised_count
    report.discovery_unreadable = discovery.unreadable_count
    report.discovery_unrecognised_sample = list(discovery.unrecognised_sample)
    report.discovery_unreadable_sample = list(discovery.unreadable_sample)
    cache = state_io.load_scan_cache(state_dir)
    cached_meta = state_io.load_scan_meta(state_dir)
    signature = _db_signature(conn)
    db_hashes, full_scan = _resolve_db_hashes(
        conn, cache, cached_meta, signature,
    )
    report.scan_mode = "full_scan" if full_scan else "incremental"
    todo, skipped = plan_scan(entries, cache, db_hashes)
    report.skipped_unchanged = skipped

    known = {entry.source_path for entry in entries}
    for stale in [key for key in cache if key not in known]:
        del cache[stale]

    for index, entry in enumerate(todo):
        if cancel is not None and cancel.is_set():
            report.status = STATUS_CANCELLED
            report.reason = (
                f"cancelled after {index} of {len(todo)} files needing a "
                "full pass; the next run resumes from the same point because "
                "the idempotency key is content-addressed"
            )
            report.not_reached = len(todo) - index
            break
        outcome = ingest_one(conn, entry)
        if outcome.outcome == "ingested":
            report.ingested += 1
            report.bytes_ingested += outcome.raw_byte_length or 0
            kind = outcome.growth_kind or "unknown"
            report.growth_kinds[kind] = report.growth_kinds.get(kind, 0) + 1
        elif outcome.outcome == "already_present":
            report.already_present += 1
        else:
            report.could_not_read += 1
            report.could_not_read_detail.append({
                "source_path": outcome.source_path,
                "reason": outcome.reason or "unstated",
            })
            cache.pop(entry.source_path, None)
            continue
        stat_key = _stat_key(entry)
        sha = _current_hash(conn, entry.source_path)
        if stat_key is not None and sha is not None:
            cache[entry.source_path] = (stat_key[0], stat_key[1], sha)
        else:
            cache.pop(entry.source_path, None)

    # Re-read the signature AFTER the writes, so what gets stored beside
    # the cache describes the database the cached hashes came from. The
    # rooting decision uses the PRE-write signature, because the
    # question is whether anything changed since the last pass.
    if _rooting_needed(cached_meta, signature) or report.ingested:
        report.rooting = {"status": "ran", **root_pending_archives(conn)}
        report.project_rooting = {
            "status": "ran", **root_pending_archives_by_project(conn),
        }
    else:
        # NOT ZEROS. A skipped pass renders as its own named state, so a
        # reader can never mistake "nothing changed, so the previous
        # verdict still stands" for "this run rooted nothing".
        skipped_note = {
            "status": "skipped_unchanged",
            "detail": (
                "no transcript_archives row and no sessions row changed "
                "since the last pass, so rooting can only reproduce the "
                "verdict already recorded"
            ),
        }
        report.rooting = dict(skipped_note)
        report.project_rooting = dict(skipped_note)
    state_io.save_scan_cache(state_dir, cache, _db_signature(conn))


