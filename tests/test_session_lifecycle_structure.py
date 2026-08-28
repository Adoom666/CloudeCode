"""THE STRUCTURAL PROOF: a failed probe CANNOT write a lifecycle.

A behavioural test only samples the branches it thought to try. These
walk the reconciler's AST instead and assert its shape: every write
statement lives in one private function, that function has exactly one
call site, the call site sits past every gate, and the gate itself is the
first statement of the entry point and refuses by returning.

Both S4 adversarial rounds found the hole one layer BELOW the behavioural
proof. This is that layer.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_lifecycle_structure.py -v
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.lifecycle_helpers import (
    ENTRY_FUNCTION,
    MODULE_PATH,
    ROOT,
    SOCKET,
    WRITER_FUNCTION,
    CountingConnection,
    ExplodingConnection,
    add_row,
    conn,
    function_named,
    live,
    module_ast,
    row_by_uuid,
    sql_literals,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

import ast

from src.core import session_lifecycle
from src.core.db_models import SESSION_LIFECYCLE_STOPPED


# ===========================================================================
# PART 5 - THE STRUCTURAL PROOF
# a behavioural test only samples the branches it thought to try
# ===========================================================================


def module_ast():
    """Parse the reconciler module into an AST.

    Inputs: none.
    Output: ast.Module.
    """
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def function_named(tree, name):
    """Find one top-level function definition by name.

    Inputs: tree (ast.Module). name (str).
    Output: ast.FunctionDef.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not a top-level function any more")


def sql_literals(node):
    """Every string constant under ``node`` that reads like SQL.

    Inputs: node (ast.AST).
    Output: list[str] - upper-cased, whitespace-collapsed.
    """
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = " ".join(sub.value.split()).upper()
            if text.startswith(("UPDATE ", "INSERT ", "DELETE ", "REPLACE ")):
                out.append(text)
    return out


def test_every_write_statement_lives_in_the_single_writer_function():
    """No SQL that changes a row may exist outside the gated writer."""
    tree = module_ast()
    writer = function_named(tree, WRITER_FUNCTION)
    all_writes = sql_literals(tree)
    writer_writes = sql_literals(writer)
    assert all_writes, "the module contains no write SQL at all - did it move?"
    assert all_writes == writer_writes, (
        "write SQL exists outside "
        f"{WRITER_FUNCTION}: {sorted(set(all_writes) - set(writer_writes))}"
    )


def test_the_writer_has_exactly_one_call_site_and_it_is_the_entry_point():
    """One door in. A second caller could bypass the gate."""
    tree = module_ast()
    callers = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                if sub.func.id == WRITER_FUNCTION:
                    callers.append(node.name)
    assert callers == [ENTRY_FUNCTION], (
        f"{WRITER_FUNCTION} must have exactly one call site, in "
        f"{ENTRY_FUNCTION}; found {callers}"
    )


def test_the_ok_gate_is_the_first_statement_of_the_entry_point():
    """The gate returns before anything else can run. Proven, not promised."""
    entry = function_named(module_ast(), ENTRY_FUNCTION)
    body = [n for n in entry.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    )]
    first = body[0]
    assert isinstance(first, ast.If), (
        "the first statement of the entry point must be the ok gate, "
        f"found {type(first).__name__}"
    )
    test = first.test
    assert isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not), (
        "the gate must be `if not listing.ok:`"
    )
    operand = test.operand
    assert isinstance(operand, ast.Attribute) and operand.attr == "ok", (
        "the gate must read listing.ok"
    )
    assert isinstance(operand.value, ast.Name) and operand.value.id == "listing"
    assert len(first.body) == 1 and isinstance(first.body[0], ast.Return), (
        "the ok branch must do exactly one thing: return"
    )
    assert sql_literals(first) == [], "the gated branch contains write SQL"


def test_the_writer_is_called_only_after_every_gate():
    """The call to the writer is the LAST statement, past all three gates."""
    entry = function_named(module_ast(), ENTRY_FUNCTION)
    body = [n for n in entry.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    )]
    gates = [n for n in body if isinstance(n, ast.If)]
    assert len(gates) >= 3, (
        "expected three refusals before the writer: ok, complete, table"
    )
    for gate in gates:
        assert all(isinstance(s, ast.Return) for s in gate.body), (
            "every gate must refuse by returning, never by falling through"
        )
    last = body[-1]
    assert isinstance(last, ast.Return)
    assert isinstance(last.value, ast.Call)
    assert isinstance(last.value.func, ast.Name)
    assert last.value.func.id == WRITER_FUNCTION
    assert body.index(last) > body.index(gates[-1])


def test_the_entry_point_itself_executes_no_sql():
    """No conn.execute in the gating function, so nothing can precede a gate."""
    entry = function_named(module_ast(), ENTRY_FUNCTION)
    for sub in ast.walk(entry):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            assert sub.func.attr not in ("execute", "executemany", "commit"), (
                f"{ENTRY_FUNCTION} must not touch the connection directly"
            )


def test_the_writer_never_writes_archived_at_or_deletes():
    """The column list is a guarantee: archiving is a user decision."""
    writer = function_named(module_ast(), WRITER_FUNCTION)
    for statement in sql_literals(writer):
        assert not statement.startswith("DELETE"), "the reaper must not DELETE"
        assert "ARCHIVED_AT" not in statement, (
            "archived_at must never appear in a write statement"
        )
        assert "ORIGIN" not in statement
        assert "ADOPTED_AT" not in statement
        assert "SESSION_UUID" not in statement
        assert "LAST_SEEN_RUNNING_AT" not in statement


def test_the_module_names_no_lifecycle_but_stopped_in_its_writes():
    """Only ``running -> stopped``. Nothing here can write ``unknown``."""
    src = session_lifecycle
    assert src.SESSION_LIFECYCLE_STOPPED == SESSION_LIFECYCLE_STOPPED
    tree = module_ast()
    names = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id.startswith("SESSION_LIFECYCLE")
    }
    assert "SESSION_LIFECYCLE_UNKNOWN" not in names



def test_the_update_reasserts_running_at_the_write_layer():
    """Defense in depth: the SELECT filters, and the UPDATE says so again.

    Behaviourally redundant TODAY - which is exactly why it needs a
    structural test. A future caller handing rows in from elsewhere would
    otherwise silently lose the guarantee, and no behavioural test in
    this file would notice.
    """
    writer = function_named(module_ast(), WRITER_FUNCTION)
    updates = [s for s in sql_literals(writer) if s.startswith("UPDATE")]
    assert updates, "the writer contains no UPDATE any more"
    for statement in updates:
        where = statement.split(" WHERE ", 1)
        assert len(where) == 2, f"an UPDATE with no WHERE clause: {statement}"
        assert "LIFECYCLE = ?" in where[1], (
            "the UPDATE must re-assert lifecycle in its WHERE clause, not "
            f"rely on the SELECT alone: {statement}"
        )
        assert "ID = ?" in where[1], "the UPDATE must be keyed on one row id"


