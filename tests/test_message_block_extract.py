"""Projection rules and the three-outcome classification, without a DB.

These are the tests that decide what "the text of a block" means, so
they are the ones that must fail loudly when someone changes the rule
without meaning to.
"""

from __future__ import annotations

import json

import pytest

from src.core.message_block_ddl import (
    DERIVED_TYPE_NON_OBJECT,
    DERIVED_TYPE_STRING_CONTENT,
    DERIVED_TYPE_UNTYPED,
    STATUS_BLOCKS_EXTRACTED,
    STATUS_CONTENT_STRING,
    STATUS_NO_MESSAGE_CONTENT,
    STATUS_UNEXPECTED_CONTENT_SHAPE,
    STATUS_UNPARSEABLE_BODY,
)
from src.core.message_block_extract import extract_blocks, project_block


def _body(content: object) -> str:
    """Wrap a content value in a minimal body record.

    Inputs: content (object) - the value for message.content.
    Output: str - the body as stored JSON text.
    Example: _body([]) -> '{"message": {"content": []}}'
    """
    return json.dumps({"message": {"content": content}})


# ---------------------------------------------------------------------------
# The three outcomes. A body that could not be evaluated must never look
# like a body that legitimately has nothing.
# ---------------------------------------------------------------------------


def test_unparseable_body_is_recorded_not_silently_empty():
    result = extract_blocks("{not json at all")
    assert result.status == STATUS_UNPARSEABLE_BODY
    assert result.could_not_evaluate is True
    assert result.detail is not None and "json.loads failed" in result.detail
    assert result.blocks == []


def test_body_with_no_message_key_is_an_answer_not_a_failure():
    result = extract_blocks(json.dumps({"type": "progress", "data": {"a": 1}}))
    assert result.status == STATUS_NO_MESSAGE_CONTENT
    assert result.could_not_evaluate is False
    assert result.blocks == []


def test_unexpected_content_shape_is_could_not_evaluate():
    result = extract_blocks(_body(12345))
    assert result.status == STATUS_UNEXPECTED_CONTENT_SHAPE
    assert result.could_not_evaluate is True
    assert "int" in result.detail


def test_non_object_body_is_could_not_evaluate():
    result = extract_blocks("[1, 2, 3]")
    assert result.status == STATUS_UNEXPECTED_CONTENT_SHAPE
    assert result.could_not_evaluate is True


def test_empty_content_array_is_extracted_with_zero_blocks():
    result = extract_blocks(_body([]))
    assert result.status == STATUS_BLOCKS_EXTRACTED
    assert result.could_not_evaluate is False
    assert result.blocks == []


# ---------------------------------------------------------------------------
# Projection rules, one per measured block type.
# ---------------------------------------------------------------------------


def test_text_block_projects_its_text():
    assert project_block({"type": "text", "text": "hello"}) == "hello"


def test_thinking_block_projects_its_thinking_not_its_signature():
    projected = project_block(
        {"type": "thinking", "thinking": "reasoning", "signature": "sig"}
    )
    assert projected == "reasoning"
    assert "sig" not in projected


def test_tool_use_projects_its_input_as_json():
    projected = project_block(
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}}
    )
    assert json.loads(projected) == {"command": "ls"}


