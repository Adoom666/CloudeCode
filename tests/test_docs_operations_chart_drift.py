"""Drift test for docs/session-project-operations.md.

WHAT THIS PROVES, AND WHAT IT DOES NOT
    The chart is a set of claims about code. This test proves every symbol
    and route the chart NAMES still resolves. It does NOT prove the arrows
    between them are right - no mechanical test can, and pretending
    otherwise would be exactly the false green the chart's own three-outcome
    section warns about.

    So: a green run means the chart's vocabulary is real. It means nothing
    about whether the chart's story is still true. A rename, a deleted
    route or a moved helper turns this RED, which is the whole point - the
    chart fails the build instead of quietly becoming fiction.

THE CITATION GRAMMAR, WHICH THE DOCUMENT DECLARES IN ITS OWN HEADER
    path/to/file.py::symbol   - that file defines that symbol
    path/to/file.js::symbol   - that file defines that function or method
    METHOD /route             - some router under src/api/ declares it

    A citation with no double colon and no leading method is not a
    citation; prose is free to mention a filename without being held to it.

WHY A MODULE PATH WITH NO SYMBOL IS ALSO CHECKED
    The chart cites a few modules as a whole - src/core/project_presence.py,
    for instance. Those are held to "the file exists", which is the only
    claim being made.

THREE OUTCOMES
    A citation resolves, a citation provably does not resolve, or the
    document could not be read at all. The third is its own failure with
    its own message; reporting "no broken citations" for a document that
    was never opened is the defect this file exists to prevent.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "docs" / "session-project-operations.md"

#: Routers whose decorators the METHOD /route citations are checked against.
ROUTER_FILES: Tuple[str, ...] = (
    "src/api/routes.py",
    "src/api/auth.py",
    "src/api/session_groups_routes.py",
    "src/api/config_files_routes.py",
    "src/api/status_routes.py",
    "src/api/setup_routes.py",
    "src/api/version_routes.py",
)

#: `path::symbol`, inside backticks so prose cannot accidentally enrol.
_SYMBOL_CITATION = re.compile(
    r"(?<![\w/.])((?:src|client|tests|macOS)/[\w./-]+\.(?:py|js))::([A-Za-z_]\w*)"
)

#: A bare module citation - a path with no `::`. Held only to file existence.
_MODULE_CITATION = re.compile(
    r"(?<![\w/.])((?:src|client|tests|macOS)/[\w./-]+\.(?:py|js))(?!::)(?![\w/.])"
)

#: `METHOD /route`. The chart writes path parameters as bare words
#: (`/sessions/session_id/name`), because braces are unreadable inside a
#: mermaid label - so the comparison normalises both sides.
_ROUTE_CITATION = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_./{}-]*)"
)

_ROUTE_DECORATOR = re.compile(
    r"@(?:router|page_router)\.(get|post|put|patch|delete)\(\s*\"([^\"]*)\"",
    re.MULTILINE,
)

#: Route prefixes each router file is mounted under, so a citation can be
#: written the way a reader sees it rather than the way FastAPI stores it.
_ROUTER_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "src/api/session_groups_routes.py": ("/session-groups",),
    "src/api/config_files_routes.py": ("/config-files",),
}


def _read_chart() -> str:
    """Read the chart, or fail with the could-not-evaluate case named.

    Inputs: none.
    Output: str - the document's text.
    """
    if not CHART.exists():
        pytest.fail(
            f"CANNOT DETERMINE: {CHART} does not exist, so no citation was "
            "checked. This is not a passing run."
        )
    text = CHART.read_text(encoding="utf-8")
    if not text.strip():
        pytest.fail(f"CANNOT DETERMINE: {CHART} is empty.")
    return text


def _python_symbols(path: Path) -> Set[str]:
    """Every def, async def, class and module-level assignment name.

    Description: methods count, at any nesting depth, because the chart
      cites manager methods by bare name the way a reader would say them.
    Inputs: path (Path) - a .py file.
    Output: set[str] - names defined anywhere in the module.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


_JS_DEFINITION = re.compile(
    r"(?:function\s+(\w+)"          # function foo(
    r"|(?:async\s+)?(\w+)\s*\("     # foo( / async foo(  - object method
    r"|(?:const|let|var)\s+(\w+)\s*=)"
)


def _js_symbols(path: Path) -> Set[str]:
    """Names a .js file plausibly defines.

    Description: deliberately loose. This is a drift alarm for a chart, not
      a JavaScript parser; the failure it must catch is "someone renamed
      deleteSessionRecord and the chart still says deleteSessionRecord",
      and a loose matcher catches that without pulling in a JS toolchain.
    Inputs: path (Path) - a .js file.
    Output: set[str] - candidate defined names.
    """
    text = path.read_text(encoding="utf-8")
    return {m for groups in _JS_DEFINITION.findall(text) for m in groups if m}


def _normalise_route(raw: str) -> str:
    """Reduce a route to a comparable shape.

    Description: strips a trailing slash and replaces every path parameter -
      `{session_uuid}`, or a bare segment the chart wrote without braces -
      with a single placeholder, so the chart may spell a route the way a
      human reads it.
    Inputs: raw (str) - a route path.
    Output: str - the normalised form.
    Example: _normalise_route("/sessions/{id}/name")  # '/sessions/*/name'
    """
    trimmed = raw.rstrip("/") or "/"
    parts = []
    for seg in trimmed.split("/"):
        if seg.startswith("{") or seg in _PARAM_WORDS:
            parts.append("*")
        else:
            parts.append(seg)
    return "/".join(parts)


