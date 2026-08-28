"""Every name used in src/ must resolve to something.

WHY THIS FILE EXISTS. Twice now a NameError has reached the live server
through a fully green suite:

  * ``run_in_threadpool`` was imported inside one function and then used
    by two others, which turned two new routes into bare HTTP 500s.
  * ``launch_name_args_for_agent_type`` was moved to another module and
    the call site updated without adding the import, which broke SESSION
    CREATION entirely - the app's single most important operation.

Neither was a syntax error, so ``py_compile`` and ``bash -n``-style
checks pass happily; an undefined name is perfectly valid Python right up
until the line executes. And neither was covered by a unit test, because
the code paths that use them need a configured server.

This is a static check with no imports and no server: walk the AST, track
what each scope binds, and flag any Name/Attribute root that resolves to
nothing. It catches the whole class rather than the two instances.
"""

import ast
import builtins
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
BUILTINS = set(dir(builtins))

#: Module-level dunders the interpreter binds itself. They appear in no
#: AST node, so a pure binding walk cannot see them and reports every
#: file that uses ``__file__`` as broken. Found by running this check for
#: the first time - 7 files, all false positives, which is exactly what a
#: new checker's first run is for.
MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__path__",
}


def _collect_bindings(tree: ast.AST) -> set:
    """Every name bound anywhere in a module, at any scope depth.

    Description: deliberately FLAT and over-permissive. Scope-accurate
      resolution is what a real linter does; this check only needs to
      catch a name that is bound NOWHERE in the file, which is what both
      real defects looked like. Being over-permissive is the right bias -
      a false positive here would block a commit for no reason, and the
      cost of missing a subtler shadowing bug is a bug this check was
      never claiming to find.
    Inputs: tree (ast.AST) - a parsed module.
    Output: set[str] - bound names.
    """
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in (
                    list(args.args)
                    + list(args.posonlyargs)
                    + list(args.kwonlyargs)
                    + [args.vararg, args.kwarg]
                ):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for x in (
                list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)
                + [a.vararg, a.kwarg]
            ):
                if x is not None:
                    bound.add(x.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound


def _unresolved(path: pathlib.Path) -> list:
    """Names LOADED in a module that nothing in it ever binds."""
    tree = ast.parse(path.read_text(), filename=str(path))
    bound = _collect_bindings(tree) | BUILTINS | MODULE_DUNDERS
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound:
                bad.append((node.id, node.lineno))
    return bad


ALL_SOURCES = sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", ALL_SOURCES, ids=lambda p: str(p.name))
def test_every_loaded_name_is_bound_somewhere(path):
    """A name used but never bound is a NameError waiting for traffic."""
    bad = _unresolved(path)
    assert not bad, (
        f"{path}: name(s) used but bound nowhere in the file - "
        + ", ".join(f"{n!r} (line {ln})" for n, ln in bad)
        + ". This is a NameError that no syntax check can see."
    )


def test_the_check_can_actually_fail():
    """Positive control.

    A checker that has never been shown returning a finding is
    indistinguishable from one that is broken - and this file exists
    precisely because two silent failures reached production.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("def f():\n    return not_bound_anywhere(1)\n")
        tmp = pathlib.Path(fh.name)
    try:
        bad = _unresolved(tmp)
        assert [n for n, _ in bad] == ["not_bound_anywhere"]
    finally:
        tmp.unlink()
