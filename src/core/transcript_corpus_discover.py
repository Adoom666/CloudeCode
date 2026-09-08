"""Corpus discovery and content hashing for transcript_corpus_ingest.py.

Description: split out of transcript_corpus_ingest.py purely to respect
  this project's 500-line file cap - see that module's docstring for the
  full design rationale (idempotency key, growing-file handling, decisive
  rooting). This file holds only the parts that never touch a database
  connection: walking the corpus directory tree by STRUCTURE (never
  content) into :class:`CorpusEntry` rows, and the streaming sha256/mtime
  helpers used to decide whether a file's content has changed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

#: Read granularity for the pre-ingest hashing pass. Matches
#: transcript_archive.DEFAULT_STREAM_CHUNK_SIZE so the two passes over a
#: changed file cost the same order of magnitude of work.
HASH_CHUNK_SIZE = 4 * 1024 * 1024

#: The subdirectory name Claude Code always uses for subagent
#: transcripts - part of the structural rooting rule, not a guess.
SUBAGENTS_DIRNAME = "subagents"

#: How many example paths each discovery outcome list keeps. The COUNTS
#: are exact; the lists are a sample, because a corpus with tens of
#: thousands of unrecognised files must not turn one report into a
#: multi-megabyte JSON blob.
OUTCOME_SAMPLE_LIMIT = 50


@dataclass
class CorpusEntry:
    """One file discovered under the corpus root, not yet read.

    Description: the output of :func:`discover_corpus` - path plus the
      structural facts derivable from its LOCATION alone (never its
      content), which is exactly what the decisive rooting rules are
      allowed to use.
    Inputs: constructed only by :func:`discover_corpus`.
    Output: n/a (data holder).
    """

    abs_path: Path
    source_path: str  # corpus-relative, POSIX separators
    kind: str  # "session" | "subagent"


def default_corpus_root() -> Path:
    """Return the default Claude Code transcript corpus root.

    Description: single source of the default path so callers who want
      the real corpus and callers who want a scratch copy pass this
      exact same join everywhere.
    Inputs: none.
    Output: Path - ``~/.claude/projects``, expanded, not verified to
      exist.
    Example: default_corpus_root()  # Path('/Users/x/.claude/projects')
    """
    return Path.home() / ".claude" / "projects"


@dataclass
class DiscoveryOutcome:
    """What one corpus walk found, refused, and could not look at.

    Description: THE THREE-OUTCOME RULE APPLIED TO DISCOVERY ITSELF.
      Before this existed :func:`discover_corpus` returned a bare list,
      so "this path holds no transcripts", "this path holds files whose
      shape I refuse to classify" and "I could not read this directory
      at all" were the same answer: absence from the list. That is
      exactly how 442 real workflow transcripts sat outside the archive
      for months with every ingest report reading green - the scanner
      was not failing to walk them, it was walking past them and saying
      nothing.
    Inputs: constructed only by :func:`discover_corpus_detailed`.
    Output: n/a (data holder).
    """

    entries: List[CorpusEntry] = field(default_factory=list)
    #: Paths the walk reached and deliberately did not classify - a
    #: non-``.jsonl`` file, or a session directory with no
    #: ``subagents/``. Exact count, sampled paths.
    unrecognised_count: int = 0
    unrecognised_sample: List[str] = field(default_factory=list)
    #: Directories the walk could not list. This is NOT "nothing there";
    #: it is "I do not know what is there", and it must never be
    #: rendered as either of the other two.
    unreadable_count: int = 0
    unreadable_sample: List[Dict[str, str]] = field(default_factory=list)


def _note(count: int, sample: list, value) -> int:
    """Record one outcome, keeping the count exact and the sample bounded.

    Description: see :data:`OUTCOME_SAMPLE_LIMIT`.
    Inputs: count (int), sample (list, mutated), value (str | dict).
    Output: int - the incremented count.
    Example: _note(0, [], "a/b") -> 1
    """
    if len(sample) < OUTCOME_SAMPLE_LIMIT:
        sample.append(value)
    return count + 1


def _iter_dir(path: Path, outcome: DiscoveryOutcome) -> Optional[List[Path]]:
    """List one directory, or record that it could not be listed.

    Description: an ``OSError`` here is the third outcome, not an empty
      directory - it is recorded on the outcome and the caller skips
      that branch rather than reporting it as "nothing found".
    Inputs: path (Path), outcome (DiscoveryOutcome, mutated).
    Output: list[Path] sorted | None when the listing failed.
    Example: _iter_dir(Path("/nope"), DiscoveryOutcome()) -> None
    """
    try:
        return sorted(path.iterdir())
    except OSError as exc:
        outcome.unreadable_count = _note(
            outcome.unreadable_count,
            outcome.unreadable_sample,
            {"path": str(path), "reason": str(exc)},
        )
        return None


def _walk_subagents(
    sub: Path, prefix: str, outcome: DiscoveryOutcome,
) -> None:
    """Collect every ``*.jsonl`` at ANY depth under one ``subagents/`` dir.

    Description: RECURSIVE ON PURPOSE, and the recursion is what closes
      the workflows gap. Claude Code nests workflow subagent transcripts
      at ``subagents/workflows/<wf_id>/*.jsonl`` (and iCloud forks that
      directory into ``workflows 2``), which the previous one-level
      listing could not see. Depth is not part of the rooting decision -
      ``<slug>/<uuid>/`` is already fixed by the two path segments above
      this call - so walking deeper adds transcripts without adding a
      single guess.

      THE SUFFIX FILTER IS LOAD-BEARING. ``tool-results/`` artifacts
      (.txt, .pdf, .jpg) live beside these trees and are NOT transcripts;
      the archive's byte-exactness guarantees are built on JSONL. They
      are counted as ``unrecognised``, never ingested.
    Inputs: sub (Path - the ``subagents`` directory), prefix (str -
      ``<slug>/<uuid>/subagents``), outcome (DiscoveryOutcome, mutated).
    Output: None.
    Example: _walk_subagents(Path("/c/s/u/subagents"), "s/u/subagents", o)
    """
    listing = _iter_dir(sub, outcome)
    if listing is None:
        return
    for item in listing:
        if item.is_file():
            if item.suffix == ".jsonl":
                outcome.entries.append(
                    CorpusEntry(
                        abs_path=item,
                        source_path=f"{prefix}/{item.name}",
                        kind="subagent",
                    )
                )
            else:
                outcome.unrecognised_count = _note(
                    outcome.unrecognised_count,
                    outcome.unrecognised_sample,
                    f"{prefix}/{item.name}",
                )
        elif item.is_dir():
            _walk_subagents(item, f"{prefix}/{item.name}", outcome)


def discover_corpus_detailed(corpus_root: Path) -> DiscoveryOutcome:
    """Walk a corpus root, classifying by location alone, and SAY what it skipped.

    Description: two shapes only, both structural - a top-level file
      directly under a project-slug directory is ``kind="session"``; a
      ``*.jsonl`` at any depth under ``<slug>/<uuid>/subagents/`` is
      ``kind="subagent"``. Everything the walk reaches and does not
      classify is counted as ``unrecognised``, and everything it could
      not list is counted as ``unreadable``. Those are three different
      answers and they are reported as three different answers.
    Inputs: corpus_root (Path).
    Output: DiscoveryOutcome.
    Example: discover_corpus_detailed(Path("/c")).unreadable_count -> 0
    """
    outcome = DiscoveryOutcome()
    corpus_root = Path(corpus_root)
    if not corpus_root.is_dir():
        return outcome

    top = _iter_dir(corpus_root, outcome)
    if top is None:
        return outcome
    for slug_dir in top:
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        listing = _iter_dir(slug_dir, outcome)
        if listing is None:
            continue
        for item in listing:
            if item.is_file():
                if item.suffix == ".jsonl":
                    outcome.entries.append(
                        CorpusEntry(
                            abs_path=item,
                            source_path=f"{slug}/{item.name}",
                            kind="session",
                        )
                    )
                else:
                    outcome.unrecognised_count = _note(
                        outcome.unrecognised_count,
                        outcome.unrecognised_sample,
                        f"{slug}/{item.name}",
                    )
            elif item.is_dir():
                sub = item / SUBAGENTS_DIRNAME
                if not sub.is_dir():
                    outcome.unrecognised_count = _note(
                        outcome.unrecognised_count,
                        outcome.unrecognised_sample,
                        f"{slug}/{item.name}",
                    )
                    continue
                _walk_subagents(
                    sub, f"{slug}/{item.name}/{SUBAGENTS_DIRNAME}", outcome,
                )
    return outcome


def discover_corpus(corpus_root: Path) -> List[CorpusEntry]:
    """Walk a corpus root and return every classified transcript.

    Description: the entries half of :func:`discover_corpus_detailed`.
      Callers that want the skipped and unreadable counts - and every
      caller that publishes a report should - call that function
      instead.
    Inputs: corpus_root (Path) - typically :func:`default_corpus_root`
      or a local read-only copy of it.
    Output: list[CorpusEntry].
    Example: discover_corpus(Path("~/.claude/projects")) -> [...]
    """
    return discover_corpus_detailed(corpus_root).entries


def hash_file(path: Path) -> tuple:
    """Stream-hash a file without holding it fully in memory.

    Description: the pre-ingest identity check - reads the file once in
      bounded chunks purely to compute sha256 and size, so the
      already-present decision can be made before the (separate,
      heavier) actual ingest read happens.
    Inputs: path (Path).
    Output: tuple[str, int] - (sha256 hex digest, byte length).
    Raises: OSError - the file could not be opened or read.
    Example: hash_file(Path("x.jsonl")) -> ("abc123...", 4096)
    """
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def mtime_iso(path: Path) -> Optional[str]:
    """Read a file's mtime as UTC ISO-8601, or None if it cannot be stat'd.

    Description: provenance only (see transcript_archive.py's module
      docstring - ``ingest_source_mtime`` is never authoritative for
      anything this module decides), so a stat failure degrades to None
      rather than aborting the ingest.
    Inputs: path (Path).
    Output: str | None.
    """
    try:
        ts = os.stat(path).st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
