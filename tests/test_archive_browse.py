"""An empty archive and an unopenable one must not look the same.

THAT IS THE WHOLE POINT OF THIS FILE. ``("ok", [])`` and
``("cannot_determine", None)`` render identically to any client that
reads only ``result``, and they mean opposite things: one is a
measurement that there is nothing there, the other is an admission that
nobody looked. Every false green in this project's history is one line of
code that turned "I could not look" into "nothing is wrong".

The second pair asserted here is just as easy to conflate and hides real
data when it is. ``project_id IS NULL`` is a NAVIGATION fact - the
transcript exists and no project path reaches it. ``host_attribution =
'cannot_determine'`` is a QUALITY fact - the transcript HAS a host and
the attribution is unevidenced. A transcript can be attributed and
unevidenced at the same time, and it must not be filed under
"unattributed" for it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_archbrowse_logs_"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.archive_hierarchy import (
    corpora_for_host,
    hosts,
    projects_for_corpus,
    transcripts_for_project,
    unattributed_for_corpus,
)
from src.core.archive_lines import transcript_header
from src.core.archive_read import (
    ATTRIBUTION_CANNOT_DETERMINE,
    ATTRIBUTION_EVIDENCED,
    RESULT_CANNOT_DETERMINE,
    RESULT_NOT_FOUND,
    RESULT_OK,
    SCOPE_CANNOT_DETERMINE,
    SCOPE_NOT_FOUND,
    SCOPE_RESOLVED,
    envelope,
    open_read_only,
    run_read,
)
from tests.archive_fixture import (
    make_state_dir,
    seed_corpus,
    seed_host,
    seed_project,
    seed_transcript,
    writable,
)

#: Every envelope carries exactly these keys, on every outcome.
ENVELOPE_KEYS = {"result", "result_status", "scope_status", "unevaluated", "meta"}


@pytest.fixture()
def empty_archive(tmp_path: Path) -> Path:
    """A real, openable archive holding a host, corpus and project, no transcripts.

    Description: the scope RESOLVES and is genuinely empty, which is the
      only way to test that an empty answer is reported as a measurement.
    Inputs: tmp_path (Path).
    Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, "empty")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            seed_project(conn, corpus_id, slug="-empty-project")
    return state_dir


@pytest.fixture()
def unopenable(tmp_path: Path) -> Path:
    """A state directory whose cloude.db cannot be opened.

    Description: the file is present and is not a database, so this
      exercises a genuine open failure rather than a missing path - the
      two are different and both must be cannot_determine.
    Inputs: tmp_path (Path).
    Output: Path - the state directory.
    """
    state_dir = tmp_path / "broken"
    state_dir.mkdir()
    (state_dir / "cloude.db").write_bytes(b"this is definitely not a sqlite file" * 40)
    return state_dir


# --- the central contrast --------------------------------------------------


def test_empty_scope_is_ok_with_an_empty_list(empty_archive: Path) -> None:
    """Genuinely empty: result_status ok, result is [], nothing unevaluated."""
    with closing(open_read_only(empty_archive)) as conn:
        result = transcripts_for_project(conn, 1)
    assert result["result_status"] == RESULT_OK
    assert result["result"] == []
    assert result["scope_status"] == SCOPE_RESOLVED
    assert result["unevaluated"] == []
    assert result["meta"]["paging"]["has_more"] is False


def test_unopenable_datastore_is_cannot_determine_with_a_null_result(
    unopenable: Path,
) -> None:
    """Could not look: result is None, and a reason names the datastore."""
    result = run_read(unopenable, transcripts_for_project, 1)
    assert result["result_status"] == RESULT_CANNOT_DETERMINE
    assert result["result"] is None
    assert result["scope_status"] == SCOPE_CANNOT_DETERMINE
    assert len(result["unevaluated"]) == 1
    assert result["unevaluated"][0]["subject"] == "datastore"
    assert result["unevaluated"][0]["reason"]


