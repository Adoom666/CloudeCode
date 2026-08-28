"""Every route handler's global names must actually resolve.

THE DEFECT. ``run_in_threadpool`` was imported inside two individual
handlers rather than at module scope. Two NEW handlers - the fork endpoint
and the LM Studio model list - used it anyway. Python does not complain at
import time about a name a function will look up later, so both modules
loaded fine, both routes registered fine, and both raised
``NameError: name 'run_in_threadpool' is not defined`` the first time
anyone called them. FastAPI turns that into a bare 500 with no body.

WHY IT COST SO MUCH. A 500 with no detail is unhelpable, so the failure
looked like it could be anything: a database problem, a tmux problem, a
permissions problem. It was a missing import, and the endpoint could not
say so. This checks the class rather than the instance.
"""

from __future__ import annotations

import ast
import builtins
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
import src.api.routes as routes

ROUTES_SRC = ROOT / "src/api/routes.py"


def _locally_bound(fn: ast.AST) -> set:
    """Names bound anywhere inside a function: params, assignments, imports."""
    bound = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    bound.add(item.optional_vars.id)
        elif isinstance(node, ast.Lambda):
            # A lambda's parameters are bindings too. Missing these
            # reported `sorted(..., key=lambda p: p.name)` as an
            # unresolved `p`, which is the check being wrong about
            # correct code - the exact false-positive direction that
            # makes a structural test get deleted instead of trusted.
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.comprehension):
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
    return bound


def test_every_route_handler_resolves_its_helper_names():
    """A name a handler will look up at CALL time must exist somewhere.

    Checked against the module's real namespace, plus builtins, plus what
    the function binds itself, plus what its ENCLOSING functions bind - a
    nested helper legitimately closes over its parent's imports and
    locals, and a check that ignores that reports a wall of false
    positives (this one did, first time).

    That is exactly the lookup Python performs, so a name that fails here
    fails in production - as run_in_threadpool did, in two routes, as a
    bare 500 with no body.
    """
    tree = ast.parse(ROUTES_SRC.read_text())
    module_names = set(vars(routes)) | set(dir(builtins))
    offenders = []

    def walk(node, enclosing):
        """Recurse, carrying the accumulated visible bindings."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visible = enclosing | _locally_bound(node)
            for child in node.body:
                walk(child, visible)
            for inner in ast.walk(node):
                if not (isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load)):
                    continue
                if inner.id in visible or inner.id in module_names:
                    continue
                offenders.append(f"{node.name}:{inner.lineno} -> {inner.id}")
            return
        for child in ast.iter_child_nodes(node):
            walk(child, enclosing)

    walk(tree, set())

    assert not offenders, (
        "route handlers reference names that resolve nowhere; each is a "
        f"NameError and a bare 500 the first time it is called: "
        f"{sorted(set(offenders))[:10]}"
    )


def test_run_in_threadpool_is_available_at_module_scope():
    """The specific regression, named.

    It used to be imported inside individual handlers, which is why a new
    handler could use it and fail only when called.
    """
    assert hasattr(routes, "run_in_threadpool"), (
        "run_in_threadpool is not at module scope; a handler that uses it "
        "without its own local import will NameError at call time"
    )