def test_tool_result_projects_a_string_content_verbatim():
    assert project_block(
        {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
    ) == "output"


def test_tool_result_joins_the_text_of_a_list_content():
    projected = project_block({
        "type": "tool_result", "tool_use_id": "t1",
        "content": [{"type": "text", "text": "one"},
                    {"type": "text", "text": "two"}],
    })
    assert projected == "one\ntwo"


def test_tool_result_list_does_not_project_an_image_sub_block():
    projected = project_block({
        "type": "tool_result", "tool_use_id": "t1",
        "content": [{"type": "text", "text": "keep"},
                    {"type": "image",
                     "source": {"data": "AAAABBBBCCCCDDDD"}}],
    })
    assert projected == "keep"
    assert "AAAABBBBCCCCDDDD" not in projected


@pytest.mark.parametrize("payload", [
    {"type": "image", "source": {"type": "base64", "data": "SECRETBYTES"}},
    {"type": "document", "source": {"data": "DOCBYTES"}},
    {"type": "fallback", "from": "a", "to": "b"},
])
def test_binary_and_structural_types_project_no_text_at_all(payload):
    assert project_block(payload) is None


def test_none_text_is_distinct_from_empty_text():
    empty = extract_blocks(_body([{"type": "text", "text": ""}])).blocks[0]
    absent = extract_blocks(
        _body([{"type": "image", "source": {"data": "x"}}])
    ).blocks[0]
    assert empty.text == "" and empty.text_length == 0
    assert absent.text is None and absent.text_length == 0
    assert empty.text != absent.text


# ---------------------------------------------------------------------------
# Tool field mapping, including the measured 39.6% of tool_result blocks
# that carry no is_error key at all.
# ---------------------------------------------------------------------------


def test_tool_use_carries_name_and_its_own_id():
    block = extract_blocks(_body([
        {"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}
    ])).blocks[0]
    assert block.tool_name == "Agent"
    assert block.tool_use_id == "toolu_1"
    assert block.is_error is None


def test_tool_result_carries_the_id_it_answers_and_no_tool_name():
    block = extract_blocks(_body([
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}
    ])).blocks[0]
    assert block.tool_use_id == "toolu_1"
    assert block.tool_name is None


def test_absent_is_error_stays_none_and_is_not_defaulted_to_false():
    block = extract_blocks(_body([
        {"type": "tool_result", "tool_use_id": "t", "content": "x"}
    ])).blocks[0]
    assert block.is_error is None, (
        "an absent is_error must not be asserted as success"
    )


@pytest.mark.parametrize("flag,expected", [(True, 1), (False, 0)])
def test_present_is_error_is_stored_as_zero_or_one(flag, expected):
    block = extract_blocks(_body([
        {"type": "tool_result", "tool_use_id": "t", "content": "x",
         "is_error": flag}
    ])).blocks[0]
    assert block.is_error == expected


# ---------------------------------------------------------------------------
# Ordering and derived types.
# ---------------------------------------------------------------------------


def test_seq_is_zero_based_and_follows_source_order():
    blocks = extract_blocks(_body([
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {}},
        {"type": "text", "text": "c"},
    ])).blocks
    assert [b.seq for b in blocks] == [0, 1, 2]
    assert [b.block_type for b in blocks] == ["text", "tool_use", "text"]


def test_string_content_becomes_one_derived_block_marked_as_derived():
    result = extract_blocks(_body("just a prompt"))
    assert result.status == STATUS_CONTENT_STRING
    assert len(result.blocks) == 1
    assert result.blocks[0].block_type == DERIVED_TYPE_STRING_CONTENT
    assert result.blocks[0].block_type.startswith("_"), (
        "a type not present in the source JSON must be marked derived"
    )
    assert result.blocks[0].text == "just a prompt"
    assert result.blocks[0].seq == 0


def test_a_block_with_no_type_key_is_kept_under_a_derived_type():
    block = extract_blocks(_body([{"text": "orphan"}])).blocks[0]
    assert block.block_type == DERIVED_TYPE_UNTYPED


def test_a_non_object_element_is_kept_not_dropped():
    blocks = extract_blocks(_body(["bare string", 7])).blocks
    assert len(blocks) == 2, "an odd element must be recorded, never dropped"
    assert blocks[0].block_type == DERIVED_TYPE_NON_OBJECT
    assert blocks[0].text == "bare string"
    assert blocks[1].text is None


def test_extract_blocks_never_raises_on_hostile_input():
    for hostile in ("", "null", "0", '{"message": null}', '{"message": 3}',
                    '{"message": {"content": null}}'):
        result = extract_blocks(hostile)
        assert result.status in {
            STATUS_UNPARSEABLE_BODY, STATUS_NO_MESSAGE_CONTENT,
            STATUS_UNEXPECTED_CONTENT_SHAPE,
        }