def test_missing_database_is_also_cannot_determine(tmp_path: Path) -> None:
    """A state dir with no cloude.db is a could-not-look, never an empty list."""
    missing = tmp_path / "gone"
    missing.mkdir()
    result = run_read(missing, hosts)
    assert result["result_status"] == RESULT_CANNOT_DETERMINE
    assert result["result"] is None


def test_the_two_are_structurally_distinguishable(
    empty_archive: Path, unopenable: Path
) -> None:
    """A client branching on result_status can never confuse the two.

    Description: asserts the discriminators independently, so a future
      change that made one of them agree would still fail here.
    """
    good = run_read(empty_archive, transcripts_for_project, 1)
    bad = run_read(unopenable, transcripts_for_project, 1)

    assert good["result_status"] != bad["result_status"]
    assert good["scope_status"] != bad["scope_status"]
    assert good["result"] == [] and bad["result"] is None
    assert good["unevaluated"] == [] and bad["unevaluated"] != []
    # The shapes are identical, so ONLY the values above can be branched on.
    assert set(good) == set(bad) == ENVELOPE_KEYS


def test_every_outcome_carries_every_envelope_key(
    empty_archive: Path, unopenable: Path
) -> None:
    """ok, not_found and cannot_determine all emit the full key set."""
    with closing(open_read_only(empty_archive)) as conn:
        ok = transcripts_for_project(conn, 1)
        missing = transcripts_for_project(conn, 99999)
    broken = run_read(unopenable, transcripts_for_project, 1)
    for result in (ok, missing, broken):
        assert set(result) == ENVELOPE_KEYS
        assert isinstance(result["unevaluated"], list)
        assert isinstance(result["meta"], dict)


def test_not_found_is_not_cannot_determine(empty_archive: Path) -> None:
    """"There is no project 99999" is a measurement; has_more stays null."""
    with closing(open_read_only(empty_archive)) as conn:
        result = transcripts_for_project(conn, 99999)
    assert result["result_status"] == RESULT_NOT_FOUND
    assert result["scope_status"] == SCOPE_NOT_FOUND
    assert result["result"] == []
    assert result["unevaluated"][0]["subject"] == "project:99999"
    # false would be a claim that the end of a list was reached. No list
    # was read, so the only honest value is null.
    assert result["meta"]["paging"]["has_more"] is None


# --- the second pair: null project vs unevidenced attribution --------------


@pytest.fixture()
def mixed_archive(tmp_path: Path) -> Path:
    """One transcript with no project, one attributed but unevidenced.

    Description: deliberately BOTH conditions in one corpus, because the
      failure this guards against is a query that files the second under
      the first.
    Inputs: tmp_path (Path).
    Output: Path - the state directory.
    """
    state_dir = make_state_dir(tmp_path, "mixed")
    with closing(writable(state_dir)) as conn:
        with conn:
            host_id = seed_host(conn)
            corpus_id = seed_corpus(conn, host_id)
            project_id = seed_project(conn, corpus_id, slug="-p")
            # No project at all: a NAVIGATION problem.
            seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=None,
                source_path="orphan.jsonl",
                project_attribution="none_declared",
            )
            # Has a host and a project, attribution unevidenced: QUALITY.
            seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="unevidenced.jsonl",
                host_attribution="cannot_determine",
            )
            seed_transcript(
                conn,
                host_id=host_id,
                corpus_id=corpus_id,
                project_id=project_id,
                source_path="normal.jsonl",
            )
    return state_dir


def test_null_project_transcript_is_reachable_only_via_unattributed(
    mixed_archive: Path,
) -> None:
    """It is absent from the project listing and present in unattributed."""
    with closing(open_read_only(mixed_archive)) as conn:
        in_project = transcripts_for_project(conn, 1)
        orphans = unattributed_for_corpus(conn, 1)
    project_paths = {row["source_path"] for row in in_project["result"]}
    assert "orphan.jsonl" not in project_paths

    assert orphans["result_status"] == RESULT_OK
    assert [row["source_path"] for row in orphans["result"]] == ["orphan.jsonl"]
    assert orphans["meta"]["unattributed_transcript_count"] == 1
    assert "note" not in orphans["meta"]


