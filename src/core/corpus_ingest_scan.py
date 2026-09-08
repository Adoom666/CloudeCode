"""The cheap half of an ingest pass: what to look at, and what to skip.

SPLIT OUT OF ``corpus_ingest_service.py`` PURELY FOR THE 500-LINE CAP.
Read that module's docstring first; this file holds only the decisions
that are made BEFORE any file is read, plus the two database
fingerprints those decisions rest on.

THE ONE INVARIANT EVERYTHING HERE PRESERVES: a shortcut may only ever
cost EXTRA WORK. Every function below either proves its premise or
falls back to the expensive path. There is no branch that skips a file
on a guess.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Tuple

from src.core.transcript_corpus_discover import CorpusEntry


def _latest_hashes(conn: sqlite3.Connection, since_id: int = 0) -> Dict[str, str]:
    """Map archived source_paths to the newest content hash stored.

    Description: ONE query instead of 19,000. The newest row per path is
      the one the idempotency key compares against, exactly as
      ``transcript_corpus_ingest._latest_archive_for_source`` does per
      file; this is that same question asked in bulk so the scan pass
      does not pay a round trip per file.

      ``since_id`` EXISTS FOR A MEASURED REASON. ``transcript_archives``
      carries the compressed transcript itself in ``content_gzip`` and
      there is no index on ``source_path``, so the unrestricted form is
      a full table scan that reads every stored blob off disk - 0.25s
      on a 400-file archive, and this is a loop that runs every few
      minutes on an archive two orders of magnitude bigger. Restricting
      to ``id > since_id`` turns it into a primary-key range scan that
      touches only rows written since the caller last looked. See
      :func:`_resolve_db_hashes` for when that is sound.
    Inputs: conn (sqlite3.Connection at schema v14 or later), since_id
      (int - 0 means every row).
    Output: dict source_path -> content_sha256, newest row winning
      because ``ORDER BY id`` makes later rows overwrite earlier ones.
    Example: _latest_hashes(conn).get("slug/a.jsonl")
    """
    if since_id > 0:
        rows = conn.execute(
            "SELECT source_path, content_sha256 FROM transcript_archives"
            " WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source_path, content_sha256 FROM transcript_archives"
            " WHERE id IN (SELECT MAX(id) FROM transcript_archives"
            "              GROUP BY source_path)"
        ).fetchall()
    return {str(row["source_path"]): str(row["content_sha256"]) for row in rows}


def _db_signature(conn: sqlite3.Connection) -> Dict[str, object]:
    """Fingerprint the database cheaply enough to check on every run.

    Description: five sub-millisecond reads, all index-served. The
      install id identifies WHICH database this is (a restored or
      recreated file gets a different one); ``max_archive_id`` says how
      far the append-only archive has advanced; the three ``sessions``
      figures are what the rooting gate compares - see
      :func:`_rooting_needed` for why a count of non-null uuids is in
      there rather than an id alone.
    Inputs: conn (sqlite3.Connection).
    Output: dict.
    Example: _db_signature(conn)["max_archive_id"] -> 401
    """
    max_archive = conn.execute(
        "SELECT MAX(id) FROM transcript_archives"
    ).fetchone()[0]
    install = conn.execute(
        "SELECT value FROM meta WHERE key = 'install_id'"
    ).fetchone()
    sessions = conn.execute(
        "SELECT COUNT(*), MAX(id), COUNT(claude_session_uuid) FROM sessions"
    ).fetchone()
    return {
        "install_id": None if install is None else str(install[0]),
        "max_archive_id": int(max_archive or 0),
        "sessions_count": int(sessions[0] or 0),
        "sessions_max_id": int(sessions[1] or 0),
        "sessions_with_uuid": int(sessions[2] or 0),
    }


def _resolve_db_hashes(
    conn: sqlite3.Connection,
    cached: Dict[str, Tuple[int, int, str]],
    cached_meta: Dict[str, object],
    signature: Dict[str, object],
) -> Tuple[Dict[str, str], bool]:
    """Return the newest hash per source_path, scanning as little as possible.

    Description: the incremental form is sound because
      ``transcript_archives`` is APPEND ONLY - there is no UPDATE of
      ``content_sha256`` and no DELETE anywhere in this codebase (see
      transcript_corpus_ingest's module docstring, which makes that a
      deliberate design property rather than an accident). Under append
      only, "the newest row for path P" can change only by a new INSERT,
      so overlaying the rows with ``id > cached_max`` onto the cached
      hashes reproduces the full answer exactly.

      IT REFUSES THE SHORTCUT WHENEVER IT CANNOT PROVE THAT PREMISE, and
      falls back to the full scan: no fingerprint on disk, a different
      ``install_id`` (this is a different or restored database), or a
      ``max_archive_id`` that went DOWN (rows disappeared, so something
      other than an append happened). All three resolve to more work,
      never to a skipped file.
    Inputs: conn, cached (scan cache entries), cached_meta (the
      fingerprint stored with them), signature (:func:`_db_signature`
      of the database as it is right now).
    Output: (dict source_path -> newest content_sha256, bool - True when
      a full scan was performed).
    Example: _resolve_db_hashes(conn, {}, {}, sig)[1] -> True
    """
    cached_max = cached_meta.get("max_archive_id")
    same_install = (
        cached_meta.get("install_id") == signature.get("install_id")
    )
    if (
        cached
        and same_install
        and isinstance(cached_max, int)
        and cached_max <= int(signature["max_archive_id"])
    ):
        hashes = {path: entry[2] for path, entry in cached.items()}
        hashes.update(_latest_hashes(conn, since_id=cached_max))
        return hashes, False
    return _latest_hashes(conn), True


def _rooting_needed(
    cached_meta: Dict[str, object], signature: Dict[str, object],
) -> bool:
    """Decide whether the rooting pass can change anything this run.

    Description: rooting resolves an unrooted archive against (a) other
      transcript_archives rows and (b) ``sessions`` rows. If NEITHER set
      has changed since the last pass, the previous pass already
      reached the same fixed point and re-running it can only produce
      the identical verdict - at a measured 0.66 ms per unrooted row,
      which is 12 seconds on a full corpus, every interval, forever.

      THE SESSIONS SIDE IS NOT AN ID COMPARISON, and that matters. A
      session row can LEARN its ``claude_session_uuid`` long after
      insert (the correlate ladder does exactly that), which makes an
      old archive newly rootable without any id moving. So the count of
      non-null uuids is part of the signature. When in doubt - no
      fingerprint, a different install - this returns True, because the
      cost of an unnecessary rooting pass is time and the cost of a
      skipped one is an archive left unrooted.
    Inputs: cached_meta (dict), signature (dict).
    Output: bool.
    Example: _rooting_needed({}, {}) -> True
    """
    if not cached_meta:
        return True
    keys = (
        "install_id", "max_archive_id", "sessions_count",
        "sessions_max_id", "sessions_with_uuid",
    )
    return any(cached_meta.get(key) != signature.get(key) for key in keys)


def _stat_key(entry: CorpusEntry) -> Optional[Tuple[int, int]]:
    """Return (size, mtime_ns) for one entry, or None when it cannot stat.

    Description: a file that cannot be stat'ed is not skipped and not
      counted as read - it is handed to the ingest path, which reports
      it as ``could_not_read`` with the real error. Deciding here would
      duplicate that classification in two places.
    Inputs: entry (CorpusEntry).
    Output: (int, int) | None.
    Example: _stat_key(entry)  # (1024, 1756000000000000000)
    """
    try:
        info = entry.abs_path.stat()
    except OSError:
        return None
    return (int(info.st_size), int(info.st_mtime_ns))


def plan_scan(
    entries: List[CorpusEntry],
    cache: Dict[str, Tuple[int, int, str]],
    db_hashes: Dict[str, str],
) -> Tuple[List[CorpusEntry], int]:
    """Split discovered entries into "must look at" and "provably unchanged".

    Description: the cheap pass described in this module's docstring. A
      file is skipped only when ALL THREE agree: the cache has an entry
      for it, the entry's size and mtime match what is on disk right
      now, and the hash the cache recorded is still the newest hash the
      DATABASE holds for that path. Any disagreement, including a
      failed stat, sends the file down the full hashing path. The cache
      can therefore only ever cost extra work; it can never cause a
      changed or missing file to be skipped.
    Inputs: entries (list[CorpusEntry]), cache (dict from
      corpus_ingest_state.load_scan_cache), db_hashes (dict from
      :func:`_latest_hashes`).
    Output: (list of entries needing a full pass, count skipped).
    Example: plan_scan([], {}, {}) -> ([], 0)
    """
    todo: List[CorpusEntry] = []
    skipped = 0
    for entry in entries:
        cached = cache.get(entry.source_path)
        if cached is None:
            todo.append(entry)
            continue
        stat_key = _stat_key(entry)
        if stat_key is None or stat_key != (cached[0], cached[1]):
            todo.append(entry)
            continue
        if db_hashes.get(entry.source_path) != cached[2]:
            todo.append(entry)
            continue
        skipped += 1
    return todo, skipped



def _current_hash(conn: sqlite3.Connection, source_path: str) -> Optional[str]:
    """Read back the newest stored hash for one source_path.

    Description: the cache records what the DATABASE ended up holding,
      not what the ingester believed it wrote, so a write that did not
      land cannot be cached as if it had.
    Inputs: conn, source_path (str).
    Output: str | None.
    Example: _current_hash(conn, "slug/a.jsonl")
    """
    row = conn.execute(
        "SELECT content_sha256 FROM transcript_archives"
        " WHERE source_path = ? ORDER BY id DESC LIMIT 1",
        (source_path,),
    ).fetchone()
    return str(row["content_sha256"]) if row is not None else None
