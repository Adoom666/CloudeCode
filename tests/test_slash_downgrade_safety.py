"""Defect 2: an object-form slash entry makes the OLD version refuse to boot.

``AuthConfig.common_slash_commands`` is ``List[str]`` at v0.8.1 and
``List[Union[str, Dict[str, Any]]]`` on the new tip. ONE object-form entry
makes v0.8.1's ``load_auth_config`` raise a pydantic ValidationError and
the server exit with "Application startup failed" - not a degraded mode,
it does not start. Measured by executing the round trip, and guarded at
v0.8.1 itself by step 08 of scripts/ci/roundtrip-upgrade-downgrade.sh.

The config migration itself only ever appends bare strings, so a pure
upgrade stays downgrade-safe. USING the feature is what breaks it.

THE FIX, and its exact scope: an entry whose description is empty,
whitespace-only or absent carries nothing the object form exists to hold,
so the write path emits a bare string for it. An entry with a real
description keeps its object form and stays downgrade-unsafe - that is
the user\'s data and the user\'s choice, not something to strip on his
behalf. This shrinks the blast radius to configs that genuinely use
descriptions; it does not eliminate it.

Every test here was run against the unfixed write path first and observed
to FAIL.
"""

from __future__ import annotations

import json

from src.core import slash_favorites



def _config_file(tmp_path, entries):
    """Write a minimal config.json carrying a favorites list.

    Inputs: tmp_path (Path). entries (list) - the raw favorites value.
    Output: Path to the written config.json.
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"common_slash_commands": entries}, indent=2))
    return path


def test_an_entry_with_no_description_is_written_as_a_bare_string(tmp_path):
    """THE DEFECT ITSELF. An object entry carrying nothing v0.8.1 cannot read.

    v0.8.1 types the key ``List[str]``, so ONE object entry makes its
    ``load_auth_config`` raise and the server exit at startup. An entry
    whose description is empty, blank or absent carries no information
    the object form exists to hold, so writing it as an object breaks the
    old version for nothing. RED against the unfixed write path, which
    passed the object straight through.
    """
    path = _config_file(tmp_path, [])
    slash_favorites.write(
        path,
        [
            "/clear",
            {"command": "/empty", "description": ""},
            {"command": "/blank", "description": "   "},
            {"command": "/absent"},
            {"command": "/null", "description": None},
        ],
    )
    stored = json.loads(path.read_text())["common_slash_commands"]
    assert stored == ["/clear", "/empty", "/blank", "/absent", "/null"]


def test_a_real_description_keeps_the_object_form(tmp_path):
    """The blast radius shrinks to configs that genuinely use descriptions.

    A user's own wording is his data. It is preserved verbatim, and the
    config stays downgrade-unsafe as a consequence of HIS choice rather
    than of ours.
    """
    path = _config_file(tmp_path, [])
    slash_favorites.write(
        path,
        [{"command": "/diff", "description": "review changes"}, "/clear"],
    )
    stored = json.loads(path.read_text())["common_slash_commands"]
    assert stored == [
        {"command": "/diff", "description": "review changes"},
        "/clear",
    ]


def test_normalizing_the_write_does_not_change_what_is_rendered(tmp_path):
    """A bare string still renders its built-in description.

    Collapsing ``{"command": "/diff", "description": ""}`` to ``"/diff"``
    must not change the chip the user sees, or the fix would be a visible
    regression dressed as a safety improvement.
    """
    path = _config_file(tmp_path, [])
    slash_favorites.write(path, [{"command": "/diff", "description": ""}])
    details = slash_favorites.payload(path)["command_details"]
    assert details == [{"command": "/diff", "description": "review changes"}]
