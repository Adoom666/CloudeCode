"""Version-pinned export goldens: the bytes, and the schema they belong to.

WHAT A GOLDEN IS FOR HERE. The fixture suite proves reassembly is CORRECT
against hand-authored lines. This one proves it has not MOVED - that the
bytes a given shape produces today are the bytes recorded against schema
v17, and that nobody changed the serializer, the style table or the
appearance-key split without a human noticing.

THE VERSION IS PART OF THE GOLDEN, ON PURPOSE.
:func:`test_golden_is_pinned_to_the_current_schema_version` fails the
moment ``CURRENT_SCHEMA_VERSION`` moves. That is not friction for its own
sake: a schema bump is exactly when somebody should re-confirm that these
bytes are still the right expectation, and a golden that silently follows
the version along would answer that question by assuming it. The failure
message says how to re-bless.

RE-BLESSING IS DELIBERATE AND LIVES OUTSIDE PYTEST. There is no env var
that regenerates these on the spot, because the fastest route past a red
golden must not be to overwrite it. Run:

    ./venv/bin/python3 scripts/bless_jsonl_export_golden.py --show
    ./venv/bin/python3 scripts/bless_jsonl_export_golden.py \\
        --i-have-reviewed-the-diff

NOT CIRCULAR. The pinned bytes are the hand-authored literals from
``tests/jsonl_shape_fixture_data``, hashed with ``hashlib`` by the bless
script; the reassembly code is never consulted about what the answer
should be. So these goldens can disagree with the code, which is the only
condition under which they are worth keeping.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_model_export import export_transcript
from src.core.message_model_ingest import SourceLine, ingest_lines
from src.core.message_model_serialize import (
    APPEARANCE_KEYS,
    KEY_ORDER_NOT_AN_OBJECT,
    SERIALIZER_STYLES,
    render_line,
    sha256_text,
)
from tests.jsonl_shape_fixture_data import FIXTURES, split_line

GOLDEN_PATH: Path = (
    Path(__file__).resolve().parent / "fixtures" / "jsonl_export_golden.json"
)

#: The golden layout this module reads.
SUPPORTED_GOLDEN_FORMAT_VERSION: int = 1

REBLESS_HINT: str = (
    "Re-bless deliberately, never automatically:\n"
    "  ./venv/bin/python3 scripts/bless_jsonl_export_golden.py --show\n"
    "  ./venv/bin/python3 scripts/bless_jsonl_export_golden.py "
    "--i-have-reviewed-the-diff"
)


@pytest.fixture(scope="module")
def golden() -> Dict[str, Any]:
    """The pinned golden document.

    Inputs: none (pytest fixture).
    Output: dict.
    Example: golden["schema_version"] -> 17
    """
    with GOLDEN_PATH.open(encoding="utf-8") as handle:
        document = json.load(handle)
    assert document["golden_format_version"] == (
        SUPPORTED_GOLDEN_FORMAT_VERSION
    ), f"golden file layout is newer than this reader. {REBLESS_HINT}"
    return document


def test_golden_is_pinned_to_the_current_schema_version(golden):
    """The golden names the schema version the code is actually at."""
    assert golden["schema_version"] == CURRENT_SCHEMA_VERSION, (
        f"the goldens are pinned to schema v{golden['schema_version']} but "
        f"the code is at v{CURRENT_SCHEMA_VERSION}. This is deliberate "
        "friction: confirm by hand that every pinned line is still the "
        "correct expectation under the new schema, THEN re-bless.\n"
        + REBLESS_HINT
    )


def test_golden_pins_the_serializer_style_table(golden):
    """The four styles, their separators and their escaping have not moved.

    Description: a change to a separator tuple would move the bytes of
      every line written in that style. Pinning the table names the cause
      directly instead of leaving 30 byte mismatches to be diagnosed.
    """
    live = [
        {"name": name, "separators": list(separators),
         "ensure_ascii": ensure_ascii}
        for name, separators, ensure_ascii in SERIALIZER_STYLES
    ]
    assert live == golden["serializer_styles"], (
        "the serializer style table changed. Every line written in a "
        f"changed style now serializes differently.\n{REBLESS_HINT}"
    )


def test_golden_pins_the_appearance_key_split(golden):
    """The keys lifted into the envelope have not changed.

    Description: adding an appearance key repartitions every stored line
      between body and envelope. Reassembly would still round-trip, so no
      byte test catches it - this does.
    """
    assert list(APPEARANCE_KEYS) == golden["appearance_keys"], (
        f"APPEARANCE_KEYS changed, repartitioning every stored line.\n"
        f"{REBLESS_HINT}"
    )


def test_golden_covers_exactly_the_current_fixture_set(golden):
    """No fixture is unpinned and no pinned entry has been deleted."""
    pinned = {entry["name"] for entry in golden["entries"]}
    live = {fixture.name for fixture in FIXTURES}
    assert pinned == live, (
        f"unpinned fixtures: {sorted(live - pinned)}; "
        f"stale golden entries: {sorted(pinned - live)}\n{REBLESS_HINT}"
    )


def test_every_pinned_shape_still_renders_to_its_pinned_bytes(golden):
    """Production reassembly reproduces every pinned line, hash and length.

    Description: the regression assertion proper. Renders through the
      real ``render_line`` and compares against the pinned literal, its
      sha256 and its byte length - three comparisons, because a hash
      mismatch alone does not say whether the output was even the right
      size.
    """
    by_name = {fixture.name: fixture for fixture in FIXTURES}
    moved = []
    for entry in golden["entries"]:
        fixture = by_name[entry["name"]]
        body, envelope = split_line(fixture.line, fixture.key_order)
        key_order = (
            KEY_ORDER_NOT_AN_OBJECT if entry["key_order"] is None
            else entry["key_order"]
        )
        produced = render_line(body, envelope, key_order, entry["style"])
        raw = produced.encode("utf-8")
        if (produced != entry["line"]
                or hashlib.sha256(raw).hexdigest() != entry["line_sha256"]
                or len(raw) != entry["line_byte_length"]):
            moved.append(
                f"  {entry['name']} ({entry['covers']})\n"
                f"    pinned:   {entry['line']!r} "
                f"[{entry['line_byte_length']} bytes]\n"
                f"    produced: {produced!r} [{len(raw)} bytes]"
            )
    assert not moved, (
        f"{len(moved)} pinned shape(s) no longer render to their recorded "
        f"bytes:\n" + "\n".join(moved) + "\n" + REBLESS_HINT
    )


# ---- end-to-end: the same guarantee through a real database ------------

#: A fixed synthetic transcript, hand-authored, used to pin the WHOLE-FILE
#: export hash rather than only per-line bytes. Ingest, storage and
#: reassembly all sit between these bytes and the export, so this catches
#: a defect in any of them - the per-line tests above cannot see ingest at
#: all.
END_TO_END_LINES = (
    '{"parentUuid":null,"isSidechain":false,"type":"user",'
    '"uuid":"uuid-e2e-1","timestamp":"2026-01-01T00:00:00.000Z",'
    '"sessionId":"session-e2e"}',
    '{"parentUuid":"uuid-e2e-1","isSidechain":true,'
    '"agentId":"a00000000000000e2","type":"assistant","uuid":"uuid-e2e-2",'
    '"timestamp":"2026-01-01T00:00:01.000Z","sessionId":"session-e2e",'
    '"message":{"role":"assistant","model":"synthetic-model","content":'
    '[{"type":"text","text":"café \\u0001 /a/b"}]}}',
    '{"type":"summary","summary":"synthetic","leafUuid":"uuid-e2e-2"}',
)


def test_end_to_end_ingest_and_export_reproduces_the_source_bytes():
    """A transcript ingested and exported comes back byte-identical.

    Description: the product guarantee stated in one assertion, against a
      hand-authored source. The expected value is the source text itself,
      so nothing about the expectation comes from the code under test.
    """
    source = "\n".join(END_TO_END_LINES) + "\n"
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    with connection:
        result = ingest_lines(
            connection, source_ref="golden-e2e", session_ref="session-e2e",
            lines=[SourceLine(text=text) for text in END_TO_END_LINES],
            has_trailing_newline=True, line_ending="LF",
        )
    exported = export_transcript(connection, result.transcript_id)
    assert exported.text == source, (
        "export did not reproduce the hand-authored source bytes"
    )
    assert exported.verified, (
        f"export reported unverified: {exported.failures()}"
    )
    assert sha256_text(exported.text) == sha256_text(source)
    connection.close()
