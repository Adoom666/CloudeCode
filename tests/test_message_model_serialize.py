"""Tests for the identity/envelope split and the byte-exact round trip.

Every assertion here is about BYTES, not about shape. The model's whole
claim is that a stored record reproduces its original line exactly, so a
test that checked "the keys are all there" would pass on the exact bug
that broke this module during development: bodies stored with their
nested keys sorted, which is valid JSON, has every key, means the same
thing, and is the wrong bytes.
"""

from __future__ import annotations

import json

import pytest

from src.core.message_model_serialize import (
    APPEARANCE_KEYS,
    KEY_ORDER_NOT_AN_OBJECT,
    SERIALIZER_STYLES,
    STYLE_NAMES,
    canonical_json,
    detect_style,
    identity_key,
    join_lines,
    parse_line,
    render_line,
    render_with_style,
    scalar_fields,
    session_ref_scheme,
    sha256_text,
    split_lines,
    split_record,
    stored_body_json,
)

REAL_SHAPED_LINE = (
    '{"parentUuid":"p1","isSidechain":false,"userType":"external",'
    '"cwd":"/Users/j","sessionId":"s1","version":"2.0.76",'
    '"gitBranch":"main","type":"assistant",'
    '"message":{"role":"assistant","model":"claude-opus-5",'
    '"content":[{"type":"text","text":"hi"}]},'
    '"uuid":"u1","timestamp":"2026-01-01T00:00:00.000Z"}'
)


def _round_trip(line: str) -> str:
    """Split a line, then rebuild it, the way ingest and export do.

    Description: the two halves of the model exercised end to end without
      a database in the way.
    Inputs: line (str).
    Output: str - the rebuilt line.
    Example: _round_trip('{"a":1}') -> '{"a":1}'
    """
    value = json.loads(line)
    split = split_record(value)
    style = detect_style(value, line)
    assert style is not None, "no registered style reproduced the line"
    return render_line(split.body, split.envelope, split.key_order, style)


# ---- the round trip ----------------------------------------------------

def test_a_real_shaped_line_round_trips_byte_exact():
    assert _round_trip(REAL_SHAPED_LINE) == REAL_SHAPED_LINE


def test_nested_key_order_survives_the_round_trip():
    """The bug this test exists for: storing the body canonically sorted
    its NESTED keys too, so a message written role/model/content came
    back content/model/role - same meaning, wrong bytes."""
    line = '{"uuid":"u","message":{"role":"a","model":"m","content":"c"}}'
    assert _round_trip(line) == line


def test_an_envelope_key_in_the_middle_goes_back_to_the_middle():
    line = '{"a":1,"isSidechain":true,"b":2}'
    assert _round_trip(line) == line


def test_non_ascii_round_trips():
    line = '{"text":"café — done"}'
    assert _round_trip(line) == line


def test_every_registered_style_is_detected_for_its_own_output():
    value = {"b": 1, "a": [1, 2], "c": {"d": "e"}}
    for name in STYLE_NAMES:
        rendered = render_with_style(value, name)
        assert detect_style(value, rendered) is not None


def test_detect_style_returns_none_rather_than_guessing():
    """The third outcome. A line no style reproduces must be reported as
    such, never assigned a style that does not actually work."""
    assert detect_style({"a": 1}, '{ "a" : 1 }') is None


# ---- the split ---------------------------------------------------------

def test_only_the_measured_appearance_keys_leave_the_body():
    split = split_record(json.loads(REAL_SHAPED_LINE))
    assert set(split.envelope) == {"isSidechain"}
    for key in APPEARANCE_KEYS:
        assert key not in split.body


def test_key_order_difference_shares_a_meaning_hash_not_a_bytes_hash():
    """Two hashes answering two questions. Order-insensitive for 'is this
    the same message', order-sensitive for 'can this share a stored
    row'."""
    one = split_record({"a": 1, "b": 2})
    two = split_record({"b": 2, "a": 1})
    assert one.body_sha256 == two.body_sha256
    assert one.body_bytes_sha256 != two.body_bytes_sha256


def test_a_genuinely_different_body_differs_on_both_hashes():
    one = split_record({"a": 1})
    two = split_record({"a": 2})
    assert one.body_sha256 != two.body_sha256
    assert one.body_bytes_sha256 != two.body_bytes_sha256


