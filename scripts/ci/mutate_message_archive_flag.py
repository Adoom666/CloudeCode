#!/usr/bin/env python3
"""Prove every message-archive gate test is capable of failing.

WHY THIS EXISTS. A test that asserts an ABSENCE - no table, no route, no
scheduler - passes for free against a build where the thing it is looking
for was never findable in the first place. A mistyped table prefix, a
mistyped path, a fixture that silently did nothing: all of them render as
a green absence assertion. So each gate is broken here, in the one
targeted way that should turn its own test red, and the run records
whether it actually did.

HOW IT RESTORES. Every file is read into memory, mutated on disk, tested,
then rewritten from the in-memory original and verified BY SHA256 against
the digest taken before the mutation. The restore is in a ``finally``, so
an interrupt or an exception restores the tree rather than leaving a
mutation behind - the failure mode where a killed mutation run poisons the
next suite with a plausible, wrong result.

Run with: ./venv/bin/python3 scripts/ci/mutate_message_archive_flag.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[2]
PYTEST = [str(ROOT / "venv" / "bin" / "python3"), "-m", "pytest", "-q",
          "-p", "no:warnings"]

#: (label, relative file, old substring, new substring, test node id).
#: Each mutation is the smallest edit that should make exactly that test
#: red. A mutation that is too broad proves nothing about the specific
#: assertion it was meant to exercise.
MUTATIONS: List[Tuple[str, str, str, str, str]] = [
    (
        "M1 pydantic default flipped on",
        "src/config.py",
        "    Fields:\n        enabled: Whether the message archive subsystem may run at all.\n    \"\"\"\n\n    enabled: bool = False",
        "    Fields:\n        enabled: Whether the message archive subsystem may run at all.\n    \"\"\"\n\n    enabled: bool = True",
        "tests/test_message_archive_flag.py::test_the_default_is_off",
    ),
    (
        "M2 DEFAULT_ENABLED constant flipped on",
        "src/core/message_archive_flag.py",
        "DEFAULT_ENABLED = False",
        "DEFAULT_ENABLED = True",
        "tests/test_message_archive_flag.py::test_the_default_is_off",
    ),
    (
        "M3 example config ships it on",
        "config.example.json",
        '"message_archive": {\n    "enabled": false\n  }',
        '"message_archive": {\n    "enabled": true\n  }',
        "tests/test_message_archive_flag.py::test_the_shipped_example_config_ships_it_off",
    ),
    (
        "M4 resolver treats an absent block as on",
        "src/core/message_archive_flag.py",
        "    if block is None:\n        return MessageArchiveFlag(\n            STATE_DISABLED, SOURCE_DEFAULT,",
        "    if block is None:\n        return MessageArchiveFlag(\n            STATE_ENABLED, SOURCE_DEFAULT,",
        "tests/test_message_archive_flag.py::test_the_default_is_off",
    ),
    (
        "M5 unparseable config resolves to a definite off",
        "src/core/message_archive_flag.py",
        "        return MessageArchiveFlag(\n            STATE_CANNOT_DETERMINE, SOURCE_CONFIG,\n            f\"{path} could not be read",
        "        return MessageArchiveFlag(\n            STATE_DISABLED, SOURCE_CONFIG,\n            f\"{path} could not be read",
        "tests/test_message_archive_flag.py::test_an_unreadable_config_is_cannot_determine_not_off",
    ),
    (
        "M6 migration gate removed from the v15->v16 step",
        "src/core/db_steps.py",
        "    Example: _step_v15_to_v16(conn)  # after _step_v14_to_v15\n    \"\"\"\n    if not message_archive_enabled():\n        return\n",
        "    Example: _step_v15_to_v16(conn)  # after _step_v14_to_v15\n    \"\"\"\n",
        "tests/test_message_archive_gating.py::test_a_fresh_database_with_the_flag_off_has_no_message_tables",
    ),
    (
        "M7 off is destructive: the gated step drops a table",
        "src/core/db_steps.py",
        "    Example: _step_v16_to_v17(conn)  # after _step_v15_to_v16\n    \"\"\"\n    if not message_archive_enabled():\n        return\n",
        "    Example: _step_v16_to_v17(conn)  # after _step_v15_to_v16\n    \"\"\"\n    if not message_archive_enabled():\n        conn.execute(\"DROP TABLE IF EXISTS message_hosts\")\n        return\n",
        "tests/test_message_archive_gating.py::test_a_gated_step_running_over_existing_message_tables_is_not_destructive",
    ),
    (
        "M8 materializer does nothing",
        "src/core/db_steps.py",
        "    _apply_v16_ddl(conn)\n    _apply_v17_ddl(conn)\n    _apply_v18_ddl(conn)",
        "    return",
        "tests/test_message_archive_gating.py::test_turning_the_flag_on_materializes_the_schema",
    ),
    (
        "M9 routers mounted unconditionally",
        "src/main.py",
        "if MESSAGE_ARCHIVE.enabled:\n    # THE MESSAGE ARCHIVE'S ENTIRE HTTP SURFACE.",
        "if True:\n    # THE MESSAGE ARCHIVE'S ENTIRE HTTP SURFACE.",
        "tests/test_message_archive_routes_gating.py::test_the_archive_api_is_absent_with_the_flag_off",
    ),
    (
        "M10 disabled page route serves the shell anyway",
        "src/main.py",
        "        Example: GET /archive -> 302 Location: /\n        \"\"\"\n        return RedirectResponse(url=\"/\", status_code=302)",
        "        Example: GET /archive -> 302 Location: /\n        \"\"\"\n        return HTMLResponse(content=_render_index_html())",
        "tests/test_message_archive_routes_gating.py::test_the_page_routes_redirect_to_the_launchpad_with_the_flag_off",
    ),
    (
        "M11 features endpoint mounted only when the feature is on",
        "src/main.py",
        '@app.get("/api/v1/features")',
        '@app.get("/api/v1/features" if MESSAGE_ARCHIVE.enabled else "/api/v1/features-hidden")',
        "tests/test_message_archive_routes_gating.py::test_features_reports_disabled_and_says_why",
    ),
    (
        "M12 master switch does not gate the ingest scheduler",
        "src/core/corpus_ingest_task.py",
        "    if not message_archive_enabled():\n        return False\n    raw = os.environ.get(ENABLE_ENV)",
        "    raw = os.environ.get(ENABLE_ENV)",
        "tests/test_message_archive_gating.py::test_the_ingest_scheduler_refuses_to_start_with_the_flag_off",
    ),
]

#: The same idea for the client half. The runner is ``node <file>``
#: instead of a pytest node id, because these assertions live in the node
#: suite; the mutate / test / restore / verify cycle is identical.
NODE_TEST = "tests/test_message_archive_client_gate.node.mjs"

NODE_MUTATIONS: List[Tuple[str, str, str, str, str]] = [
    (
        "C1 launchpad archive section ships visible",
        "client/js/launchpad.js",
        '<div class="launchpad-section" id="archive-section" hidden\n                     style="display:none;">',
        '<div class="launchpad-section" id="archive-section">',
        NODE_TEST,
    ),
    (
        "C2 launchpad never measures availability",
        "client/js/launchpad.js",
        "            window.ArchiveEntry.ensure().then((state) => {",
        "            Promise.resolve('enabled').then((state) => {",
        NODE_TEST,
    ),
    (
        "C3 a failed probe resolves to enabled",
        "client/js/archive-entry.js",
        "        }).catch(function (err) {\n            _settle(STATE_UNKNOWN,",
        "        }).catch(function (err) {\n            _settle(STATE_ENABLED,",
        NODE_TEST,
    ),
    (
        "C4 header control is not hidden at wire time",
        "client/js/header-menu.js",
        "        btn.style.display = 'none';\n        btn.hidden = true;",
        "        btn.hidden = false;",
        NODE_TEST,
    ),
    (
        "C5 header control is revealed on an unmeasured probe",
        "client/js/header-menu.js",
        "                if (state !== window.ArchiveEntry.STATE_ENABLED) return;",
        "                if (state === window.ArchiveEntry.STATE_DISABLED) return;",
        NODE_TEST,
    ),
]


def _sha256(path: Path) -> str:
    """Digest a file's bytes.

    Inputs: path (Path).
    Output: str - hex digest.
    Example: _sha256(p)[:8] -> 'a1b2c3d4'
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(node_id: str) -> int:
    """Run one pytest node id and return its exit code.

    Inputs: node_id (str) - a pytest node identifier.
    Output: int - the process exit code; 0 means the test passed.
    Example: _run("tests/x.py::test_y") -> 1
    """
    if node_id.endswith(".node.mjs"):
        command = ["node", node_id]
    else:
        command = PYTEST + [node_id]
    return subprocess.run(
        command, cwd=str(ROOT), capture_output=True
    ).returncode


def main() -> int:
    """Apply each mutation, require its test to go red, restore, verify.

    Inputs: none.
    Output: int - 0 when every mutation was caught and every file was
      restored byte-for-byte, 1 otherwise.
    Example: sys.exit(main())
    """
    failures = 0
    all_mutations = MUTATIONS + NODE_MUTATIONS
    for label, rel, old, new, node_id in all_mutations:
        path = ROOT / rel
        original = path.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            print(f"SKIP-BROKEN {label}: anchor appears "
                  f"{text.count(old)} times, expected 1")
            failures += 1
            continue
        try:
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            code = _run(node_id)
            caught = code != 0
            print(f"{'CAUGHT ' if caught else 'SURVIVED'} {label} "
                  f"-> {node_id} exit={code}")
            if not caught:
                failures += 1
        finally:
            path.write_bytes(original)
            after = _sha256(path)
            if after != before:
                print(f"RESTORE FAILED {rel}: {before} != {after}")
                failures += 1
            else:
                print(f"    restored {rel} sha256 {after[:16]} verified")
    print(f"\n{len(all_mutations) - failures}/{len(all_mutations)} mutations caught "
          f"and restored cleanly")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
