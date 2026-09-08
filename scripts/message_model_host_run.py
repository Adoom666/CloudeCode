#!/usr/bin/env python3
"""Ingest several machines' corpora into ONE v17 database, attributed.

WHAT IS DIFFERENT FROM message_model_corpus_run.py. That script proves
one corpus byte-exact and knows nothing about hosts. This one takes a
list of (host, corpus) pairs, each with a collection manifest captured
ON its own machine, and adds the attribution layer: which machine, which
corpus, which project, and how well each of those is evidenced. The
byte-exact machinery is IMPORTED from that script rather than
reimplemented, because two copies of a proof are two proofs that can
disagree.

WHY source_ref IS REWRITTEN. v16's ``source_ref`` is UNIQUE and held a
path relative to one corpus root. With two machines running as the same
unix user that is no longer unique - both hold
``-Users-jsugamele/<uuid>.jsonl`` shaped paths - so the constraint would
have rejected the mini's genuinely distinct file with an IntegrityError
that ``process_one`` records as ``could_not_evaluate``. The file would
be DROPPED for a naming accident, which this model does not do. So
``source_ref`` becomes ``<machine_id>::<corpus_key>::<relpath>`` and the
human-readable part moves to the new ``source_path`` column.
``--attribute-existing`` rewrites the pre-existing rows into that form
in the same pass that attributes them, so there is one convention in the
database rather than two.

THE THREE OUTCOMES SURVIVE INTO ATTRIBUTION. A file is
``manifest_verified``, ``declared``, or ``cannot_determine`` - see
src/core/message_host_identity.classify_attribution. Nothing is dropped
for landing in the third; it is stored with its attribution withheld and
counted where a reader will see it.

Usage:
  --attribute-existing --manifest M.json --root R --layout L
  --ingest --manifest M.json --root R --layout L --results OUT.jsonl
  --verify-all --map 'MACHINE_ID::corpus-key=/local/root'
Reporting lives in scripts/message_model_host_report.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.message_model_corpus_run import (  # noqa: E402
    OUTCOME_CANNOT_EVALUATE,
    OUTCOME_IDENTICAL,
    OUTCOME_MISMATCH,
    _classify,
    _reclassify_growing,
    read_file_lines,
)
from src.core.db_models import CURRENT_SCHEMA_VERSION  # noqa: E402
from src.core.db_steps import run_chain  # noqa: E402
from src.core.message_host_dimension import (  # noqa: E402
    LAYOUT_CLAUDE_PROJECTS,
    PROJ_DERIVED,
    attribute_transcript,
    derive_slug,
    global_source_ref,
    unseen_manifest_paths,
    upsert_corpus,
    upsert_host,
    upsert_project,
)
from src.core.message_host_identity import (  # noqa: E402
    ATTR_CANNOT_DETERMINE,
    HostIdentity,
    classify_attribution,
    iter_manifest_paths,
    manifest_sha,
    walk_jsonl,
)
from src.core.message_model_ingest import SourceLine, ingest_lines  # noqa: E402


def load_manifest(path: str) -> Dict[str, object]:
    """Read a collection manifest captured on a source machine.

    Description: the manifest is the only evidence of provenance in the
      whole pipeline, so a malformed one is an error rather than a
      silently-empty dict that would degrade every file to ``declared``
      without saying so.
    Inputs: path (str).
    Output: dict.
    Raises: ValueError - not a manifest.
    Example: load_manifest("/nonexistent")  # raises OSError
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or "files" not in data or \
            "machine_id" not in data:
        raise ValueError(f"{path} is not a collection manifest")
    return data


def identity_of(manifest: Dict[str, object]) -> HostIdentity:
    """The HostIdentity a manifest carries.

    Inputs: manifest (dict).
    Output: HostIdentity.
    Example: identity_of({"machine_id": "m", "machine_id_scheme":
      "declared", "display_name": "d"}).machine_id -> "m"
    """
    return HostIdentity(
        machine_id=str(manifest["machine_id"]),
        machine_id_scheme=str(manifest.get("machine_id_scheme", "declared")),
        display_name=str(manifest.get("display_name") or manifest["machine_id"]),
        hostname=str(manifest.get("hostname") or ""),
        platform=str(manifest.get("platform") or ""),
    )