def test_a_non_object_line_is_kept_whole_and_marked_as_such():
    split = split_record(["a", "b"])
    assert split.key_order == KEY_ORDER_NOT_AN_OBJECT
    assert split.envelope == {}
    assert _round_trip('["a","b"]') == '["a","b"]'


def test_reassembly_refuses_to_emit_a_short_object():
    """A key named in the stored order that neither part holds is a
    corrupt row. Raising names the problem; silently emitting a shorter
    object would fail the hash check with no explanation."""
    from src.core.message_model_serialize import reassemble
    with pytest.raises(KeyError):
        reassemble({"a": 1}, {}, ["a", "missing"])


# ---- scalars and identity ---------------------------------------------

def test_scalar_fields_reaches_into_the_nested_message_for_role_and_model():
    fields = scalar_fields(json.loads(REAL_SHAPED_LINE))
    assert fields["record_type"] == "assistant"
    assert fields["role"] == "assistant"
    assert fields["model"] == "claude-opus-5"
    assert fields["origin_session_ref"] == "s1"
    assert fields["parent_uuid"] == "p1"


def test_scalar_fields_returns_none_rather_than_a_default_for_absent_keys():
    fields = scalar_fields({})
    assert all(
        fields[key] is None
        for key in ("record_type", "role", "model", "compact_subtype",
                    "parent_uuid", "ts", "origin_session_ref", "message_uuid")
    )


def test_both_compaction_shapes_produce_a_compact_subtype():
    boundary = scalar_fields({"type": "system", "subtype": "compact_boundary"})
    summary = scalar_fields({"type": "user", "isCompactSummary": True})
    assert boundary["compact_subtype"] == "compact_boundary"
    assert summary["compact_subtype"] == "isCompactSummary"
    assert boundary["is_compact_boundary"] == 1
    assert summary["is_compact_boundary"] == 1


def test_an_unrelated_system_subtype_is_not_called_a_compact_subtype():
    assert scalar_fields(
        {"type": "system", "subtype": "something_else"}
    )["compact_subtype"] is None


def test_identity_key_has_no_null_to_be_exempted_by():
    assert identity_key(None, "ab") == ":ab"
    assert identity_key("u", "ab") == "u:ab"
    assert identity_key(None, "ab") != identity_key("", "ba")


# ---- session identity schemes -----------------------------------------

def test_both_measured_agent_prefixes_are_recognised():
    """The brief named only 'agent-'. The live sessions table on
    2026-08-29 holds 'agent:' on 17,996 rows and 'agent-' on 224."""
    assert session_ref_scheme("agent:a7b0a2e") == "agent"
    assert session_ref_scheme("agent-a00fdb4") == "agent"


def test_a_session_uuid_is_not_mistaken_for_an_agent_ref():
    assert session_ref_scheme("07e1cc0e-8a47-4029-8cfc-554f883ba28f") == "uuid"


# ---- line and file splitting ------------------------------------------

def test_split_and_join_are_exact_inverses():
    for text in ("", "a\n", "a\nb\n", "a\nb", "a\n\nb\n", "\n"):
        lines, trailing = split_lines(text)
        assert join_lines(lines, trailing) == text


def test_a_blank_line_in_the_middle_is_kept():
    lines, _ = split_lines("a\n\nb\n")
    assert lines == ["a", "", "b"]


def test_parse_line_names_its_three_outcomes():
    assert parse_line("   ")[0] == "blank"
    assert parse_line("{nope")[0] == "invalid_json"
    assert parse_line('{"a":1}') == ("ok", {"a": 1})


def test_canonical_and_stored_renderings_differ_where_it_matters():
    value = {"b": 1, "a": 2}
    assert canonical_json(value) == '{"a":2,"b":1}'
    assert stored_body_json(value) == '{"b":1,"a":2}'


def test_sha256_text_is_over_utf8_bytes():
    assert sha256_text("a") == sha256_text("a")
    assert sha256_text("é") != sha256_text("e")


def test_style_table_has_no_duplicate_names():
    names = [name for name, _, _ in SERIALIZER_STYLES]
    assert len(names) == len(set(names))
