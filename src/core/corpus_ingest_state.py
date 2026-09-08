"""On-disk state for the app's corpus ingester: scan cache and liveness.

TWO ARTIFACTS, TWO PURPOSES, DELIBERATELY SEPARATE FILES.

1. THE SCAN CACHE is a pure PERFORMANCE artifact and is allowed to be
   wrong in exactly one direction. It records, per corpus-relative
   source_path, the ``(size, mtime_ns, content_sha256)`` last observed.
   The ingester uses it to skip re-hashing a file whose size AND mtime
   are both unchanged AND whose cached hash still matches what the
   database holds for that path. Deleting it costs one slow run and
   nothing else; a corrupt or unreadable one is treated as absent. It is
   NOT in the database on purpose: it is a cache, it carries no history,
   and putting it in cloude.db would mean a schema migration to add a
   table whose entire content can be recomputed from the filesystem.

   WHY THE DATABASE HASH IS PART OF THE SKIP CONDITION. An mtime+size
   match alone would say "this file has not changed since we looked",
   which is not the question. The question is "is what is on disk
   already IN the archive". If the cache says the file is unchanged but
   the newest archive row for that path carries a different hash (a
   half-finished run, a restored database, a deleted row), the file is
   re-hashed and re-ingested rather than skipped. The cache can therefore
   only ever cause extra work, never a missed file.

2. THE LIVENESS ARTIFACT is the ingester's own dead-man's switch, and
   its AGE is the signal, not the absence of errors. An ingester that
   died looks exactly like one that keeps finding nothing new: both
   produce no output and no alert. So every run - including a run that
   ingested nothing, and including a run that FAILED - stamps
   ``latest.json`` with a UTC timestamp, and the status surface reports
   the age of that stamp. A missing artifact is NEVER "fine, nothing to
   report"; it is ``never_ran``, a named third outcome.

THE THREE-OUTCOME RULE APPLIES TO FRESHNESS ITSELF.
:func:`classify_freshness` returns one of four states and never
collapses them: ``current``, ``stale``, ``never_ran`` (no artifact on
disk at all) and ``cannot_determine`` (an artifact exists but its
timestamp cannot be parsed, or the clock produced a negative age). A
freshness verdict of ``current`` is only ever returned when an age was
actually measured.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.core.json_artifact import atomic_write_json, read_json_object

#: Subdirectory of the state dir holding every artifact this module writes.
INGEST_DIRNAME = "corpus-ingest"

#: The dead-man's-switch file. Its mtime is not trusted; the timestamp
#: INSIDE it is, because a file copy changes one and not the other.
LATEST_FILENAME = "latest.json"

#: The performance cache. See the module docstring for why it is not a table.
SCAN_CACHE_FILENAME = "scan-cache.json"

#: How many dated per-run records to retain beside ``latest.json``. Dated
#: records exist so a human can see the shape of the last month of runs;
#: they are capped because this is a cache directory, not a log store.
DATED_RETENTION = 30

#: Freshness verdicts. Four, never two - see the module docstring.
FRESHNESS_CURRENT = "current"
FRESHNESS_STALE = "stale"
FRESHNESS_NEVER_RAN = "never_ran"
FRESHNESS_CANNOT_DETERMINE = "cannot_determine"

#: Default age at which the archive stops being CURRENT. The ingester's
#: own interval is far shorter (see corpus_ingest_task.DEFAULT_INTERVAL_
#: SECONDS), so this threshold is set from the PHASE between producer and
#: checker rather than from the period: it must be comfortably longer
#: than one interval so an ordinary long run does not read as stale, and
#: comfortably shorter than a day so a single dead scheduler is caught on
#: the same day it dies rather than on the second miss.
DEFAULT_STALE_AFTER_SECONDS = 6 * 3600


def artifact_dir(state_dir: Path) -> Path:
    """Return the directory this module's artifacts live in.

    Description: one place that knows the layout, so the service, the
      scheduler, the API route and the tests cannot disagree about where
      liveness is published.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
    Output: Path - ``<state_dir>/corpus-ingest`` (not created here).
    Example: artifact_dir(Path("/s"))  # Path('/s/corpus-ingest')
    """
    return Path(state_dir) / INGEST_DIRNAME


def latest_path(state_dir: Path) -> Path:
    """Return the path of the liveness artifact.

    Inputs: state_dir (Path).
    Output: Path.
    Example: latest_path(Path("/s")).name -> 'latest.json'
    """
    return artifact_dir(state_dir) / LATEST_FILENAME


def scan_cache_path(state_dir: Path) -> Path:
    """Return the path of the scan cache.

    Inputs: state_dir (Path).
    Output: Path.
    Example: scan_cache_path(Path("/s")).name -> 'scan-cache.json'
    """
    return artifact_dir(state_dir) / SCAN_CACHE_FILENAME


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix.

    Description: one formatter, so a timestamp written by the service
      and a timestamp parsed by the status route cannot drift.
    Inputs: none.
    Output: str, e.g. ``2026-08-30T12:00:00Z``.
    Example: utc_now_iso().endswith("Z") -> True
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: dict) -> bool:
    """Write a JSON payload atomically under this module's log event name.

    Description: a thin binding of :func:`src.core.json_artifact.
      atomic_write_json` to the ``corpus_ingest_artifact_write_failed``
      event, so the ingester keeps its own name in the log stream while
      the write itself has exactly one implementation - see that module
      for why a half-written artifact is worse than a missing one.
    Inputs: path (Path), payload (dict - must be JSON-serialisable).
    Output: bool - True when the bytes are on disk under ``path``.
    Example: _atomic_write_json(Path("/nonexistent/x.json"), {}) -> False
    """
    return atomic_write_json(
        path, payload, log_event="corpus_ingest_artifact_write_failed",
    )


def _read_json(path: Path) -> Optional[dict]:
    """Read a JSON object from disk, returning None on any failure.

    Description: a thin binding of :func:`src.core.json_artifact.
      read_json_object`. An unreadable or non-object artifact is treated
      exactly like an absent one by every caller, because both mean
      "this file cannot tell me anything" - which the caller then reports
      as a named third outcome rather than as a healthy zero.
    Inputs: path (Path).
    Output: dict | None.
    Example: _read_json(Path("/nonexistent.json")) -> None
    """
    return read_json_object(
        path, log_event="corpus_ingest_artifact_unreadable",
    )


def load_scan_cache(state_dir: Path) -> Dict[str, Tuple[int, int, str]]:
    """Load the scan cache, treating any failure as an empty cache.

    Description: see the module docstring - the cache may only ever
      cause EXTRA work, so an empty result is always safe and never
      needs to be reported as a failure.
    Inputs: state_dir (Path).
    Output: dict mapping source_path -> (size, mtime_ns, content_sha256).
      Malformed individual entries are dropped, not repaired.
    Example: load_scan_cache(Path("/nonexistent")) -> {}
    """
    data = _read_json(scan_cache_path(state_dir))
    if not data:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    out: Dict[str, Tuple[int, int, str]] = {}
    for source_path, value in entries.items():
        if (isinstance(value, list) and len(value) == 3
                and isinstance(value[0], int) and isinstance(value[1], int)
                and isinstance(value[2], str)):
            out[str(source_path)] = (value[0], value[1], value[2])
    return out


def load_scan_meta(state_dir: Path) -> Dict[str, object]:
    """Load the database fingerprint stored alongside the scan cache.

    Description: the fingerprint is what lets the next run decide
      whether the cached hashes still describe the SAME database - see
      ``corpus_ingest_service._resolve_db_hashes``. An absent or
      malformed fingerprint means "cannot tell", and the caller's
      response to that is a full scan, never a skip.
    Inputs: state_dir (Path).
    Output: dict - empty when nothing usable is on disk.
    Example: load_scan_meta(Path("/nonexistent")) -> {}
    """
    data = _read_json(scan_cache_path(state_dir))
    meta = (data or {}).get("db")
    return meta if isinstance(meta, dict) else {}


def save_scan_cache(
    state_dir: Path, cache: Dict[str, Tuple[int, int, str]],
    db_meta: Optional[Dict[str, object]] = None,
) -> bool:
    """Persist the scan cache and its database fingerprint atomically.

    Inputs: state_dir (Path), cache (dict as returned by
      :func:`load_scan_cache`), db_meta (dict or None - the fingerprint
      of the database the cached hashes were read from).
    Output: bool - False when the write failed, which is survivable.
    Example: save_scan_cache(Path("/nonexistent/x"), {}) -> False
    """
    payload = {
        "written_at": utc_now_iso(),
        "db": dict(db_meta or {}),
        "entries": {k: list(v) for k, v in cache.items()},
    }
    return _atomic_write_json(scan_cache_path(state_dir), payload)


def write_liveness(state_dir: Path, record: dict) -> bool:
    """Publish one run record as the liveness artifact, plus a dated copy.

    Description: ``latest.json`` is what the status surface reads; the
      dated copy exists so a human can see the last month of runs
      without the artifact having to accumulate history inside itself.
      A failure to write the dated copy does NOT fail the call - the
      dead-man's switch is ``latest.json`` and only that.
    Inputs: state_dir (Path), record (dict - must carry
      ``finished_at``; the caller builds it).
    Output: bool - True when ``latest.json`` was replaced.
    Example: write_liveness(Path("/nonexistent"), {}) -> False
    """
    ok = _atomic_write_json(latest_path(state_dir), record)
    stamp = str(record.get("finished_at") or utc_now_iso())
    safe = stamp.replace(":", "").replace("-", "")
    _atomic_write_json(artifact_dir(state_dir) / f"run-{safe}.json", record)
    _prune_dated(state_dir)
    return ok


def _prune_dated(state_dir: Path) -> None:
    """Keep at most DATED_RETENTION dated run records, newest first.

    Description: never raises; a directory that cannot be listed is left
      exactly as it is, since the retention of a cache directory is not
      worth a failure path of its own.
    Inputs: state_dir (Path).
    Output: None.
    Example: _prune_dated(Path("/nonexistent"))  # returns None
    """
    try:
        dated = sorted(artifact_dir(state_dir).glob("run-*.json"))
    except OSError:
        return
    for path in dated[:-DATED_RETENTION] if len(dated) > DATED_RETENTION else []:
        try:
            path.unlink()
        except OSError:
            pass


def read_liveness(state_dir: Path) -> Optional[dict]:
    """Read the liveness artifact.

    Inputs: state_dir (Path).
    Output: dict | None - None means "no artifact on disk or unreadable",
      which the caller must render as ``never_ran``, never as healthy.
    Example: read_liveness(Path("/nonexistent")) -> None
    """
    return _read_json(latest_path(state_dir))


def _parse_stamp(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 stamp written by :func:`utc_now_iso`.

    Inputs: value (object - expected str).
    Output: datetime (tz-aware, UTC) or None when unparseable.
    Example: _parse_stamp("2026-01-01T00:00:00Z").year -> 2026
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def classify_freshness(
    record: Optional[dict], *, now: Optional[datetime] = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> Tuple[str, Optional[float], str]:
    """Decide whether the archive is current, stale, or unevaluable.

    Description: the four-outcome verdict described in the module
      docstring. ``current`` is returned ONLY when an age was actually
      measured and is within the threshold; a record whose timestamp
      cannot be parsed is ``cannot_determine``, never ``current`` and
      never ``stale``, because both of those assert a measurement that
      was not taken. A NEGATIVE age (a run stamped in the future, which
      means a clock moved) is also ``cannot_determine``: the age is
      real arithmetic on an unreal input.
    Inputs: record (dict | None as returned by :func:`read_liveness`),
      now (datetime | None - defaults to current UTC),
      stale_after_seconds (int).
    Output: (verdict str, age_seconds float | None, human reason str).
    Example: classify_freshness(None)[0] -> 'never_ran'
    """
    if record is None:
        return (
            FRESHNESS_NEVER_RAN, None,
            "no liveness artifact on disk: the ingester has not completed a "
            "run in this state directory, or the artifact is unreadable",
        )
    stamp = _parse_stamp(record.get("finished_at"))
    if stamp is None:
        return (
            FRESHNESS_CANNOT_DETERMINE, None,
            "liveness artifact carries no parseable finished_at, so its age "
            "cannot be measured",
        )
    reference = now or datetime.now(timezone.utc)
    age = (reference - stamp).total_seconds()
    if age < 0:
        return (
            FRESHNESS_CANNOT_DETERMINE, age,
            f"liveness artifact is stamped {abs(age):.0f}s in the future, so "
            "its age is not a measurement of anything",
        )
    if age <= stale_after_seconds:
        return (
            FRESHNESS_CURRENT, age,
            f"last run completed {age:.0f}s ago, within the "
            f"{stale_after_seconds}s freshness window",
        )
    return (
        FRESHNESS_STALE, age,
        f"last run completed {age:.0f}s ago, past the "
        f"{stale_after_seconds}s freshness window",
    )


def dated_records(state_dir: Path) -> List[str]:
    """List the dated run records currently retained, oldest first.

    Description: a read for the status surface and for tests; never
      raises, an unlistable directory is an empty list.
    Inputs: state_dir (Path).
    Output: list[str] - file names, not paths.
    Example: dated_records(Path("/nonexistent")) -> []
    """
    try:
        return sorted(p.name for p in artifact_dir(state_dir).glob("run-*.json"))
    except OSError:
        return []