def open_db(path: str) -> sqlite3.Connection:
    """Open a database and bring it up to the current schema.

    Description: the migration runs in the caller's transaction, so a
      failure leaves the version untouched. Bulk-load pragmas match
      message_model_corpus_run's, because this writes into the same
      11 GB artifact.
    Inputs: path (str).
    Output: sqlite3.Connection.
    Example: open_db(":memory:").execute("SELECT 1")
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-1048576")
    conn.execute("PRAGMA temp_store=MEMORY")
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    current = int(row[0]) if row else 0
    if current < CURRENT_SCHEMA_VERSION:
        with conn:
            run_chain(conn, current, CURRENT_SCHEMA_VERSION)
    return conn


def _project_for(
    conn: sqlite3.Connection, corpus_id: int, rel: str, layout: str,
    cache: Dict[str, Optional[int]],
) -> Tuple[Optional[int], str]:
    """Resolve (project_id, project_attribution) for one relative path.

    Description: memoised per slug, because a corpus with 19,540 files
      has 75 projects and re-querying per file is 19,465 pointless
      round trips.
    Inputs: conn, corpus_id (int), rel (str), layout (str), cache (dict,
      mutated).
    Output: (project id or None, attribution string).
    Example: _project_for(conn, 1, "s/a.jsonl", "claude_projects", {})
    """
    slug, attribution = derive_slug(rel, layout)
    if slug is None or attribution != PROJ_DERIVED:
        return None, attribution
    if slug not in cache:
        cache[slug] = upsert_project(conn, corpus_id, slug)
    return cache[slug], attribution


def attribute_existing(
    conn: sqlite3.Connection, manifest: Dict[str, object], root: str,
    layout: str,
) -> Dict[str, int]:
    """Attribute v16 rows that were ingested before hosts existed.

    Description: the pre-existing 19,541 laptop transcripts hold a bare
      relative path in ``source_ref`` and NULL in every v17 column. This
      maps each one onto its host, corpus and project, rewrites
      ``source_ref`` into the globally unique form, and classifies the
      host attribution against the manifest by re-hashing the file on
      disk. It re-hashes rather than trusting the stored
      ``content_sha256``: that column is the model's own claim about
      what it ingested, and comparing a claim against a manifest proves
      the manifest agrees with the claim, not with the bytes.
    Inputs: conn, manifest (dict), root (str - this corpus's local
      root), layout (str).
    Output: dict of counters.
    Example: attribute_existing(conn, m, "/r", "claude_projects")
    """
    identity = identity_of(manifest)
    host_id = upsert_host(conn, identity)
    corpus_id = upsert_corpus(conn, host_id, str(manifest["corpus_key"]),
                              str(manifest.get("root_path") or root),
                              manifest_sha(manifest))
    rows = conn.execute(
        "SELECT id, source_ref FROM message_transcripts "
        "WHERE host_id IS NULL ORDER BY id").fetchall()
    counts: Dict[str, int] = {"attributed": 0}
    cache: Dict[str, Optional[int]] = {}
    for transcript_id, source_ref in rows:
        rel = str(source_ref)
        full = os.path.join(root, rel)
        try:
            data = read_file_lines(full)
            observed, nbytes = data.source_sha256, data.byte_length
            del data.lines[:]
        except (OSError, UnicodeDecodeError, MemoryError) as exc:
            observed, nbytes = "", -1
            counts["reread_failed"] = counts.get("reread_failed", 0) + 1
            _ = exc
        attribution, _detail = classify_attribution(
            manifest, rel, observed, nbytes)
        project_id, project_attr = _project_for(
            conn, corpus_id, rel, layout, cache)
        attribute_transcript(
            conn, int(transcript_id), host_id=host_id, corpus_id=corpus_id,
            project_id=project_id, source_path=rel,
            host_attribution=attribution, project_attribution=project_attr)
        conn.execute(
            "UPDATE message_transcripts SET source_ref = ? WHERE id = ?",
            (global_source_ref(identity.machine_id,
                               str(manifest["corpus_key"]), rel),
             int(transcript_id)))
        counts["attributed"] += 1
        counts[attribution] = counts.get(attribution, 0) + 1
        if counts["attributed"] % 2000 == 0:
            conn.commit()
            print(f"  attributed {counts['attributed']}/{len(rows)}",
                  flush=True)
    conn.commit()
    counts["missing_from_db"] = len(unseen_manifest_paths(
        conn, corpus_id, list(iter_manifest_paths(manifest))))
    return counts


def ingest_corpus(
    conn: sqlite3.Connection, manifest: Dict[str, object], root: str,
    layout: str, results_path: str,
) -> Dict[str, int]:
    """Ingest and prove one host's corpus, attributing as it goes.

    Description: per file - read and hash the bytes, ingest them under a
      globally unique source_ref, reconstruct the transcript and compare
      it against the hash of what was read, then attribute. The
      byte-exact comparison is message_model_corpus_run's, unchanged.
      Attribution happens AFTER storage, always: a file whose provenance
      cannot be evidenced is fully stored with its attribution withheld.
    Inputs: conn, manifest (dict), root (str), layout (str),
      results_path (str - one JSON record per file is appended here).
    Output: dict of counters.
    Example: ingest_corpus(conn, m, "/r", "claude_projects", "/dev/null")
    """
    identity = identity_of(manifest)
    host_id = upsert_host(conn, identity)
    corpus_id = upsert_corpus(conn, host_id, str(manifest["corpus_key"]),
                              str(manifest.get("root_path") or root),
                              manifest_sha(manifest))
    paths = sorted(iter_manifest_paths(manifest))
    counts: Dict[str, int] = {OUTCOME_IDENTICAL: 0, OUTCOME_MISMATCH: 0,
                              OUTCOME_CANNOT_EVALUATE: 0, "skipped_present": 0}
    cache: Dict[str, Optional[int]] = {}
    present = {
        str(row[0]) for row in conn.execute(
            "SELECT source_path FROM message_transcripts "
            "WHERE corpus_id = ? AND source_path IS NOT NULL", (corpus_id,))
    }
    counts["on_disk_not_in_manifest"] = len(
        set(walk_jsonl(root)) - set(paths))
    started = time.monotonic()
    with open(results_path, "a", encoding="utf-8") as log:
        for index, rel in enumerate(paths, start=1):
            if rel in present:
                counts["skipped_present"] += 1
                continue
            record = _one_file(conn, manifest, root, rel, layout, host_id,
                               corpus_id, identity, cache)
            counts[str(record["outcome"])] += 1
            key = str(record.get("host_attribution") or "unattributed")
            counts[key] = counts.get(key, 0) + 1
            log.write(json.dumps(record) + "\n")
            log.flush()
            if record["outcome"] == OUTCOME_MISMATCH:
                print(f"MISMATCH {rel}: {record.get('reason')}", flush=True)
            if index % 250 == 0:
                print(f"  [{index}/{len(paths)}] "
                      f"{time.monotonic() - started:.1f}s "
                      f"ok={counts[OUTCOME_IDENTICAL]} "
                      f"mismatch={counts[OUTCOME_MISMATCH]} "
                      f"cannot={counts[OUTCOME_CANNOT_EVALUATE]}", flush=True)
    conn.commit()
    counts["missing_from_db"] = len(unseen_manifest_paths(
        conn, corpus_id, paths))
    return counts


def _one_file(
    conn: sqlite3.Connection, manifest: Dict[str, object], root: str,
    rel: str, layout: str, host_id: int, corpus_id: int,
    identity: HostIdentity, cache: Dict[str, Optional[int]],
) -> Dict[str, object]:
    """Ingest, prove and attribute exactly one transcript file.

    Description: never raises for a bad file. Every known failure mode -
      unreadable, non-UTF-8, unparseable JSON, a duplicate source_ref -
      resolves to a record carrying an outcome.
    Inputs: conn, manifest, root (str), rel (str - relative path),
      layout (str), host_id (int), corpus_id (int), identity, cache.
    Output: dict - one result record, always carrying "outcome".
    Example: _one_file(conn, {}, "/r", "missing.jsonl", "claude_projects",
      1, 1, ident, {})["outcome"] -> "could_not_evaluate"
    """
    full = os.path.join(root, rel)
    source_ref = global_source_ref(
        identity.machine_id, str(manifest.get("corpus_key")), rel)
    record: Dict[str, object] = {"source_ref": source_ref, "source_path": rel,
                                 "machine_id": identity.machine_id}
    try:
        data = read_file_lines(full)
    except (OSError, UnicodeDecodeError, MemoryError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"read failed: {type(exc).__name__}: {exc}"
        return record
    record["bytes_read"] = data.byte_length
    record["lines"] = len(data.lines)
    attribution, detail = classify_attribution(
        manifest, rel, data.source_sha256, data.byte_length)
    record["host_attribution"] = attribution
    if detail:
        record["host_attribution_detail"] = detail
    session_ref = os.path.basename(rel)[: -len(".jsonl")]
    try:
        with conn:
            result = ingest_lines(
                conn, source_ref=source_ref, session_ref=session_ref,
                lines=[SourceLine(text=line) for line in data.lines],
                has_trailing_newline=data.has_trailing_newline)
    except (ValueError, sqlite3.Error, RecursionError, MemoryError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"ingest failed: {type(exc).__name__}: {exc}"
        return record
    record["transcript_id"] = result.transcript_id
    record["bodies_created"] = result.bodies_created
    record["bodies_reused"] = result.bodies_reused
    record["secret_findings"] = result.secret_findings
    del data.lines[:]
    project_id, project_attr = _project_for(conn, corpus_id, rel, layout,
                                            cache)
    record["project_attribution"] = project_attr
    with conn:
        attribute_transcript(
            conn, result.transcript_id, host_id=host_id, corpus_id=corpus_id,
            project_id=project_id, source_path=rel,
            host_attribution=attribution, project_attribution=project_attr)
    try:
        outcome, why = _classify(conn, result.transcript_id,
                                 data.source_sha256)
    except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        record["outcome"] = OUTCOME_CANNOT_EVALUATE
        record["reason"] = f"export failed: {type(exc).__name__}: {exc}"
        return record
    if outcome == OUTCOME_MISMATCH:
        outcome, why = _reclassify_growing(conn, full, result.transcript_id,
                                           record, why)
    record["outcome"] = outcome
    if why:
        record["reason"] = why
    return record


def verify_all(
    conn: sqlite3.Connection, roots: Dict[Tuple[str, str], str],
) -> Dict[str, int]:
    """Re-prove EVERY stored transcript byte-exact, after the migration.

    Description: the earlier single-host run proved 19,541 files against
      a v16 database. This database is not that one - it has been
      migrated, its ``source_ref`` column has been rewritten and two more
      corpora have been written into it. Carrying the old verdict
      forward would be inheriting a proof taken on a different artifact,
      which is the same defect as trusting a deploy's own exit code. So
      every transcript is reconstructed from the decomposed storage
      again, here, and compared against a fresh hash of the bytes on
      disk. Three outcomes per file, and a growing live file is narrowed
      to a pass ONLY by the prefix comparison, never by assumption.
    Inputs: conn, roots (dict mapping (machine_id, corpus_key) to the
      local directory that corpus's files can be read from).
    Output: dict of counters.
    Example: verify_all(conn, {}) -> {...}
    """
    counts: Dict[str, int] = {OUTCOME_IDENTICAL: 0, OUTCOME_MISMATCH: 0,
                              OUTCOME_CANNOT_EVALUATE: 0}
    rows = conn.execute(
        "SELECT t.id, t.source_path, h.machine_id, c.corpus_key "
        "  FROM message_transcripts t "
        "  JOIN message_corpora c ON c.id = t.corpus_id "
        "  JOIN message_hosts h ON h.id = c.host_id "
        " ORDER BY t.id").fetchall()
    for index, (transcript_id, rel, machine_id, corpus_key) in enumerate(
            rows, start=1):
        root = roots.get((str(machine_id), str(corpus_key)))
        if root is None:
            counts[OUTCOME_CANNOT_EVALUATE] += 1
            counts["no_local_root"] = counts.get("no_local_root", 0) + 1
            continue
        full = os.path.join(root, str(rel))
        try:
            data = read_file_lines(full)
            source_sha = data.source_sha256
            del data.lines[:]
        except (OSError, UnicodeDecodeError, MemoryError) as exc:
            counts[OUTCOME_CANNOT_EVALUATE] += 1
            print(f"  CANNOT {rel}: {type(exc).__name__}: {exc}", flush=True)
            continue
        try:
            outcome, detail = _classify(conn, int(transcript_id), source_sha)
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            counts[OUTCOME_CANNOT_EVALUATE] += 1
            print(f"  CANNOT {rel}: export {type(exc).__name__}", flush=True)
            continue
        if outcome == OUTCOME_MISMATCH:
            outcome, detail = _reclassify_growing(
                conn, full, int(transcript_id), {}, detail)
            if outcome == OUTCOME_IDENTICAL:
                counts["grew_while_live"] = counts.get("grew_while_live", 0) + 1
        counts[outcome] += 1
        if outcome == OUTCOME_MISMATCH:
            print(f"  MISMATCH {machine_id} {rel}: {detail}", flush=True)
        if index % 2500 == 0:
            print(f"  verified {index}/{len(rows)} "
                  f"ok={counts[OUTCOME_IDENTICAL]} "
                  f"mismatch={counts[OUTCOME_MISMATCH]} "
                  f"cannot={counts[OUTCOME_CANNOT_EVALUATE]}", flush=True)
    counts["transcripts"] = len(rows)
    return counts


def parse_root_map(values: Sequence[str]) -> Dict[Tuple[str, str], str]:
    """Turn ``machine_id::corpus_key=/local/root`` strings into a dict.

    Inputs: values (sequence of str).
    Output: dict[(machine_id, corpus_key), root].
    Raises: ValueError - a value is not in that form.
    Example: parse_root_map(["M::k=/r"]) -> {("M", "k"): "/r"}
    """
    out: Dict[Tuple[str, str], str] = {}
    for value in values:
        if "=" not in value or "::" not in value.split("=", 1)[0]:
            raise ValueError(
                f"{value!r} is not machine_id::corpus_key=/local/root")
        left, root = value.split("=", 1)
        machine_id, corpus_key = left.split("::", 1)
        out[(machine_id, corpus_key)] = os.path.expanduser(root)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code - 1 if any file mismatched.
    Example: main(["--db", ":memory:", "--finalize"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--root")
    parser.add_argument("--layout", default=LAYOUT_CLAUDE_PROJECTS)
    parser.add_argument("--results", default="/dev/null")
    parser.add_argument("--attribute-existing", action="store_true")
    parser.add_argument("--ingest", action="store_true")
    parser.add_argument("--verify-all", action="store_true")
    parser.add_argument("--map", action="append", default=[])
    args = parser.parse_args(argv)
    conn = open_db(args.db)
    try:
        if args.verify_all:
            counts = verify_all(conn, parse_root_map(args.map))
            for key in sorted(counts):
                print(f'  {key:22s} {counts[key]}')
            return 1 if counts.get(OUTCOME_MISMATCH) else 0
        if not args.manifest or not args.root:
            parser.error("--manifest and --root are required")
        manifest = load_manifest(args.manifest)
        if args.attribute_existing:
            counts = attribute_existing(conn, manifest, args.root, args.layout)
        elif args.ingest:
            counts = ingest_corpus(conn, manifest, args.root, args.layout,
                                   args.results)
        else:
            parser.error("choose --attribute-existing, --ingest or --verify-all")
        for key in sorted(counts):
            print(f"  {key:22s} {counts[key]}")
        if counts.get("missing_from_db"):
            print("  WARNING: the source machine's manifest names files that "
                  "are not in this database")
        if counts.get(ATTR_CANNOT_DETERMINE):
            print("  NOTE: some files are stored with attribution WITHHELD "
                  "(cannot_determine) - they are not lost, only unattributed")
        return 1 if counts.get(OUTCOME_MISMATCH) else 0
    finally:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