def test_unattributed_count_is_published_on_the_corpus_and_projects_pages(
    mixed_archive: Path,
) -> None:
    """A client paging projects cannot finish believing it saw everything."""
    with closing(open_read_only(mixed_archive)) as conn:
        corpora = corpora_for_host(conn, 1)
        projects = projects_for_corpus(conn, 1)
    assert corpora["result"][0]["unattributed_transcript_count"] == 1
    assert corpora["result"][0]["transcript_count"] == 3
    assert projects["meta"]["unattributed"]["transcript_count"] == 1
    assert projects["meta"]["unattributed"]["href"].endswith("/corpora/1/unattributed")


def test_unevidenced_transcript_stays_under_its_project_and_says_so(
    mixed_archive: Path,
) -> None:
    """cannot_determine attribution is NEVER moved into the orphan bucket."""
    with closing(open_read_only(mixed_archive)) as conn:
        in_project = transcripts_for_project(conn, 1)
        orphans = unattributed_for_corpus(conn, 1)
    by_path = {row["source_path"]: row for row in in_project["result"]}

    assert "unevidenced.jsonl" in by_path, "an unevidenced transcript was hidden"
    assert by_path["unevidenced.jsonl"]["attribution_state"] == ATTRIBUTION_CANNOT_DETERMINE
    assert by_path["normal.jsonl"]["attribution_state"] == ATTRIBUTION_EVIDENCED

    orphan_paths = {row["source_path"] for row in orphans["result"]}
    assert "unevidenced.jsonl" not in orphan_paths


def test_the_two_conditions_are_reported_by_different_fields(
    mixed_archive: Path,
) -> None:
    """One shows up as a null project, the other as an attribution state."""
    with closing(open_read_only(mixed_archive)) as conn:
        orphan = transcript_header(conn, 1)["result"]
        unevidenced = transcript_header(conn, 2)["result"]

    # The orphan: no project, but its host attribution is fine.
    assert orphan["project"] is None
    assert orphan["project_attribution"] == "none_declared"
    assert orphan["attribution_state"] == ATTRIBUTION_EVIDENCED

    # The unevidenced one: a real project, an unevidenced host.
    assert unevidenced["project"] is not None
    assert unevidenced["host"] is not None
    assert unevidenced["attribution_state"] == ATTRIBUTION_CANNOT_DETERMINE


def test_empty_unattributed_says_so_in_words(empty_archive: Path) -> None:
    """An ok/[] on this route carries a note, so nobody has to infer it."""
    with closing(open_read_only(empty_archive)) as conn:
        result = unattributed_for_corpus(conn, 1)
    assert result["result_status"] == RESULT_OK
    assert result["result"] == []
    assert result["meta"]["note"] == (
        "every transcript in this corpus resolved to a project"
    )


def test_hosts_always_publishes_the_orphan_count(empty_archive: Path) -> None:
    """Emitted even at 0, so "all attributed" differs from "never asked"."""
    with closing(open_read_only(empty_archive)) as conn:
        result = hosts(conn)
    assert result["meta"]["totals"]["transcripts_with_no_host_id"] == 0
    assert "transcripts_attributed_to_a_host" in result["meta"]["totals"]


# --- the constructor itself ------------------------------------------------


def test_envelope_rejects_an_unknown_status() -> None:
    """An invented status must not reach a client as a plausible string."""
    with pytest.raises(ValueError):
        envelope(result=[], result_status="fine")
    with pytest.raises(ValueError):
        envelope(result=[], result_status=RESULT_OK, scope_status="probably")


def test_envelope_rejects_an_unevaluated_entry_with_no_reason() -> None:
    """A dropped reason is how a cannot-determine decays into an ok."""
    with pytest.raises(ValueError):
        envelope(
            result=None,
            result_status=RESULT_CANNOT_DETERMINE,
            unevaluated=[{"subject": "cursor"}],
        )