#: Bare segments the chart uses where the code has a path parameter. Listed
#: explicitly rather than guessed at, so a genuinely new literal segment
#: cannot be silently normalised away into a match.
_PARAM_WORDS: Set[str] = {
    "session_id",
    "session_uuid",
    "session_name",
    "project_name",
    "group_uuid",
    "name",
    "wrapper_id",
    "toast_id",
}


def _declared_routes() -> Set[Tuple[str, str]]:
    """Every route the API actually declares, normalised.

    Inputs: none.
    Output: set[(METHOD, normalised path)].
    """
    found: Set[Tuple[str, str]] = set()
    for rel in ROUTER_FILES:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        prefixes = _ROUTER_PREFIXES.get(rel, ("",))
        for method, route in _ROUTE_DECORATOR.findall(text):
            for prefix in prefixes:
                found.add(
                    (method.upper(), _normalise_route(f"{prefix}{route}"))
                )
    return found


# --------------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------------


def test_chart_is_readable_and_actually_cites_things():
    """A chart with no citations must not pass this file silently.

    Description: the positive control. Every other test here is an
      antijoin, and an antijoin over an empty left side is vacuously green -
      which is precisely how a broken extractor reports a clean document.
    """
    text = _read_chart()
    symbols = _SYMBOL_CITATION.findall(text)
    routes = _ROUTE_CITATION.findall(text)
    assert len(symbols) >= 40, (
        "the citation extractor found "
        f"{len(symbols)} path::symbol citations, which is far fewer than the "
        "chart carries. The extractor is broken, not the chart - a low count "
        "here would make every other assertion in this file vacuous."
    )
    assert len(routes) >= 10, (
        f"only {len(routes)} METHOD /route citations found; the extractor is "
        "not matching the document."
    )


def test_every_cited_file_exists():
    """Every path the chart names resolves to a file in the tree."""
    text = _read_chart()
    cited = {m for m, _ in _SYMBOL_CITATION.findall(text)}
    cited |= set(_MODULE_CITATION.findall(text))
    missing = sorted(p for p in cited if not (REPO_ROOT / p).exists())
    assert not missing, (
        "docs/session-project-operations.md cites files that no longer "
        f"exist: {missing}"
    )


def test_every_cited_symbol_is_defined_in_the_file_it_is_cited_from():
    """The core assertion: chart vocabulary matches code vocabulary."""
    text = _read_chart()
    by_file: Dict[str, Set[str]] = {}
    for rel, symbol in _SYMBOL_CITATION.findall(text):
        by_file.setdefault(rel, set()).add(symbol)

    broken: List[str] = []
    for rel, wanted in sorted(by_file.items()):
        path = REPO_ROOT / rel
        if not path.exists():
            broken.append(f"{rel} (file missing)")
            continue
        defined = _python_symbols(path) if rel.endswith(".py") else _js_symbols(path)
        for symbol in sorted(wanted):
            if symbol not in defined:
                broken.append(f"{rel}::{symbol}")

    assert not broken, (
        "docs/session-project-operations.md names symbols that are not "
        "defined where it says they are. Either the code moved and the chart "
        "is now fiction, or the citation is a typo:\n  "
        + "\n  ".join(broken)
    )


def test_every_cited_route_is_declared_by_a_router():
    """A route the chart names must exist on some router under src/api/."""
    text = _read_chart()
    declared = _declared_routes()
    assert declared, (
        "CANNOT DETERMINE: no route decorators were parsed out of any router "
        "file, so no route citation was really checked."
    )

    broken: List[str] = []
    for method, route in _ROUTE_CITATION.findall(text):
        key = (method.upper(), _normalise_route(route))
        if key not in declared:
            broken.append(f"{method} {route}")

    assert not broken, (
        "docs/session-project-operations.md names routes that no router "
        f"declares: {sorted(set(broken))}"
    )


def test_the_gui_fork_the_chart_now_claims_actually_exists():
    """SUPERSEDES ``test_fork_is_still_unimplemented_where_the_chart_says_it_is``.

    Description: that test asserted fork's ABSENCE, because the chart's most
      load-bearing statement was that fork did not exist. It said, in its own
      docstring, that when fork was built it SHOULD fail and the assertion
      should be replaced rather than deleted quietly. Fork was built on
      2026-08-26; this is the replacement.

      The claim has flipped, so the assertion flips with it. Section 4 now
      states that the GUI fork ships, and a chart that claims a route exists
      is exactly as misleading as one that claims it does not if nobody
      checks. So: the route must exist, the client must call it, and the CLI
      fork - which is still specification, and is the INVERSE shape - must
      still be marked NOT IMPLEMENTED.
    """
    declared = _declared_routes()
    fork_routes = sorted(r for _, r in declared if "fork" in r.lower())
    assert fork_routes, (
        "docs/session-project-operations.md section 4 says the GUI fork "
        "ships, but no router declares a fork route."
    )

    client = REPO_ROOT / "client" / "js"
    callers: List[str] = []
    if client.is_dir():
        for js in sorted(client.glob("*.js")):
            body = js.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bforkSession\b", body):
                callers.append(str(js.relative_to(REPO_ROOT)))
    assert callers, (
        "section 4 says the fork is reachable from the interface, but no "
        "client file calls forkSession."
    )

    chart = (REPO_ROOT / "docs" / "session-project-operations.md").read_text(
        encoding="utf-8"
    )
    assert "fork - CLI | **NOT IMPLEMENTED**" in chart, (
        "the CLI fork is still unbuilt and is the INVERSE shape of the GUI "
        "fork; the chart must keep saying so."
    )
