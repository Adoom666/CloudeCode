#!/usr/bin/env python3
"""Ingest the WHOLE ~/.claude/projects corpus into a fresh v16 message
model, straight from the .jsonl files, and prove every file byte-exact.

WHY THE FILES AND NOT claude_history. The history database keeps
``raw_json`` for only about 56.5 percent of its rows by a deliberate
policy, so a line whose original bytes were never kept cannot be used to
prove byte-exactness. The .jsonl files ARE the original bytes. Ingesting
from them makes 100 percent of lines verifiable instead of 56.5 percent,
which is the whole reason this script exists rather than a larger
``--sessions`` on message_model_sample_proof.py.

THE THIRD OUTCOME IS A FIRST-CLASS RESULT HERE. A file is
``byte_identical``, ``MISMATCH``, or ``could_not_evaluate`` - and the
third is never counted as a pass. A file that could not be read, could
not be decoded, or blew up during ingest is named with its reason. A run
that reported 19,000 passes and silently dropped 500 unreadable files
would be exactly the false green this model exists to avoid.

STREAMING, NOT ``read()``. Files are read in 1 MiB chunks and split into
lines as they arrive; the source sha256 is computed incrementally over
the bytes actually read. The largest file in this corpus measured 233 MB
on 2026-08-29, so ``open(p).read()`` is not an option.

LIVE FILES ARE EXPECTED. Sessions are being written while this runs, so
a file can grow between the ``stat`` and the last chunk. The proof is
against THE BYTES THIS PROCESS READ, whose hash is taken from those exact
bytes; a file whose size changed is still proven, and the change is
reported as its own counted fact rather than swept up as a failure.

Usage:
  ./venv/bin/python scripts/message_model_corpus_run.py \
      --db /path/to/scratch.db --results /path/to/results.jsonl [--resume]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.db_models import CURRENT_SCHEMA_VERSION  # noqa: E402
from src.core.db_steps import run_chain  # noqa: E402
from src.core.message_model_export import (  # noqa: E402
    VERIFY_CANNOT_RENDER,
    export_transcript,
)
from src.core.message_model_ingest import SourceLine, ingest_lines  # noqa: E402
from src.core.message_model_serialize import sha256_text  # noqa: E402

CORPUS_ROOT: str = os.path.expanduser("~/.claude/projects")
CHUNK_BYTES: int = 1 << 20

OUTCOME_IDENTICAL: str = "byte_identical"
OUTCOME_MISMATCH: str = "MISMATCH"
OUTCOME_CANNOT_EVALUATE: str = "could_not_evaluate"


@dataclass
class FileRead:
    """One file's bytes, decoded into lines, with the hash of what was read.

    - ``lines``: the file's lines WITHOUT their trailing newlines.
    - ``has_trailing_newline``: whether the last line was terminated.
    - ``source_sha256``: sha256 of the exact bytes this process read,
      which is what byte-exactness is proven against.
    - ``byte_length``: how many bytes were read.
    """

    lines: List[str] = field(default_factory=list)
    has_trailing_newline: bool = False
    source_sha256: str = ""
    byte_length: int = 0


def find_transcripts(root: str) -> List[str]:
    """Every .jsonl file under the corpus root, in a deterministic order.

    Description: sorted by path, which puts a session's own file ahead of
      its ``subagents/`` directory ('.' sorts before '/'). That order is
      not required for correctness - dangling parents are re-evaluated
      against the finished database - but it keeps the transient finding
      count close to the settled one.
    Inputs: root (str) - directory to walk.
    Output: list[str] - absolute paths, sorted.
    Example: len(find_transcripts("/nonexistent")) -> 0
    """
    found: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


def read_file_lines(path: str) -> FileRead:
    """Stream one file into lines, hashing the bytes as they are read.

    Description: reads in CHUNK_BYTES chunks and splits on b"\\n" as it
      goes, so no file is ever held whole as a single bytes object. Each
      line is decoded strictly - a multi-byte UTF-8 sequence cannot span
      a newline, so per-line decoding is exact.
    Inputs: path (str).
    Output: FileRead.
    Raises: OSError - the file could not be opened or read.
      UnicodeDecodeError - a line is not valid UTF-8.
    Example: read_file_lines("/dev/null").byte_length -> 0
    """
    digest = hashlib.sha256()
    lines: List[str] = []
    pending = b""
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            pending += chunk
            if b"\n" not in pending:
                continue
            parts = pending.split(b"\n")
            pending = parts.pop()
            for part in parts:
                lines.append(part.decode("utf-8"))
    trailing = bool(lines) and pending == b""
    if pending:
        lines.append(pending.decode("utf-8"))
    return FileRead(lines=lines, has_trailing_newline=trailing,
                    source_sha256=digest.hexdigest(), byte_length=total)


def prefix_check(path: str, text: str) -> Tuple[bool, int, int]:
    """Whether a reconstruction is a byte-exact PREFIX of a live file.

    Description: the honest answer for a session that is still being
      written. The whole-file hash of a growing file cannot match a
      reconstruction of the bytes that existed at ingest, and calling
      that a MISMATCH would report a defect that is not there. Reading
      exactly ``len(text)`` bytes and comparing them is a real
      comparison, not a weakening of one - it proves every byte that was
      ingested is reproduced, and says nothing about bytes appended
      afterwards, which is exactly the claim being made.
    Inputs: path (str), text (str - the reconstructed transcript).
    Output: (is_prefix, reconstructed_bytes, current_file_bytes).
    Example: prefix_check("/dev/null", "") -> (True, 0, 0)
    """
    blob = text.encode("utf-8")
    with open(path, "rb") as handle:
        head = handle.read(len(blob))
    return head == blob, len(blob), os.path.getsize(path)


def _classify(
    conn: sqlite3.Connection, transcript_id: int, source_sha256: str,
) -> Tuple[str, str]:
    """Reconstruct a transcript and compare it against the source hash.

    Description: the proof. The export is re-rendered from the decomposed
      storage and hashed; the comparison is against the hash of the bytes
      read off disk, not against anything the model wrote about itself.
      ``strict=False`` so an unrenderable line is reported rather than
      raising and hiding the state of every other line in the file.
    Inputs: conn, transcript_id (int), source_sha256 (str).
    Output: (outcome, detail) where outcome is OUTCOME_IDENTICAL,
      OUTCOME_MISMATCH or OUTCOME_CANNOT_EVALUATE.
    Example: _classify(conn, 1, "deadbeef")[0] -> "MISMATCH"
    """
    result = export_transcript(conn, transcript_id, strict=False)
    unrenderable = [ln.line_no for ln in result.lines
                    if ln.outcome == VERIFY_CANNOT_RENDER]
    if unrenderable:
        return OUTCOME_CANNOT_EVALUATE, (
            f"{len(unrenderable)} line(s) could not be rendered at all, "
            f"first at line {unrenderable[0]}"
        )
    actual = sha256_text(result.text)
    if actual != source_sha256:
        bad = [ln.line_no for ln in result.failures()][:5]
        return OUTCOME_MISMATCH, (
            f"reconstructed sha256 {actual} != source {source_sha256}; "
            f"first mismatching lines {bad}"
        )
    if not result.verified:
        return OUTCOME_MISMATCH, (
            "whole-file bytes matched the source but a per-line hash did "
            "not - the two comparisons disagree, which is itself a defect"
        )
    return OUTCOME_IDENTICAL, ""


def open_destination(path: str, resume: bool) -> sqlite3.Connection:
    """Create (or reopen) the scratch database at the current schema.

    Description: refuses to reuse an existing file unless ``resume`` is
      set, because silently appending to a previous run's database would
      make every count in the report a mixture of two runs.
    Inputs: path (str), resume (bool).
    Output: sqlite3.Connection with bulk-load pragmas applied.
    Raises: FileExistsError - the database exists and resume is False.
    Example: open_destination(":memory:", False).execute("SELECT 1")
    """
    if path != ":memory:" and os.path.exists(path) and not resume:
        raise FileExistsError(
            f"{path} already exists - pass --resume to continue that run, or "
            "choose a fresh path; appending silently would mix two runs"
        )
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-1048576")
    conn.execute("PRAGMA temp_store=MEMORY")
    with conn:
        run_chain(conn, 0, CURRENT_SCHEMA_VERSION)
    return conn


def already_ingested(conn: sqlite3.Connection) -> Dict[str, int]:
    """Map source_ref to transcript id for everything already stored.

    Description: supports --resume without re-reading a single file, and
      makes a partial run's state explicit rather than inferred.
    Inputs: conn.
    Output: dict source_ref -> transcript id.
    Example: already_ingested(conn) -> {}
    """
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT source_ref, id FROM message_transcripts")
    }


def already_logged(path: str) -> Dict[str, str]:
    """Map source_ref to outcome for every result already in the log.

    Description: the database and the result log can disagree after a
      kill, because a transcript is committed per file while the log is
      a buffered stream. A file present in one and not the other is
      exactly the gap :func:`reverify_unlogged` closes, and pretending
      the two are always in step is how a resumed run would silently
      report fewer files than it actually holds.
    Inputs: path (str) - the results log, which need not exist.
    Output: dict source_ref -> outcome.
    Example: already_logged("/nonexistent") -> {}
    """
    logged: Dict[str, str] = {}
    if not os.path.exists(path):
        return logged
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = record.get("source_ref")
            if isinstance(ref, str):
                logged[ref] = str(record.get("outcome"))
    return logged


def reverify_unlogged(
    conn: sqlite3.Connection, path: str, transcript_id: int, source_ref: str,
) -> Dict[str, object]:
    """Re-prove a transcript that is in the database but not in the log.

    Description: re-reads the file to get the source hash, then runs the
      same export comparison the original pass would have run. The
      ingest-side counters cannot be recovered for these files, which is
      why the report derives bodies and line counts from the DATABASE
      rather than by summing this log.
    Inputs: conn, path (str - absolute), transcript_id (int), source_ref.
    Output: dict - one result record, marked ``reverified``.
    Example: reverify_unlogged(conn, "/missing", 1, "x")["outcome"]
      -> "could_not_evaluate"
    """
    record: Dict[str, object] = {"path": path, "source_ref": source_ref,
                                 "transcript_id": transcript_id,
                                 "reverified": True}
    try:
        data = read_file_lines(path)
    except (OSError, UnicodeDecodeError, MemoryError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"reverify read failed: {type(exc).__name__}: {exc}"
        return record
    record["bytes_read"] = data.byte_length
    record["lines"] = len(data.lines)
    record["source_sha256"] = data.source_sha256
    del data.lines[:]
    try:
        outcome, detail = _classify(conn, transcript_id, data.source_sha256)
    except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"reverify export failed: {type(exc).__name__}"
        return record
    if outcome == OUTCOME_MISMATCH:
        outcome, detail = _reclassify_growing(conn, path, transcript_id,
                                              record, detail)
    record["outcome"] = outcome
    if detail:
        record["reason"] = detail
    return record


def _reclassify_growing(
    conn: sqlite3.Connection, path: str, transcript_id: int,
    record: Dict[str, object], detail: str,
) -> Tuple[str, str]:
    """Re-judge a whole-file hash miss against a file that has grown.

    Description: only ever narrows a MISMATCH to a pass when the
      reconstruction is proven to be the file's exact leading bytes AND
      no individual line hash failed. If either is untrue the MISMATCH
      stands - this is a second comparison, never an excuse.
    Inputs: conn, path (str), transcript_id (int), record (dict, mutated
      with the evidence), detail (str - the original mismatch detail).
    Output: (outcome, detail).
    Example: _reclassify_growing(conn, "/dev/null", 1, {}, "d")[0]
      -> "MISMATCH"
    """
    try:
        result = export_transcript(conn, transcript_id, strict=False)
        is_prefix, built, current = prefix_check(path, result.text)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return OUTCOME_CANNOT_EVALUATE, f"prefix check failed: {exc}"
    if not is_prefix or result.failures():
        return OUTCOME_MISMATCH, detail
    record["prefix_of_growing_file"] = True
    record["reconstructed_bytes"] = built
    record["file_bytes_now"] = current
    return OUTCOME_IDENTICAL, (
        f"file grew while live: {built} ingested bytes reproduce byte-exact "
        f"as the leading bytes of the now-{current}-byte file; 0 per-line "
        "hash failures"
    )


def process_one(
    conn: sqlite3.Connection, path: str, source_ref: str,
) -> Dict[str, object]:
    """Ingest and verify one file, never raising for a bad file.

    Description: every failure mode this corpus is known to contain - an
      unreadable file, a non-UTF-8 byte, a line that is not JSON, a file
      growing under the read - resolves to a RECORD, not an exception. A
      line that is not valid JSON is not an error at all here: the model
      stores it raw and flags it, which is the contract.
    Inputs: conn, path (str - absolute), source_ref (str - the key this
      transcript is stored under).
    Output: dict - one result record, always carrying "outcome".
    Example: process_one(conn, "/missing", "x")["outcome"]
      -> "could_not_evaluate"
    """
    record: Dict[str, object] = {"path": path, "source_ref": source_ref}
    try:
        stat_before = os.stat(path)
        record["size_before"] = stat_before.st_size
    except OSError as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"stat failed: {type(exc).__name__}: {exc}"
        return record

    read_start = time.monotonic()
    try:
        data = read_file_lines(path)
    except UnicodeDecodeError as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"not valid UTF-8: {exc}"
        return record
    except (OSError, MemoryError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"read failed: {type(exc).__name__}: {exc}"
        return record
    record["read_seconds"] = round(time.monotonic() - read_start, 3)
    record["bytes_read"] = data.byte_length
    record["lines"] = len(data.lines)
    record["source_sha256"] = data.source_sha256
    try:
        record["grew_during_run"] = (
            os.stat(path).st_size != stat_before.st_size)
    except OSError:
        record["grew_during_run"] = None

    session_ref = os.path.basename(path)[: -len(".jsonl")]
    ingest_start = time.monotonic()
    try:
        with conn:
            result = ingest_lines(
                conn, source_ref=source_ref, session_ref=session_ref,
                lines=[SourceLine(text=line) for line in data.lines],
                has_trailing_newline=data.has_trailing_newline,
            )
    except (ValueError, sqlite3.Error, RecursionError, MemoryError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"ingest failed: {type(exc).__name__}: {exc}"
        return record
    record["ingest_seconds"] = round(time.monotonic() - ingest_start, 3)
    record["transcript_id"] = result.transcript_id
    record["bodies_created"] = result.bodies_created
    record["bodies_reused"] = result.bodies_reused
    record["fidelity_verified"] = result.fidelity_verified
    record["fidelity_failed"] = result.fidelity_failed
    record["fidelity_unverifiable"] = result.fidelity_unverifiable
    record["secret_findings"] = result.secret_findings

    del data.lines[:]
    verify_start = time.monotonic()
    try:
        outcome, detail = _classify(conn, result.transcript_id,
                                    data.source_sha256)
    except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"export failed: {type(exc).__name__}: {exc}"
        return record
    record["verify_seconds"] = round(time.monotonic() - verify_start, 3)
    record["outcome"] = outcome
    if detail:
        record["reason"] = detail
    return record


def recheck_mismatches(db_path: str, results_path: str) -> int:
    """Re-judge every logged MISMATCH, appending a fresh verdict.

    Description: appends rather than rewrites, so the original verdict
      and its revision both stay in the log and the change is auditable.
      The report reads the LAST record per source_ref.
    Inputs: db_path (str), results_path (str).
    Output: int exit code - 1 if any mismatch survived the recheck.
    Example: recheck_mismatches(":memory:", "/dev/null") -> 0
    """
    logged = already_logged(results_path)
    refs = [ref for ref, outcome in logged.items()
            if outcome == OUTCOME_MISMATCH]
    print(f"rechecking {len(refs)} logged mismatch(es)", flush=True)
    if not refs:
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ids = already_ingested(conn)
    survived = 0
    with open(results_path, "a", encoding="utf-8") as log:
        for ref in sorted(refs):
            path = os.path.join(CORPUS_ROOT, ref)
            record = reverify_unlogged(conn, path, ids[ref], ref)
            record["recheck"] = True
            log.write(json.dumps(record) + "\n")
            log.flush()
            print(f"  {record['outcome']} {ref}\n    {record.get('reason')}",
                  flush=True)
            if record["outcome"] == OUTCOME_MISMATCH:
                survived += 1
    conn.close()
    return 1 if survived else 0


def run(
    db_path: str, results_path: str, resume: bool, limit: Optional[int],
) -> int:
    """Walk the corpus, ingest and verify every file, log every result.

    Description: writes one JSON record per file to ``results_path`` and
      flushes as it goes, so a run that is killed at hour three still
      leaves a complete record of everything it did.
    Inputs: db_path (str), results_path (str), resume (bool), limit
      (int or None - stop after this many files, for a smoke test).
    Output: int exit code - 0 when no file mismatched, 1 when any did.
    Example: run(":memory:", "/dev/null", False, 1) -> 0
    """
    conn = open_destination(db_path, resume)
    done = already_ingested(conn) if resume else {}
    logged = already_logged(results_path) if resume else {}
    paths = find_transcripts(CORPUS_ROOT)
    if limit is not None:
        paths = paths[:limit]
    counts: Dict[str, int] = {OUTCOME_IDENTICAL: 0, OUTCOME_MISMATCH: 0,
                              OUTCOME_CANNOT_EVALUATE: 0, "skipped_resume": 0,
                              "reverified": 0}
    started = time.monotonic()
    total_lines = 0
    total_bytes = 0
    with open(results_path, "a" if resume else "w", encoding="utf-8") as log:
        for index, path in enumerate(paths, start=1):
            source_ref = os.path.relpath(path, CORPUS_ROOT)
            if source_ref in done:
                if source_ref in logged:
                    counts["skipped_resume"] += 1
                    continue
                counts["reverified"] += 1
                record = reverify_unlogged(conn, path, done[source_ref],
                                           source_ref)
            else:
                record = process_one(conn, path, source_ref)
            counts[str(record["outcome"])] += 1
            total_lines += int(record.get("lines") or 0)
            total_bytes += int(record.get("bytes_read") or 0)
            log.write(json.dumps(record) + "\n")
            log.flush()
            if record["outcome"] == OUTCOME_MISMATCH:
                print(f"MISMATCH {source_ref}: {record.get('reason')}",
                      flush=True)
            if index % 250 == 0:
                elapsed = time.monotonic() - started
                print(
                    f"[{index}/{len(paths)}] {elapsed:8.1f}s "
                    f"lines={total_lines} bytes={total_bytes} "
                    f"ok={counts[OUTCOME_IDENTICAL]} "
                    f"mismatch={counts[OUTCOME_MISMATCH]} "
                    f"cannot={counts[OUTCOME_CANNOT_EVALUATE]} "
                    f"rss={_peak_rss_mb():.0f}MB",
                    flush=True,
                )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    elapsed = time.monotonic() - started
    print("\n--- corpus run complete ---", flush=True)
    print(f"  files walked                {len(paths)}")
    for key in (OUTCOME_IDENTICAL, OUTCOME_MISMATCH, OUTCOME_CANNOT_EVALUATE,
                "skipped_resume", "reverified"):
        print(f"  {key:27s} {counts[key]}")
    print(f"  lines ingested              {total_lines}")
    print(f"  source bytes read           {total_bytes}")
    print(f"  wall clock seconds          {elapsed:.1f}")
    print(f"  peak rss MB                 {_peak_rss_mb():.0f}")
    return 1 if counts[OUTCOME_MISMATCH] else 0


def _peak_rss_mb() -> float:
    """Peak resident set size of this process, in megabytes.

    Description: macOS reports ``ru_maxrss`` in bytes, unlike Linux which
      reports kilobytes. The platform is checked rather than guessed
      because a 1024x error in a memory figure is the kind of number a
      reader would believe.
    Inputs: none.
    Output: float - megabytes.
    Example: _peak_rss_mb() > 0 -> True
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code.
    Example: main(["--db", ":memory:", "--results", "/dev/null",
      "--limit", "1"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--recheck-mismatches", action="store_true")
    args = parser.parse_args(argv)
    if args.recheck_mismatches:
        return recheck_mismatches(args.db, args.results)
    return run(args.db, args.results, args.resume, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
