"""
Guard: the Cloudflare tunnel subsystem was removed in plan v3.2, and its
runtime dependency must not come back with it.

Why this test asserts on PARSED requirement names rather than on the raw
bytes of requirements.txt: the repo legitimately mentions Cloudflare in
prose (README, RELEASE-NOTES, and comments explaining the removal). A raw
substring search over the file would fail on a comment documenting the very
removal this test exists to protect - an absence check matching the string
inside a comment about its own removal. Parsing to distribution names makes
the comment inert and the declaration load-bearing.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"

# Distributions that existed only to serve the removed tunnel subsystem.
REMOVED_TUNNEL_DISTRIBUTIONS = {"cloudflare"}

# Modules the tunnel subsystem imported. `cloudflare` 2.x installs its
# package under the name `CloudFlare`, so both spellings are checked.
REMOVED_TUNNEL_MODULES = ("cloudflare", "CloudFlare")

# Roots that ship or run as part of the product.
PRODUCT_ROOTS = ("src", "scripts", "packaging", "verify")

_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9._-]+)")


def declared_distributions() -> set[str]:
    """
    Parse requirements.txt into the set of declared distribution names.

    Inputs: none (reads REQUIREMENTS).
    Outputs: set[str] of lowercased, normalized distribution names.
    Example: "pydantic>=2.10.0,<3" -> {"pydantic"}
    """
    names: set[str] = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def test_parser_sees_the_dependencies_that_are_really_declared():
    """Positive control: the parser must be capable of returning names.

    Without this, a parser that silently returns an empty set would make the
    absence assertions below pass for the wrong reason.
    """
    declared = declared_distributions()
    assert "fastapi" in declared
    assert "pydantic" in declared
    assert len(declared) >= 10


def test_no_removed_tunnel_distribution_is_declared():
    declared = declared_distributions()
    leaked = REMOVED_TUNNEL_DISTRIBUTIONS & declared
    assert not leaked, (
        f"requirements.txt declares {sorted(leaked)}, which served only the "
        "Cloudflare tunnel subsystem removed in plan v3.2. Nothing imports it."
    )


def test_nothing_in_the_product_imports_the_tunnel_sdk():
    """The reason the dependency is removable, asserted directly."""
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(?:%s)\b" % "|".join(REMOVED_TUNNEL_MODULES),
        re.MULTILINE,
    )
    offenders = []
    for root in PRODUCT_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"tunnel SDK imported by: {offenders}"
