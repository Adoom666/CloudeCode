"""Every distinct JSONL shape in the archive still exports byte-exact.

THE LOAD-BEARING TEST. The product guarantee is that a conversation
exported from the database is byte-for-byte the .jsonl that was ingested.
That guarantee was measured once, on 2026-08-31, across 1,347 distinct
structural shapes. A measurement taken once is a historical fact, not a
defence; this module re-measures it on every run so a change to the
reassembly path cannot quietly stop being true.

ALL 1,347, NEVER A SAMPLE. 464 of the signatures occur fewer than ten
times and together are 0.045 percent of appearances, so a random sample
of any practical size misses essentially all of them - and they are
disproportionately the odd shapes most likely to break reassembly. The
manifest exists precisely so coverage can be enumerated instead of
sampled.

THE REAL CODE, NOT A RE-IMPLEMENTATION. Rendering goes through
``message_model_export._render_row`` and ``sha256_text``, the same
functions the product's export path calls. A second renderer written for
the test could agree with itself while both were wrong, which is the one
failure a fidelity test must not be capable of.

THE THIRD OUTCOME. This corpus does not exist in CI and will not exist on
another machine. A missing corpus is therefore reported as
COULD-NOT-EVALUATE through :func:`record_not_evaluated`, which both
supplies the skip reason and files a ledger entry that the
terminal-summary hook prints at the end of EVERY run. The skip is not
allowed to be silent: a test that cannot fail because its data is absent,
sitting quietly in a green summary, is exactly the false green this
archive's own hazard list was written about.

PRIVACY. No message text is read into an assertion or a failure message.
Failures are reported by signature id and coordinate only.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from src.core.message_model_export import _render_row
from src.core.message_model_serialize import sha256_text
from tests.jsonl_shape_rows import fetch_exemplar
from tests.jsonl_shape_support import (
    corpus_path,
    load_manifest,
    open_corpus_readonly,
    record_not_evaluated,
)

#: The guarantee this module proves, named once so the skip reason, the
#: ledger and the docstring cannot drift apart.
GUARANTEE: str = (
    "byte-for-byte export fidelity across every distinct JSONL shape "
    "in the archive"
)


@pytest.fixture(scope="module")
def corpus():
    """A proven read-only handle on the shape corpus, or a loud skip.

    Description: module-scoped so the 1,347 exemplar reads share one
      connection and one page cache; the whole sweep was measured at
      about one second that way.
    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: corpus.execute("SELECT 1").fetchone() -> (1,)
    """
    path = corpus_path()
    if path is None:
        pytest.skip(record_not_evaluated(
            GUARANTEE,
            "no corpus database is available on this machine, so NONE of "
            "the 1,347 shapes were round-tripped. Set CLOUDE_FIDELITY_DB "
            "to a message-model database to verify them."
        ))
    connection = open_corpus_readonly(path)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def manifest():
    """The shape manifest, loaded once per module.

    Inputs: none (pytest fixture).
    Output: dict.
    Example: manifest["totals"]["distinct_signatures"] -> 1347
    """
    return load_manifest()


def _verify_one(conn, entry: Dict[str, object]) -> Tuple[str, str]:
    """Round-trip one signature's exemplar and judge the result.

    Description: renders through the production ``_render_row``, then
      compares BOTH the sha256 and the byte length against what was
      stored at ingest. Two comparisons rather than one: the hash proves
      the bytes, and the length makes a hash mismatch legible by saying
      whether the reconstruction was even the right size.
    Inputs: conn (sqlite3.Connection), entry (dict - one manifest
      signature entry).
    Output: (verdict, detail) where verdict is 'match', 'mismatch',
      'cannot_render' or 'missing_exemplar'.
    Example: _verify_one(conn, entry) -> ("match", "")
    """
    exemplar = entry["exemplar"]
    row = fetch_exemplar(
        conn, exemplar["transcript_id"], exemplar["line_no"],
    )
    if row is None:
        return "missing_exemplar", (
            f"no row at transcript {exemplar['transcript_id']} "
            f"line {exemplar['line_no']}"
        )
    text, detail = _render_row(row)
    if text is None:
        return "cannot_render", detail
    actual_sha = sha256_text(text)
    actual_len = len(text.encode("utf-8"))
    if actual_sha != row["line_sha256"]:
        return "mismatch", (
            f"sha256 differs (expected length {row['line_byte_length']}, "
            f"produced {actual_len})"
        )
    if actual_len != row["line_byte_length"]:
        return "mismatch", (
            f"sha256 matched but byte length differs: stored "
            f"{row['line_byte_length']}, produced {actual_len}"
        )
    return "match", ""


def test_every_signature_exemplar_round_trips_byte_exact(corpus, manifest):
    """All 1,347 shapes reproduce their stored hash and byte length."""
    entries: List[Dict[str, object]] = manifest["signatures"]
    assert entries, "manifest carries no signatures - nothing was checked"
    failures: List[str] = []
    verdicts: Dict[str, int] = {}
    for entry in entries:
        verdict, detail = _verify_one(corpus, entry)
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if verdict != "match":
            exemplar = entry["exemplar"]
            failures.append(
                f"  signature {entry['signature_id']} "
                f"(count {entry['count']}, transcript "
                f"{exemplar['transcript_id']} line {exemplar['line_no']}): "
                f"{verdict} - {detail}"
            )
    assert not failures, (
        f"{len(failures)} of {len(entries)} shapes no longer export "
        "byte-exact:\n" + "\n".join(failures[:20])
    )
    assert verdicts.get("match") == len(entries), (
        f"expected every shape to match, got {verdicts}"
    )


def test_the_fidelity_comparison_can_actually_fail(corpus, manifest):
    """A negative control: a mutated render must NOT match its hash.

    Description: 1,347 passes mean nothing from a comparison never shown
      capable of returning a failure. This renders one real exemplar,
      confirms it matches, then flips one character and confirms the same
      comparison rejects it.
    """
    entry = manifest["signatures"][0]
    exemplar = entry["exemplar"]
    row = fetch_exemplar(
        corpus, exemplar["transcript_id"], exemplar["line_no"],
    )
    assert row is not None, "the control exemplar itself is missing"
    text, _ = _render_row(row)
    assert text is not None, "the control exemplar could not be rendered"
    assert sha256_text(text) == row["line_sha256"], (
        "the unmutated control did not match, so the mutation below would "
        "prove nothing"
    )
    mutated = ("X" if text[0] != "X" else "Y") + text[1:]
    assert sha256_text(mutated) != row["line_sha256"], (
        "a one-character mutation still matched the stored hash - the "
        "comparison cannot detect a difference and every pass above is void"
    )


def test_manifest_totals_describe_the_signature_list(manifest):
    """The manifest's own totals agree with the list it carries.

    Description: runs without a corpus, so CI still checks that the
      fixture is internally consistent even when nothing can be
      round-tripped against live data.
    """
    entries = manifest["signatures"]
    totals = manifest["totals"]
    assert len(entries) == totals["distinct_signatures"]
    assert len({e["signature_id"] for e in entries}) == len(entries), (
        "two manifest entries share a signature id"
    )
    assert sum(1 for e in entries if e["count"] < 10) == (
        totals["rare_signatures_lt10"]
    )
    assert totals["roundtrip_fail"] == 0, (
        "the manifest was generated with shapes that already failed to "
        "round-trip; this suite must not be built on it"
    )
    known = {k["digest"] for k in manifest["key_orders"]}
    dangling = sorted({
        e["dimensions"]["key_order_digest"] for e in entries
    } - known)
    assert not dangling, f"signatures reference unknown key orders: {dangling}"
