#!/usr/bin/env python3
"""Re-bless the pinned JSONL export goldens. DELIBERATE ACTION ONLY.

WHY THIS IS A SCRIPT AND NOT A FLAG ON THE TEST. A golden that can
rebaseline itself is not a golden. The common failure is an env var that
regenerates on the spot, because the fastest way past a red test is then
to set it - which converts "the output changed" into "the output is
whatever it is now", silently, in the same commit that broke it. So
re-blessing lives outside pytest, refuses to run without an explicit
acknowledgement flag, and PRINTS THE DIFF it is about to write so a human
sees what moved before it is written.

WHAT IS PINNED, AND WHERE THE VALUES COME FROM. Every expected line is the
HAND-AUTHORED literal from ``tests/jsonl_shape_fixture_data``, and every
hash is computed from that literal with ``hashlib`` directly. Nothing here
runs the reassembly code under test, so the goldens stay a statement about
what the bytes SHOULD be rather than a recording of what the code
currently emits. That distinction is the whole reason this file does not
simply call the exporter and save the answer.

Usage:
    ./venv/bin/python3 scripts/bless_jsonl_export_golden.py --show
    ./venv/bin/python3 scripts/bless_jsonl_export_golden.py \\
        --i-have-reviewed-the-diff
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.db_models import CURRENT_SCHEMA_VERSION  # noqa: E402
from src.core.message_model_serialize import (  # noqa: E402
    APPEARANCE_KEYS,
    SERIALIZER_STYLES,
)
from tests.jsonl_shape_fixture_data import FIXTURES, NOT_AN_OBJECT  # noqa: E402

GOLDEN_PATH: Path = REPO_ROOT / "tests" / "fixtures" / "jsonl_export_golden.json"

#: Bumped by hand when the golden file's own layout changes, so a reader
#: written for the old layout refuses rather than misreading it.
GOLDEN_FORMAT_VERSION: int = 1


def build_golden() -> Dict[str, Any]:
    """Assemble the golden document from hand-authored fixture literals.

    Description: hashes each fixture's literal directly. The reassembly
      code is never called, so this cannot record a bug as the new
      expectation.
    Inputs: none.
    Output: dict - the golden document.
    Example: build_golden()["schema_version"] -> 17
    """
    entries: List[Dict[str, Any]] = []
    for fixture in sorted(FIXTURES, key=lambda f: f.name):
        raw = fixture.line.encode("utf-8")
        entries.append({
            "name": fixture.name,
            "covers": fixture.covers,
            "style": fixture.style,
            "key_order": (
                None if fixture.key_order == NOT_AN_OBJECT
                else list(fixture.key_order)
            ),
            "line": fixture.line,
            "line_sha256": hashlib.sha256(raw).hexdigest(),
            "line_byte_length": len(raw),
        })
    return {
        "golden_format_version": GOLDEN_FORMAT_VERSION,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "note": (
            "Pinned to schema v%d. If CURRENT_SCHEMA_VERSION moves, the "
            "test fails ON PURPOSE so a human re-confirms these bytes are "
            "still right for the new schema before re-blessing."
            % CURRENT_SCHEMA_VERSION
        ),
        "serializer_styles": [
            {"name": name, "separators": list(separators),
             "ensure_ascii": ensure_ascii}
            for name, separators, ensure_ascii in SERIALIZER_STYLES
        ],
        "appearance_keys": list(APPEARANCE_KEYS),
        "entries": entries,
    }


def render(document: Dict[str, Any]) -> str:
    """Serialize the golden document the one way it is ever written.

    Inputs: document (dict).
    Output: str - the file's exact text, newline terminated.
    Example: render({})[-1] -> "\\n"
    """
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def summarize_change(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Describe, in words, what re-blessing would change.

    Description: a unified text diff of a 40 KB JSON file is unreadable,
      and an unreadable diff gets approved without being read. This
      reports the decisions instead: version moves, and per-entry
      additions, removals and byte changes.
    Inputs: old (dict - the golden on disk, or {}), new (dict).
    Output: list[str] - human-readable lines, empty when nothing moves.
    Example: summarize_change({}, {}) -> []
    """
    lines: List[str] = []
    for key in ("golden_format_version", "schema_version"):
        if old.get(key) != new.get(key):
            lines.append(
                f"  {key}: {old.get(key)!r} -> {new.get(key)!r}"
            )
    if old.get("serializer_styles") != new.get("serializer_styles"):
        lines.append("  serializer_styles table CHANGED")
    if old.get("appearance_keys") != new.get("appearance_keys"):
        lines.append(
            f"  appearance_keys: {old.get('appearance_keys')} -> "
            f"{new.get('appearance_keys')}"
        )
    old_by_name = {e["name"]: e for e in old.get("entries", [])}
    new_by_name = {e["name"]: e for e in new.get("entries", [])}
    for name in sorted(set(new_by_name) - set(old_by_name)):
        lines.append(f"  ADDED   {name}")
    for name in sorted(set(old_by_name) - set(new_by_name)):
        lines.append(f"  REMOVED {name}")
    for name in sorted(set(old_by_name) & set(new_by_name)):
        before, after = old_by_name[name], new_by_name[name]
        if before["line_sha256"] != after["line_sha256"]:
            lines.append(
                f"  CHANGED {name}: {before['line_byte_length']} bytes "
                f"-> {after['line_byte_length']} bytes"
            )
    return lines


def main() -> int:
    """Show or write the goldens, refusing to write without acknowledgement.

    Inputs: none (reads argv).
    Output: int - process exit status.
    Example: main() -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true",
                        help="report what would change and write nothing")
    parser.add_argument("--i-have-reviewed-the-diff", action="store_true",
                        dest="reviewed",
                        help="required to actually write the golden file")
    args = parser.parse_args()

    new = build_golden()
    old: Dict[str, Any] = {}
    if GOLDEN_PATH.is_file():
        with GOLDEN_PATH.open(encoding="utf-8") as handle:
            old = json.load(handle)

    changes = summarize_change(old, new)
    if not changes:
        print(f"no change: {GOLDEN_PATH} already matches "
              f"schema v{CURRENT_SCHEMA_VERSION}")
        return 0
    print(f"re-blessing would change {GOLDEN_PATH}:")
    for line in changes:
        print(line)
    if args.show or not args.reviewed:
        print("\nNOTHING WRITTEN. Re-run with --i-have-reviewed-the-diff "
              "once you have confirmed every line above is intended.")
        return 1
    GOLDEN_PATH.write_text(render(new), encoding="utf-8")
    print(f"\nwritten: {GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
