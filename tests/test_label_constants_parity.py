"""The client's copies of the label constants must equal the server's.

WHY THIS FILE EXISTS. ``client/js/session-label.js`` mirrors two values
out of ``src/core/session_label.py``: the ``cloude_`` tmux prefix it
strips for display, and the maximum label length an edit control draws as
its ``maxlength``. Neither is served over the wire - an editor needs its
own limit before it can draw itself, which is before any request could
tell it - so a copy is the only option available.

A copy is fine. An UNCHECKED copy is a second declaration waiting to
disagree, and the disagreement is silent in both directions:

  * client limit LOWER than the server's - the ``maxlength`` truncates
    what the user typed, stores the truncation, and reports nothing.
    Nobody sees an error because nothing errored. This is what the
    hardcoded 64 was doing against a server that accepts 200.
  * client limit HIGHER - the user types a label the client accepts and
    the server rejects, after the typing.

The first is worse, so the assertion is EQUALITY rather than an
inequality in the safe direction: "safe" here still means the user's text
is quietly altered.

The prefix has been mirrored since the resolver was written and has never
been checked at all. It is included here rather than left for the next
drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.session_label import APP_TMUX_PREFIX, LABEL_MAX_CHARS

#: The client module holding the mirrored copies.
_RESOLVER = Path(__file__).resolve().parents[1] / "client" / "js" / "session-label.js"


def _js_source() -> str:
    """Read the client resolver as text.

    Description: read as UTF-8 bytes decoded explicitly rather than
      through any shell tool. This repo has a documented hazard where
      ``grep`` classifies a file carrying UTF-8 punctuation as binary and
      returns nothing, which is indistinguishable from a clean non-match.
      Parsing in Python removes that failure mode from the path.
    Inputs: none.
    Output: str - the file's full text.
    """
    return _RESOLVER.read_text(encoding="utf-8")


def _js_var(name: str) -> str:
    """Extract one ``var NAME = <literal>;`` initialiser from the client.

    Description: matches the DECLARATION specifically, not any mention of
      the name, so the module's own prose about a constant cannot satisfy
      the search. A missing or duplicated declaration fails loudly rather
      than returning a value nobody can attribute - the third outcome
      matters here as much as anywhere.
    Inputs: name (str) - the JavaScript identifier.
    Output: str - the raw literal text, e.g. ``"'cloude_'"`` or ``"200"``.
    Raises: AssertionError - not found, or found more than once.
    Example: _js_var('LABEL_MAX_CHARS')  # '200'
    """
    hits = re.findall(
        r"^\s*var\s+" + re.escape(name) + r"\s*=\s*([^;]+);",
        _js_source(),
        re.MULTILINE,
    )
    assert hits, f"no `var {name} = ...;` declaration in {_RESOLVER.name}"
    assert len(hits) == 1, f"{name} declared {len(hits)} times in {_RESOLVER.name}"
    return hits[0].strip()


def test_label_max_chars_matches_server() -> None:
    """The client's maxlength equals the server's LABEL_MAX_CHARS."""
    assert int(_js_var("LABEL_MAX_CHARS")) == LABEL_MAX_CHARS


def test_app_tmux_prefix_matches_server() -> None:
    """The client strips exactly the prefix the server puts on."""
    assert _js_var("APP_TMUX_PREFIX").strip("'\"") == APP_TMUX_PREFIX


def test_extractor_rejects_a_name_it_cannot_find() -> None:
    """The extractor fails on an absent name rather than returning junk.

    Description: the POSITIVE CONTROL for the two tests above. Both of
      them pass by comparing a parsed value; if the parser silently
      returned something for anything, they would pass for a reason
      unrelated to the file's contents. This asserts the parser can
      actually fail, which is what makes their passing mean something.
    """
    with pytest.raises(AssertionError):
        _js_var("NO_SUCH_CONSTANT_DECLARED_ANYWHERE")
