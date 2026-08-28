"""Regression test: WCAG 2.1 contrast for every real foreground/background
pairing (``scripts/contrast/pairings.py``), across every theme.

FAILS the moment any pairing in any theme drops below its threshold, so a
future theme edit (or a new theme) that reintroduces a low-contrast pair
cannot land silently. See scripts/contrast/audit_themes.py for the CLI
version of the same check with a human-readable table.

Three-outcome rule: a pairing that could not be resolved to concrete colors
(unresolved var() chain, unparseable color function) is asserted as its own
explicit failure - `pytest.fail` naming what could not be measured - never
silently skipped and never counted as a pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contrast.audit_themes import evaluate  # noqa: E402
from scripts.contrast.color_utils import list_theme_ids  # noqa: E402
from scripts.contrast.pairings import PAIRINGS  # noqa: E402

THEME_IDS = list_theme_ids()


def test_at_least_twenty_themes_discovered():
    """Sanity floor so a broken theme-discovery glob fails loudly instead
    of silently evaluating zero themes and reporting a hollow green run.
    """
    assert len(THEME_IDS) >= 20, f"expected >=20 themes, found {len(THEME_IDS)}: {THEME_IDS}"


@pytest.mark.parametrize("theme_id", THEME_IDS)
def test_theme_contrast_pairings_pass(theme_id):
    """Every pairing for this theme must PASS. A COULD_NOT_EVALUATE result
    is also a failure here (never a silent skip) - it means a token in
    this theme cannot be resolved to a concrete color at all, which is
    itself a bug worth surfacing.
    """
    rows = evaluate(theme_id)
    assert len(rows) == len(PAIRINGS), (
        f"{theme_id}: evaluated {len(rows)} rows, expected {len(PAIRINGS)} "
        "(pairings.py and audit_themes.py have drifted apart)"
    )

    cannot_evaluate = [r for r in rows if r["status"] == "COULD_NOT_EVALUATE"]
    failing = [r for r in rows if r["status"] == "FAIL"]

    if cannot_evaluate:
        detail = "; ".join(f"{r['pairing']}: {r['detail']}" for r in cannot_evaluate)
        pytest.fail(f"{theme_id}: could not evaluate {len(cannot_evaluate)} pairing(s): {detail}")

    if failing:
        detail = "; ".join(
            f"{r['pairing']} ratio={r['ratio']} need={r['threshold']} ({r['detail']})"
            for r in failing
        )
        pytest.fail(f"{theme_id}: {len(failing)} pairing(s) below WCAG threshold: {detail}")


def test_matrix_sidebar_specifically_readable():
    """The user's original report named the matrix theme's sidebar by
    name. Pin an explicit floor on it so this exact regression (reported
    2026-08-17) cannot come back disguised as a passing generic loop.
    """
    rows = evaluate("matrix")
    sidebar_rows = [r for r in rows if r["pairing"].startswith("sidebar")]
    assert sidebar_rows, "no sidebar pairings found for matrix - pairings.py lost sidebar coverage"
    for r in sidebar_rows:
        assert r["status"] == "PASS", f"matrix {r['pairing']}: {r}"
