"""Hand-authored JSONL lines covering every reassembly path, for CI.

HOW CIRCULARITY WAS AVOIDED - read this before adding a fixture.

The obvious way to build these is to run the exporter and save what it
emits. That produces a suite that cannot detect a reassembly bug: the
expected bytes and the actual bytes come from the same function, so they
agree by construction and stay agreeing as the function goes wrong. Every
``line`` below is therefore a HAND-AUTHORED literal, typed from the JSON
spec and the declared serializer style, never pasted from program output.

The inputs to the code under test are derived from that literal by
:func:`split_line`, a twenty-line splitter written for this module that
imports nothing from ``src``. It uses only ``json.loads`` and dict
iteration. So both sides of every assertion are independent of
``reassemble`` and ``render_with_style``, which are the functions being
tested.

``key_order`` is stronger still: it is declared BY HAND on each fixture
rather than derived from the literal at all. Key-order interleaving is
the axis most likely to break reassembly, so it is the one axis where no
derivation sits between the author's intent and the assertion.

WHAT THIS SET COVERS, AND WHY IT IS NOT 1,347 FILES. The census enumerates
1,347 signatures across 26 dimensions, but most of those dimensions are
VALUES CARRIED INSIDE the body - ``model``, ``record_type``, ``role``,
``stop_reason`` and the rest. Reassembly cannot branch on them: it walks a
key order and hands the result to ``json.dumps``. Projecting the manifest
onto only the dimensions reassembly can actually branch on collapses all
1,347 signatures to SIX distinct classes, and ``test_jsonl_shape_fixtures``
asserts that collapse programmatically rather than taking this paragraph's
word for it. Those six are covered here, plus every value the census
listed as unrepresented in the corpus - which is the half a corpus-derived
suite can never supply.

SYNTHETIC CONTENT, REAL STRUCTURE. No line here is from a real
conversation. Identifiers are obvious placeholders, text is filler. This
file is mirrored to GitHub.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

#: The two keys ingest lifts out of a line into the per-appearance
#: envelope. Declared here BY HAND rather than imported so that a change
#: to the production constant shows up as a failing assertion in
#: ``test_appearance_key_set_has_not_changed`` instead of being silently
#: absorbed into every fixture at once.
ENVELOPE_KEYS: Tuple[str, ...] = ("isSidechain", "agentId")

#: Marker for a line whose top-level JSON is not an object, matching the
#: production sentinel. Declared by hand for the same reason.
NOT_AN_OBJECT: str = "__not_an_object__"


@dataclass(frozen=True)
class ShapeFixture:
    """One hand-authored line and the reassembly inputs it should rebuild.

    - ``line``: the exact expected bytes, typed by hand. This is the
      ASSERTION TARGET and never comes from program output.
    - ``key_order``: hand-declared top-level key order, or NOT_AN_OBJECT.
    - ``style``: the serializer style the line is written in. Declared,
      not detected, so no production function decides what the answer is.
    - ``covers``: which class or census gap this fixture exists for,
      shown in a failure so the reader knows what broke.
    """

    name: str
    covers: str
    line: str
    style: str
    key_order: Any
    envelope_keys_expected: List[str] = field(default_factory=list)


def split_line(text: str, key_order: Any) -> Tuple[Any, Dict[str, Any]]:
    """Split a hand-authored line into (body, envelope), independently.

    Description: the deliberately minimal, dependency-free counterpart to
      the production splitter. It imports nothing from ``src``, so a
      fixture's inputs and the production reassembly share no code and
      cannot agree by construction. A non-object line has no envelope and
      is carried whole.
    Inputs: text (str - the fixture's literal line), key_order (list of
      str, or NOT_AN_OBJECT).
    Output: (body, envelope).
    Raises: json.JSONDecodeError - the hand-authored literal is not valid
      JSON, which is an error in this file, not in the code under test.
    Example: split_line('{"isSidechain":false,"a":1}',
      ["isSidechain", "a"]) -> ({"a": 1}, {"isSidechain": False})
    """
    value = json.loads(text)
    if key_order == NOT_AN_OBJECT or not isinstance(value, dict):
        return value, {}
    body: Dict[str, Any] = {}
    envelope: Dict[str, Any] = {}
    for key, item in value.items():
        if key in ENVELOPE_KEYS:
            envelope[key] = item
        else:
            body[key] = item
    return body, envelope


#: Every hand-authored fixture. ``covers`` names the reassembly class or
#: the census gap the fixture exists for; it is printed on failure so the
#: reader learns which path broke, not merely which string differed.
#:
#: The first block mirrors the SIX reassembly classes measured across all
#: 1,347 corpus signatures. The rest are values the census recorded as
#: UNREPRESENTED in the corpus, which is exactly the coverage a
#: corpus-derived suite cannot supply.
FIXTURES: Tuple[ShapeFixture, ...] = (
    # ---- the six reassembly classes present in the corpus ----
    ShapeFixture(
        name="class_compact_env_issidechain",
        covers="corpus class 1 of 6: compact, envelope {isSidechain}, "
               "envelope key in the middle (698 signatures)",
        line='{"parentUuid":null,"isSidechain":false,"type":"user",'
             '"uuid":"uuid-0001","timestamp":"2026-01-01T00:00:00.000Z"}',
        style="compact",
        key_order=["parentUuid", "isSidechain", "type", "uuid", "timestamp"],
        envelope_keys_expected=["isSidechain"],
    ),
    ShapeFixture(
        name="class_compact_env_both",
        covers="corpus class 2 of 6: compact, envelope {agentId, "
               "isSidechain}, both in the middle (578 signatures)",
        line='{"parentUuid":"uuid-0000","isSidechain":true,'
             '"agentId":"a0123456789abcdef","type":"progress",'
             '"uuid":"uuid-0002","timestamp":"2026-01-01T00:00:01.000Z"}',
        style="compact",
        key_order=["parentUuid", "isSidechain", "agentId", "type", "uuid",
                   "timestamp"],
        envelope_keys_expected=["agentId", "isSidechain"],
    ),
    ShapeFixture(
        name="class_compact_env_empty",
        covers="corpus class 3 of 6: compact, NO envelope keys at all "
               "(65 signatures)",
        line='{"type":"summary","summary":"synthetic summary text",'
             '"leafUuid":"uuid-0003"}',
        style="compact",
        key_order=["type", "summary", "leafUuid"],
        envelope_keys_expected=[],
    ),
    ShapeFixture(
        name="class_spaced_env_issidechain",
        covers="corpus class 4 of 6: the spaced style, which occurs on "
               "720 corpus rows and is easy to assume away (3 signatures)",
        line='{"parentUuid": null, "isSidechain": false, "type": "system", '
             '"uuid": "uuid-0004"}',
        style="spaced",
        key_order=["parentUuid", "isSidechain", "type", "uuid"],
        envelope_keys_expected=["isSidechain"],
    ),
    ShapeFixture(
        name="class_compact_env_agentid_only",
        covers="corpus class 5 of 6: agentId WITHOUT isSidechain, the "
               "envelope state easiest to assume impossible (2 signatures)",
        line='{"agentId":"a0fedcba98765432","type":"assistant",'
             '"uuid":"uuid-0005","message":{"role":"assistant",'
             '"model":"synthetic-model","content":[{"type":"text",'
             '"text":"synthetic"}],"stop_reason":"end_turn",'
             '"usage":{"input_tokens":1,"output_tokens":2}}}',
        style="compact",
        key_order=["agentId", "type", "uuid", "message"],
        envelope_keys_expected=["agentId"],
    ),
    # class 6 of 6 is the raw_line branch. It bypasses render_line
    # entirely, so it cannot be proven here - it is proven end to end
    # through ingest and export in test_jsonl_line_endings.py.

    # ---- census gap: serializer styles that never occur in the corpus --
    ShapeFixture(
        name="gap_compact_ascii",
        covers="census gap: serializer_style compact_ascii, zero corpus "
               "rows. Non-ASCII escaped as \\uXXXX",
        line='{"type":"user","uuid":"uuid-0006","text":"caf\\u00e9"}',
        style="compact_ascii",
        key_order=["type", "uuid", "text"],
    ),
    ShapeFixture(
        name="gap_spaced_ascii",
        covers="census gap: serializer_style spaced_ascii, zero corpus rows",
        line='{"type": "user", "uuid": "uuid-0007", "text": "caf\\u00e9"}',
        style="spaced_ascii",
        key_order=["type", "uuid", "text"],
    ),
    ShapeFixture(
        name="unicode_unescaped_compact",
        covers="19.66 percent of corpus bodies are non-ASCII and every "
               "one was written with escaping OFF",
        line='{"type":"user","uuid":"uuid-0008","text":"café"}',
        style="compact",
        key_order=["type", "uuid", "text"],
    ),
    ShapeFixture(
        name="unicode_astral_unescaped",
        covers="astral-plane characters above U+FFFF, on 1.22 percent of "
               "corpus bodies, unescaped",
        line='{"type":"user","uuid":"uuid-0009","text":"\U0001D11E"}',
        style="compact",
        key_order=["type", "uuid", "text"],
    ),
    ShapeFixture(
        name="unicode_astral_escaped",
        covers="an astral character under compact_ascii becomes a "
               "SURROGATE PAIR, which is where a naive escaper breaks",
        line='{"type":"user","uuid":"uuid-0010","text":"\\ud834\\udd1e"}',
        style="compact_ascii",
        key_order=["type", "uuid", "text"],
    ),

    # ---- census gap: key orderings that are not object key lists -------
    ShapeFixture(
        name="gap_not_an_object_array",
        covers="census gap: a line whose top-level JSON is an ARRAY, so "
               "there is no key order to walk",
        line='[1,2,3]',
        style="compact",
        key_order=NOT_AN_OBJECT,
    ),
    ShapeFixture(
        name="gap_not_an_object_string",
        covers="census gap: a bare top-level STRING line",
        line='"a synthetic bare string line"',
        style="compact",
        key_order=NOT_AN_OBJECT,
    ),
    ShapeFixture(
        name="gap_not_an_object_number",
        covers="census gap: a bare top-level NUMBER line",
        line='42',
        style="compact",
        key_order=NOT_AN_OBJECT,
    ),
    ShapeFixture(
        name="gap_not_an_object_null",
        covers="census gap: a bare top-level null, which must not be "
               "confused with an absent body",
        line='null',
        style="compact",
        key_order=NOT_AN_OBJECT,
    ),

    # ---- envelope key POSITION, the axis reassembly walks --------------
    ShapeFixture(
        name="gap_envelope_key_first",
        covers="census gap: an envelope key at position 0. Measured over "
               "the whole corpus, isSidechain is NEVER first, so this "
               "interleaving has no live exemplar",
        line='{"isSidechain":true,"agentId":"a1111111","type":"user",'
             '"uuid":"uuid-0011"}',
        style="compact",
        key_order=["isSidechain", "agentId", "type", "uuid"],
        envelope_keys_expected=["agentId", "isSidechain"],
    ),
    ShapeFixture(
        name="envelope_key_last",
        covers="an envelope key at the FINAL position, which occurs on "
               "474 corpus appearances",
        line='{"type":"user","uuid":"uuid-0012","agentId":"a2222222"}',
        style="compact",
        key_order=["type", "uuid", "agentId"],
        envelope_keys_expected=["agentId"],
    ),
    ShapeFixture(
        name="envelope_only_object",
        covers="a line consisting of nothing BUT an envelope key, so the "
               "stored body is an empty object",
        line='{"isSidechain":false}',
        style="compact",
        key_order=["isSidechain"],
        envelope_keys_expected=["isSidechain"],
    ),
    ShapeFixture(
        name="empty_object",
        covers="an empty object line, whose key order is [] and must not "
               "be confused with the not-an-object sentinel",
        line='{}',
        style="compact",
        key_order=[],
    ),

    # ---- census gap: message.content shapes that never occur ----------
    ShapeFixture(
        name="gap_content_absent",
        covers="census gap: message present, content key ABSENT",
        line='{"type":"assistant","uuid":"uuid-0013","message":'
             '{"role":"assistant","model":"synthetic-model"}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_content_null",
        covers="census gap: message.content explicitly null, distinct "
               "from absent",
        line='{"type":"assistant","uuid":"uuid-0014","message":'
             '{"role":"assistant","content":null}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_content_number",
        covers="census gap: message.content as a number",
        line='{"type":"assistant","uuid":"uuid-0015","message":'
             '{"role":"assistant","content":7}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_content_object",
        covers="census gap: message.content as an object rather than a "
               "string or array",
        line='{"type":"assistant","uuid":"uuid-0016","message":'
             '{"role":"assistant","content":{"type":"text"}}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_usage_null",
        covers="census gap: message.usage null, never observed live",
        line='{"type":"assistant","uuid":"uuid-0017","message":'
             '{"role":"assistant","usage":null}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_non_object_content_block",
        covers="census gap: a content array holding non-object blocks",
        line='{"type":"assistant","uuid":"uuid-0018","message":'
             '{"role":"assistant","content":["bare string block",42]}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),
    ShapeFixture(
        name="gap_non_string_block_type",
        covers="census gap: a content block whose type is not a string",
        line='{"type":"assistant","uuid":"uuid-0019","message":'
             '{"role":"assistant","content":[{"type":5}]}}',
        style="compact",
        key_order=["type", "uuid", "message"],
    ),

    # ---- nested ordering, escapes and scalar preservation --------------
    ShapeFixture(
        name="nested_key_order_not_alphabetical",
        covers="a NESTED object whose keys are in non-alphabetical order. "
               "This is the exact shape that broke when body storage "
               "sorted keys - the line stays valid JSON and the bytes move",
        line='{"type":"user","uuid":"uuid-0020","payload":'
             '{"z":1,"m":2,"a":0}}',
        style="compact",
        key_order=["type", "uuid", "payload"],
    ),
    ShapeFixture(
        name="deep_nesting",
        covers="ten levels of nesting, the deepest measured in the corpus",
        line='{"type":"user","uuid":"uuid-0021","d":{"d":{"d":{"d":{"d":'
             '{"d":{"d":{"d":1}}}}}}}}',
        style="compact",
        key_order=["type", "uuid", "d"],
    ),
    ShapeFixture(
        name="scalar_types_preserved",
        covers="every JSON scalar type in one line, so an integer cannot "
               "come back as a float or a bool as a number",
        line='{"type":"user","uuid":"uuid-0022","n":0,"f":1.5,"t":true,'
             '"g":false,"z":null}',
        style="compact",
        key_order=["type", "uuid", "n", "f", "t", "g", "z"],
    ),
    ShapeFixture(
        name="string_escapes",
        covers="quote, backslash, newline, tab and a control character "
               "inside a string value",
        line='{"type":"user","uuid":"uuid-0023",'
             '"s":"a\\"b\\\\c\\nd\\te\\u0001f"}',
        style="compact",
        key_order=["type", "uuid", "s"],
    ),
    ShapeFixture(
        name="forward_slash_unescaped",
        covers="a forward slash, which JSON permits escaped or not and "
               "which Python does not escape - a real byte-level trap",
        line='{"type":"user","uuid":"uuid-0024","p":"/a/b"}',
        style="compact",
        key_order=["type", "uuid", "p"],
    ),
)
