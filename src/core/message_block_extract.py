"""Turning one stored body into its content blocks. NO DATABASE HERE.

This module is pure: JSON text in, a verdict plus a list of blocks out.
That is deliberate. The projection rules are the part most likely to be
argued with and changed, so they live somewhere a test can exercise them
without a schema, a connection or a corpus.

THE VERDICT IS THE POINT, NOT THE BLOCKS. Every body resolves to exactly
one status, and two of the five mean COULD NOT EVALUATE rather than
"nothing here". A caller that only reads the block list sees the same
empty list for a progress record (correct, it has no message) and for a
body whose JSON is corrupt (not correct, and the difference matters).
:class:`BlockExtraction` refuses to let those look alike.

WHAT IS PROJECTED AS TEXT, AND WHAT IS NOT.

  text              ``text``                       125 MB corpus-wide
  thinking          ``thinking``                    46 MB
  tool_use          ``input``, re-rendered as JSON 348 MB
  tool_result       ``content`` when it is a str,  792 MB
                    else the text of its sub-blocks
  image             NOTHING. ``source`` is base64 bytes.
  document          NOTHING. ``source`` is a payload.
  fallback          NOTHING. It carries only ``from``/``to``.

An image's base64 source is the single largest per-block value in the
corpus and no query wants it as TEXT. Projecting it would copy the
biggest values in the database into a second column to serve a search
that would never match. ``text`` is NULL for those types, which is not
the same as ``''``: NULL says "this type carries no projectable text"
and ``''`` says "it carries text and the text is empty". Both occur.

TOOL_USE INPUT IS RE-RENDERED, NOT SLICED OUT OF THE SOURCE BYTES. The
projection is ``json.dumps(input)``, so its key order and spacing are
this module's, not the source's. That is fine BECAUSE THIS TABLE IS NOT
AUTHORITATIVE - byte-exact export reads ``body_json`` and never this.
Slicing the original substring would have been a second, subtly
different serialiser to keep in step with the export one, for no gain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.core.message_block_ddl import (
    DERIVED_TYPE_NON_OBJECT,
    DERIVED_TYPE_STRING_CONTENT,
    DERIVED_TYPE_UNTYPED,
    EXTRACTOR_VERSION,
    STATUS_BLOCKS_EXTRACTED,
    STATUS_CONTENT_STRING,
    STATUS_NO_MESSAGE_CONTENT,
    STATUS_UNEXPECTED_CONTENT_SHAPE,
    STATUS_UNPARSEABLE_BODY,
)

#: Block types whose payload is binary or structural, never text. Listed
#: rather than inferred so adding a type is a deliberate edit here and
#: not an accidental NULL somewhere downstream.
NON_TEXT_BLOCK_TYPES: Tuple[str, ...] = ("image", "document", "fallback")


@dataclass(frozen=True)
class ContentBlock:
    """One extracted content block, ready to be stored.

    Attributes:
        seq: 0-based position within ``message.content``.
        block_type: the source ``type`` string, or one of the derived
            ``_``-prefixed types when the source had none.
        text: the human-readable projection, or None when this block
            type carries no projectable text at all.
        tool_name: the tool being called. tool_use blocks only.
        tool_use_id: a tool_use block's own id, or the id a tool_result
            answers. None for every other type.
        is_error: a tool_result's error flag. None means THE KEY WAS
            ABSENT, which is 39.6% of tool_result blocks.
    """

    seq: int
    block_type: str
    text: Optional[str]
    tool_name: Optional[str] = None
    tool_use_id: Optional[str] = None
    is_error: Optional[int] = None

    @property
    def text_length(self) -> int:
        """Character count of the projection, 0 when there is none.

        Inputs: none.
        Output: int.
        Example: ContentBlock(0, "text", "abc").text_length -> 3
        """
        return len(self.text) if self.text is not None else 0


@dataclass(frozen=True)
class BlockExtraction:
    """The complete verdict for one body.

    Attributes:
        status: one of message_block_ddl.BLOCK_STATUSES. Exactly one.
        blocks: the extracted blocks, possibly empty.
        detail: why, when the status is not a plain success. None
            otherwise.
        extractor_version: the projection vocabulary these blocks were
            produced under.
    """

    status: str
    blocks: List[ContentBlock] = field(default_factory=list)
    detail: Optional[str] = None
    extractor_version: int = EXTRACTOR_VERSION

    @property
    def could_not_evaluate(self) -> bool:
        """Whether this verdict is an inability to look, not an answer.

        Inputs: none.
        Output: bool - True for unparseable_body and
          unexpected_content_shape, False for the three real answers.
        Example: BlockExtraction("no_message_content").could_not_evaluate
          -> False
        """
        return self.status in (
            STATUS_UNPARSEABLE_BODY,
            STATUS_UNEXPECTED_CONTENT_SHAPE,
        )


def _project_tool_result_content(content: Any) -> Optional[str]:
    """Text of a tool_result's ``content``, which has two measured shapes.

    Description: 405,369 of 452,443 tool_result blocks carry a plain
      string and 47,074 carry a list of sub-blocks. A list's text
      sub-blocks are joined with newlines; its image sub-blocks
      contribute nothing, for the same reason a top-level image block
      does.
    Inputs: content (Any) - the block's ``content`` value.
    Output: str | None - None when there is no projectable text at all.
    Example: _project_tool_result_content("out") -> "out"
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for sub in content:
            if isinstance(sub, str):
                parts.append(sub)
            elif isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(sub.get("text") or "")
        return "\n".join(parts) if parts else None
    return None


