#!/usr/bin/env python3
"""One-shot mutation test for the contrast regression suite. Applies each
mutation, runs pytest, records KILLED/SURVIVED, then restores the file
byte-for-byte from git. Never leaves a mutation applied on exit.

Usage: venv/bin/python3 scripts/contrast/mutation_test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTEST = ["/Users/jsugamele/Development/CloudeCode/venv/bin/python3", "-m", "pytest",
          "tests/test_theme_contrast.py", "tests/test_theme_token_sourcing.py", "-q"]


def run_pytest() -> bool:
    r = subprocess.run(PYTEST, cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0  # True == all passed


def mutate_json(path: Path, key: str, value: str):
    d = json.loads(path.read_text(encoding="utf-8"))
    d["cssVars"][key] = value
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


results = []


def case(desc, apply_fn, path: Path):
    # Snapshot the file's CURRENT working-tree content (already carries the
    # real fixes from this session) - `git checkout` would instead revert
    # to the committed HEAD, undoing this session's actual fix and
    # corrupting every subsequent case that depends on it.
    original = path.read_text(encoding="utf-8")
    apply_fn()
    passed = run_pytest()
    killed = not passed
    results.append((desc, killed))
    path.write_text(original, encoding="utf-8")


# M1: reintroduce the original low-contrast matrix sidebar color
p1 = ROOT / "client/css/themes/matrix/theme.json"
case("matrix fg-subtle reverted to low-contrast #006619 (the original bug)",
     lambda: mutate_json(p1, "--color-fg-subtle", "#006619"), p1)

# M2: near-invisible fg-faint on a mid theme (dracula)
p2 = ROOT / "client/css/themes/dracula/theme.json"
case("dracula fg-faint set to same as its own bg-elevated (0 contrast)",
     lambda: mutate_json(p2, "--color-fg-faint", "#44475a"), p2)

# M3: on-accent set to a color that fails against accent (snes)
p3 = ROOT / "client/css/themes/snes/theme.json"
case("snes on-accent set to a mid-gray with poor contrast on its accent fill",
     lambda: mutate_json(p3, "--color-on-accent", "#8a8aa0"), p3)

# M4: reintroduce hardcoded yellow on the ownership stripe (styles.css)
p4 = ROOT / "client/css/styles.css"


def mutate_stripe():
    css = p4.read_text(encoding="utf-8")
    css2 = css.replace(
        ".running-session-row.external {\n    border-left: 3px solid var(--color-badge-external-fg);\n}",
        ".running-session-row.external {\n    border-left: 3px solid #ffd400;\n}",
    )
    assert css2 != css, "mutation M4 substring not found - selector text drifted"
    p4.write_text(css2, encoding="utf-8")


case("ownership stripe .external hardcoded back to a literal yellow", mutate_stripe, p4)

# M5: strip the help link rule entirely (falls back to default blue)
p5 = ROOT / "client/css/styles.css"


def mutate_help_link():
    import re
    css = p5.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\n/\* README link.*?\.adopt-disclosure-body a:focus-visible \{[^}]*\}\n",
        re.DOTALL,
    )
    css2, n = pattern.subn("\n", css)
    assert n == 1, f"expected exactly 1 match removing the help-link block, got {n}"
    assert css2 != css
    p5.write_text(css2, encoding="utf-8")


case("help README link rule removed entirely (default browser blue)", mutate_help_link, p5)

# M6: hardcode the help link to a literal blue instead of a token
p6 = ROOT / "client/css/styles.css"


def mutate_help_link_hardcode():
    css = p6.read_text(encoding="utf-8")
    css2 = css.replace(
        ".adopt-disclosure-body a {\n    color: var(--color-accent);",
        ".adopt-disclosure-body a {\n    color: #3366ff;",
        1,
    )
    assert css2 != css
    p6.write_text(css2, encoding="utf-8")


case("help link color hardcoded to a literal blue instead of a token", mutate_help_link_hardcode, p6)

print()
n_killed = sum(1 for _, k in results if k)
for desc, killed in results:
    print(f"{'KILLED  ' if killed else 'SURVIVED'}  {desc}")
print(f"\n{n_killed}/{len(results)} mutations killed")
sys.exit(0 if n_killed == len(results) else 1)