def project_block(block: Dict[str, Any]) -> Optional[str]:
    """The human-readable text of one content block.

    Description: the single authority for what "the text of a block"
      means. Changing it means bumping
      message_block_ddl.EXTRACTOR_VERSION, because stored rows produced
      under the old rules would otherwise sit in the same column as rows
      produced under the new ones with nothing saying which is which.
    Inputs: block (dict) - one element of ``message.content``.
    Output: str | None - None when this type carries no text.
    Example: project_block({"type": "text", "text": "hi"}) -> "hi"
    """
    btype = block.get("type")
    if btype == "text":
        value = block.get("text")
        return value if isinstance(value, str) else None
    if btype == "thinking":
        value = block.get("thinking")
        return value if isinstance(value, str) else None
    if btype == "tool_use":
        payload = block.get("input")
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)
    if btype == "tool_result":
        return _project_tool_result_content(block.get("content"))
    return None


def _block_from_object(seq: int, block: Dict[str, Any]) -> ContentBlock:
    """Build one ContentBlock from one JSON object in the content array.

    Inputs: seq (int) - 0-based position. block (dict) - the element.
    Output: ContentBlock.
    Example: _block_from_object(0, {"type": "text", "text": "a"}).seq -> 0
    """
    raw_type = block.get("type")
    btype = raw_type if isinstance(raw_type, str) else DERIVED_TYPE_UNTYPED
    tool_name: Optional[str] = None
    tool_use_id: Optional[str] = None
    is_error: Optional[int] = None
    if btype == "tool_use":
        name = block.get("name")
        tool_name = name if isinstance(name, str) else None
        own_id = block.get("id")
        tool_use_id = own_id if isinstance(own_id, str) else None
    elif btype == "tool_result":
        answered = block.get("tool_use_id")
        tool_use_id = answered if isinstance(answered, str) else None
        flag = block.get("is_error")
        if flag is not None:
            is_error = 1 if flag else 0
    return ContentBlock(
        seq=seq,
        block_type=btype,
        text=project_block(block),
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        is_error=is_error,
    )


def extract_blocks(body_json: str) -> BlockExtraction:
    """Classify one stored body and extract its content blocks.

    Description: the whole decision tree, in the order the shapes were
      measured to occur. Every path returns a BlockExtraction with
      exactly one status, and no path returns an empty list without also
      saying WHY it is empty.
    Inputs: body_json (str) - the exact text of message_bodies.body_json.
    Output: BlockExtraction.
    Raises: nothing. A body this function cannot read is a verdict, not
      an exception - the caller is a bulk backfill and one bad row must
      not stop 2.4M good ones.
    Example: extract_blocks('{"message":{"content":"hi"}}').status
      -> "content_string"
    """
    try:
        doc = json.loads(body_json)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return BlockExtraction(
            status=STATUS_UNPARSEABLE_BODY,
            detail=f"json.loads failed: {type(exc).__name__}: {exc}"[:500],
        )
    if not isinstance(doc, dict):
        return BlockExtraction(
            status=STATUS_UNEXPECTED_CONTENT_SHAPE,
            detail=f"body is a JSON {type(doc).__name__}, not an object",
        )
    message = doc.get("message")
    if not isinstance(message, dict) or "content" not in message:
        return BlockExtraction(status=STATUS_NO_MESSAGE_CONTENT)
    content = message["content"]
    if isinstance(content, str):
        return BlockExtraction(
            status=STATUS_CONTENT_STRING,
            blocks=[
                ContentBlock(
                    seq=0,
                    block_type=DERIVED_TYPE_STRING_CONTENT,
                    text=content,
                )
            ],
        )
    if not isinstance(content, list):
        return BlockExtraction(
            status=STATUS_UNEXPECTED_CONTENT_SHAPE,
            detail=(
                "message.content is a JSON "
                f"{type(content).__name__}, expected array or string"
            ),
        )
    blocks: List[ContentBlock] = []
    for seq, element in enumerate(content):
        if isinstance(element, dict):
            blocks.append(_block_from_object(seq, element))
        else:
            blocks.append(
                ContentBlock(
                    seq=seq,
                    block_type=DERIVED_TYPE_NON_OBJECT,
                    text=element if isinstance(element, str) else None,
                )
            )
    return BlockExtraction(status=STATUS_BLOCKS_EXTRACTED, blocks=blocks)
