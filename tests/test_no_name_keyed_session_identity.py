"""Structural guard: no NEW sessions-row identity lookup keyed on tmux
name alone.

WHY THIS FILE EXISTS. Four separate bugs shipped in one week, all one
class: something decided WHICH sessions row a live tmux pane IS, or which
row to write durable state onto, using the tmux NAME as the key -

  * the home screen joined live sessions to stored rows by name (a
    running session VANISHED from its project)
  * activity state was written to every row sharing a name (a dead row
    got the live row's status)
  * the client and server derived a display name from a tmux name by
    different rules
  * fork resolution and identity_for_live_name still pick a row by name
    today

A tmux NAME is not durable identity. tmux recycles names the moment a
session is recreated after its pane dies, so two SESSIONS - two different
rows, two different histories - can legitimately share one name. The
durable keys in this schema are ``sessions.id`` / ``session_uuid``, the
INSTANCE TRIPLE (``tmux_socket``, ``tmux_name``, ``tmux_created_epoch``),
and ``claude_session_uuid``. Each of the four bugs above picked a row by
name and then treated the result as if it were keyed on one of those.

Fixing the four instances does not close the class. Nothing stops a fifth
call site from doing the exact same thing next week. This is that stop.

WHAT COUNTS AS A VIOLATION. This file scans every ``*.execute(...)`` call
in ``src/`` whose literal SQL text:

  1. touches the ``sessions`` table (``FROM sessions`` or
     ``UPDATE sessions`` - never ``session_group_members``, a different
     table with its own, separately registered problem - see below), AND
  2. filters its WHERE clause on ``tmux_name = ?``, AND
  3. does NOT also filter on an EXACT ``tmux_created_epoch = ?`` value.

(3) is the line that actually separates safe from unsafe. A query that
requires ``tmux_created_epoch = <a specific value>`` is keyed on the
INSTANCE TRIPLE, which is durable identity - a name is only ever the
first half of the key. A query that merely checks
``tmux_created_epoch IS NOT NULL`` and then takes the newest row
(``ORDER BY tmux_created_epoch DESC ... LIMIT 1``) is NOT keyed on the
triple: it is keyed on the name, with recency used as a heuristic
tiebreak. That heuristic is exactly the bug - "the most recent session
with this name" is a guess, not a fact, the moment two sessions have
shared the name.

WHAT IS DELIBERATELY ALLOWED, and does not need an entry in the
exemption list at all because it never matches the pattern above:

  * rendering a name to a human (no SQL involved)
  * a tmux CLI call that addresses a session by name (tmux has no other
    handle - this file only looks at ``sessions`` table SQL)
  * a listing/candidate-set query with no ``tmux_name = ?`` equality
    filter at all (e.g. ``session_lifecycle.py``'s
    ``_running_candidates``, which scans every running row on a socket
    for further per-row reconciliation - it is not picking THE row for
    a name)
  * a query keyed on an EXACT epoch value
    (``tmux_created_epoch = ?``) - the whole ``session_import_promote.py``
    module is this shape throughout, which is why nothing in it appears
    below despite an earlier draft of this task's brief expecting a
    finding at line ~61: verified against the live file, every WHERE
    clause there pins an exact epoch, so it is not a name-only key at
    all and does not belong in the exemption list. Registering it
    anyway would be dead weight - an exemption for a site that was never
    a violation, which is exactly the kind of furniture this repo's own
    hazard list warns against.

WHAT IS **NOT** ALLOWED, and needs a registered exemption with a reason:
selecting or updating a ``sessions`` row by name in order to establish
IDENTITY - to decide which row a live session IS, or which row to write
durable state onto. Every current instance is below, verified against
the live file at the time this guard was written; NONE of them is fixed
by this file. This is a guard, not a fix.

THE GROUP MEMBERSHIP TABLE, FIXED IN SCHEMA v24. It used to be the
worst instance of this class and the one this file could not scan:
``session_group_members`` declared ``tmux_name TEXT PRIMARY KEY``
(``src/core/db_models.py``, ``DDL_SESSION_GROUP_MEMBERS``) - a SCHEMA
asserting that an ephemeral name IS an identity, an enforced INCORRECT
contract and worse than no contract at all. v24 adds
``session_group_membership`` keyed on ``sessions.session_uuid`` and
carries every old row across (``src/core/db_steps.py``'s
``_step_v23_to_v24``). The v8 table is LEFT IN PLACE and unread, because
every migration step in this project is additive; that is why
``test_group_membership_primary_key_is_a_known_bad_contract`` still
asserts the old DDL text unchanged, and why a second test asserts the
new table exists and keys on the durable id. The pair is what stops the
fix from silently regressing in either direction.
"""

from __future__ import annotations

import ast
import re
import pathlib
from dataclasses import dataclass
from typing import List, Optional

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DB_MODELS = SRC / "core" / "db_models.py"

#: A query touches the sessions table's own identity surface, never the
#: unrelated membership table (``session_group_members`` does not match
#: this word-boundary pattern, which is deliberate - see module
#: docstring). INSERT is excluded on purpose: creating a row is not
#: resolving one, so it cannot exhibit this class of bug.
TABLE_RE = re.compile(r"\b(FROM|UPDATE)\s+sessions\b", re.IGNORECASE)

#: An equality filter on the name alone.
NAME_EQ_RE = re.compile(r"tmux_name\s*=\s*\?", re.IGNORECASE)

#: An EXACT epoch filter - the thing that turns a name into an instance
#: triple. ``tmux_created_epoch IS NOT NULL`` does not satisfy this; only
#: an equality against a bound parameter does.
EPOCH_EQ_RE = re.compile(r"tmux_created_epoch\s*=\s*\?", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    """One ``sessions`` query that picks a row by name with no exact
    epoch to anchor it.

    Description: everything a reviewer needs to find and judge the site
      without re-running the scan.
    Inputs: path (pathlib.Path) - the source file. lineno (int) - where
      the ``.execute(...)`` call starts. func (str) - the enclosing
      function/method name, or ``"<module>"``. sql (str) - the literal
      SQL text found.
    Output: n/a (data holder).
    """

    path: pathlib.Path
    lineno: int
    func: str
    sql: str

    @property
    def key(self) -> tuple:
        """The allowlist key: (relative path, enclosing function name).

        Description: keyed on the FUNCTION, not the line number, on
          purpose. A line number drifts every time someone edits
          anything above the call in the same file, which would make an
          already-reviewed exemption spuriously fail this test on
          unrelated work - exactly the kind of noise that gets a check
          disabled rather than fixed. The function name is what a
          reviewer actually reasons about, and it only changes when the
          function itself is renamed or removed, which is precisely when
          the exemption SHOULD be re-examined.
        Output: tuple[str, str].
        """
        return (str(self.path.relative_to(SRC.parent)), self.func)


def _literal_sql(node: ast.AST) -> Optional[str]:
    """Fold a call argument into its literal SQL text, or give up.

    Description: handles the three shapes this codebase actually uses -
      a plain string constant (adjacent string literals are already
      merged into one ``ast.Constant`` by the parser, so the common case
      needs nothing special), an f-string whose interpolated parts are
      unrelated helper clauses (treated as empty text, which is safe
      here because every f-string call site in this codebase interpolates
      an eligibility/origin clause, never a tmux_name or epoch token), and
      ``+`` concatenation of literals. Anything else (a name built from a
      variable, a function call with no literal text) returns None rather
      than guessing, because a query this scan cannot read is a query it
      must not claim a verdict about - it is simply not counted, not
      silently passed.
    Inputs: node (ast.AST) - the first argument to an ``.execute(...)``
      call.
    Output: str | None.
    Example: _literal_sql(ast.parse("'SELECT 1'", mode='eval').body) ->
      'SELECT 1'
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_sql(node.left)
        right = _literal_sql(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _iter_execute_calls(tree: ast.Module):
    """Every ``*.execute(...)`` call in a module, with its enclosing
    function name.

    Description: a plain recursive walk rather than ``ast.walk`` because
      the enclosing-function context has to travel WITH each node, and
      ``ast.walk`` throws that context away. Each recursive branch
      receives its own copy of the function-name stack (built with `+`,
      never mutated in place), so a call inside one method is never
      mislabeled with a sibling method's name.
    Inputs: tree (ast.Module).
    Output: list[tuple[int, str, ast.Call]] - (lineno, func name or
      "<module>", the Call node).
    """
    results: List[tuple] = []

    def visit(node: ast.AST, stack: List[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + [node.name]
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "execute":
                results.append((node.lineno, stack[-1] if stack else "<module>", node))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return results


def _scan_file(path: pathlib.Path) -> List[Violation]:
    """Every name-keyed-without-exact-epoch ``sessions`` query in one file.

    Inputs: path (pathlib.Path).
    Output: list[Violation].
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: List[Violation] = []
    for lineno, func, call in _iter_execute_calls(tree):
        if not call.args:
            continue
        sql = _literal_sql(call.args[0])
        if not sql:
            continue
        if not TABLE_RE.search(sql):
            continue
        where_idx = sql.upper().find("WHERE")
        where_clause = sql[where_idx:] if where_idx != -1 else ""
        if not NAME_EQ_RE.search(where_clause):
            continue
        if EPOCH_EQ_RE.search(where_clause):
            continue
        found.append(Violation(path=path, lineno=lineno, func=func, sql=sql))
    return found


def find_violations(root: pathlib.Path = SRC) -> List[Violation]:
    """Every name-keyed-without-exact-epoch ``sessions`` query under a root.

    Inputs: root (pathlib.Path) - defaults to ``src/``.
    Output: list[Violation], sorted by (path, lineno) for stable output.
    Example: find_violations()  # -> [] once every site is fixed
    """
    out: List[Violation] = []
    for path in sorted(root.rglob("*.py")):
        out.extend(_scan_file(path))
    out.sort(key=lambda v: (str(v.path), v.lineno))
    return out


# ---------------------------------------------------------------------
# THE EXEMPTION LIST. Every entry is a REAL, CURRENT site (verified
# against the live file when this guard was written), each carrying a
# one-line reason. This is the honest inventory of what is still wrong,
# per this repo's own THREE-OUTCOME RULE: a check that quietly excludes
# what it cannot fix is worse than one that names it. A new, unregistered
# site fails the test below - that is the entire point of an allowlist
# over a denylist here: nobody can add a fifth instance of this bug
# without this file demanding they either fix it on the spot or write
# down why not.
#
# Key: (path relative to the repo root, enclosing function name).
# ---------------------------------------------------------------------
EXEMPTIONS = {
    (
        "src/core/session_group_membership_migrate.py",
        "resolve_session_uuid",
    ): (
        "THE BOUNDARY, not a lookup that could be keyed better. It is the "
        "ONE place a tmux name becomes a durable session_uuid, and it "
        "exists because the sidebar addresses its rows by name while the "
        "v24 membership table keys on session_uuid; the guess is now made "
        "once, explicitly, with a stated rule (running row, else newest) "
        "instead of being frozen into a primary key on every row of "
        "session_group_members. The blast radius is bounded: the wrong "
        "answer files a conversation in a group, which is user-visible, "
        "reversible with one drag, and touches no lifecycle, no lineage "
        "and no conversation. The instance triple is not available here - "
        "the caller has a name and nothing else."
    ),
    (
        "src/core/session_store.py",
        "identity_for_live_name",
    ): (
        "KNOWN BUG, unfixed by this guard. Returns id + parent_session_id "
        "for the newest row sharing a name, and the result reaches the "
        "client - exactly the class this file exists to stop the NEXT "
        "instance of. Its own docstring already says the guarantee is "
        "weaker than an exact key."
    ),
    (
        "src/core/session_fork.py",
        "resolve_fork_source",
    ): (
        "KNOWN BUG, unfixed by this guard. Finds the row to fork FROM by "
        "newest-row-for-name; a forked session's parent can be resolved "
        "to the wrong session if the name was reused."
    ),
    (
        "src/core/session_fork.py",
        "newest_anchor_uuid",
    ): (
        "KNOWN BUG, unfixed by this guard. Finds the row a fork just "
        "created by newest-row-for-name, to stamp lineage onto it; a "
        "reused name can stamp lineage onto the wrong row."
    ),
    (
        "src/core/session_manager.py",
        "_claude_uuid_for_tmux_name",
    ): (
        "KNOWN BUG, unfixed by this guard. Resolves the Claude conversation "
        "uuid to push an out-of-band rename into by newest-row-for-name; a "
        "reused name can push the rename into the wrong conversation. "
        "session_manager.py is excluded from edits by this task - flagged "
        "here, not fixed here."
    ),
    (
        "src/core/session_manager.py",
        "_restored_activity_state",
    ): (
        "KNOWN BUG, unfixed by this guard, and NOT one of the sites named "
        "in this task's brief - found by this scan itself. Reads "
        "activity_state / activity_state_at off the newest row sharing a "
        "name; this is the exact 'activity state written to every row "
        "sharing a name' bug class from this task's own background, one "
        "call site the brief's enumeration missed. session_manager.py is "
        "excluded from edits by this task - flagged here, not fixed here."
    ),
    (
        "src/core/session_label.py",
        "label_for_name",
    ): (
        "ALLOWED, not debt. Reads ONLY the title/label for display - never "
        "an id, a uuid, or anything used to decide which row IS the "
        "session or to write durable state. This is exactly the "
        "'deriving a display label from it' carve-out this guard's own "
        "spec names session_label.py for. Its own docstring already "
        "documents the one gap it cannot cover (a live external session "
        "with no row at all) and points callers at the instance-keyed "
        "sibling wherever an epoch is available."
    ),
}


def test_no_new_name_keyed_sessions_identity_lookup():
    """Every ``sessions`` query keyed on name alone must be a reviewed,
    registered exemption - never a silent new one.

    A site that is not in EXEMPTIONS is either a regression of one of the
    known bugs above, or a brand new instance of the same class. Either
    way the fix is the same: resolve the row through the instance triple
    (``tmux_socket``, ``tmux_name``, ``tmux_created_epoch``) or
    ``claude_session_uuid``/``session_uuid``, not through name plus a
    recency guess - or, if the site turns out to be a legitimate
    display-only read like ``label_for_name``, register it here with a
    reason that says so.
    """
    found = find_violations()
    found_keys = {v.key for v in found}
    unregistered = [v for v in found if v.key not in EXEMPTIONS]
    assert not unregistered, (
        "new (or newly regressed) sessions-row identity lookup(s) keyed on "
        "tmux_name alone, with no exact tmux_created_epoch to anchor them:\n"
        + "\n".join(
            f"  {v.path.relative_to(SRC.parent)}:{v.lineno} "
            f"in {v.func}() - {v.sql[:160]!r}"
            for v in unregistered
        )
        + "\n\nA tmux name is reused every time a session is recreated "
        "after its pane dies, so 'the newest row with this name' is a "
        "guess, not identity. Key the lookup on the instance triple "
        "(tmux_socket, tmux_name, tmux_created_epoch) or on "
        "claude_session_uuid/session_uuid instead. If this read is "
        "genuinely display-only (a label, never used to decide which row "
        "IS the session), register it in EXEMPTIONS in "
        "tests/test_no_name_keyed_session_identity.py with a one-line "
        "reason instead of fixing it."
    )

    # The other direction matters too: an exemption for a site that no
    # longer exists is dead weight that will mislead the next reader into
    # believing a bug is still there, or that this guard is watching a
    # function that has been deleted or renamed out from under it.
    stale = sorted(set(EXEMPTIONS) - found_keys)
    assert not stale, (
        "exemption(s) registered for a site this scan no longer finds - "
        "the function was fixed, renamed, or removed, and the entry is "
        "now dead weight:\n"
        + "\n".join(f"  {path}::{func}" for path, func in stale)
        + "\n\nRemove the stale entry from EXEMPTIONS (or, if it was "
        "fixed, celebrate and remove it)."
    )


def test_the_guard_can_actually_fail():
    """Positive control: prove find_violations() can return a finding.

    This repo's own history has several checks that were never once
    observed to fail before shipping - a guard nobody has seen catch
    anything is indistinguishable from a guard that cannot. This does
    not touch src/; it builds a throwaway module in a temp directory that
    reproduces the exact shape of the four real bugs (name equality, no
    exact epoch, ORDER BY ... LIMIT 1) and asserts the scanner flags it.
    """
    import tempfile

    src = (
        "import sqlite3\n\n"
        "def resolve_by_name_only(conn, name):\n"
        "    return conn.execute(\n"
        "        \"SELECT id FROM sessions WHERE tmux_name = ? \"\n"
        "        \"AND tmux_created_epoch IS NOT NULL \"\n"
        "        \"ORDER BY tmux_created_epoch DESC LIMIT 1\",\n"
        "        (name,),\n"
        "    ).fetchone()\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        planted = tmp_path / "planted_violation.py"
        planted.write_text(src, encoding="utf-8")
        found = find_violations(root=tmp_path)
    assert len(found) == 1, (
        f"expected exactly one planted violation, scanner found {len(found)}"
    )
    assert found[0].func == "resolve_by_name_only"


def test_epoch_exact_match_is_not_flagged():
    """Negative control, same shape, one line different: a query keyed on
    an EXACT epoch value must never be flagged.

    Without this, a future edit to the scanner's regex could start
    treating every ``sessions`` + ``tmux_name`` query as a violation,
    which would make the exemption list meaningless (everything would
    need one) and bury the real signal.
    """
    import tempfile

    src = (
        "import sqlite3\n\n"
        "def resolve_by_instance_triple(conn, socket, name, epoch):\n"
        "    return conn.execute(\n"
        "        \"SELECT id FROM sessions WHERE tmux_socket = ? \"\n"
        "        \"AND tmux_name = ? AND tmux_created_epoch = ?\",\n"
        "        (socket, name, epoch),\n"
        "    ).fetchone()\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "safe.py").write_text(src, encoding="utf-8")
        found = find_violations(root=tmp_path)
    assert found == []


def test_group_membership_primary_key_is_a_known_bad_contract():
    """The RETIRED v8 table still declares its bad key, untouched.

    Description: this asserts the OLD table's DDL is unchanged, which
      after schema v24 is a statement about the MIGRATION rather than
      about the feature: every step in this project is additive, so v24
      created a new correctly-keyed table and left ``session_group_members``
      exactly as it was rather than dropping or retyping it. A step that
      quietly "tidied" it would break the additive-forward /
      restore-backward rollback design that db_steps.py's docstring
      rests on, and this is what would notice.

      The live contract is asserted by
      ``test_group_membership_is_now_keyed_on_durable_identity`` below;
      read the two together.
    Output: n/a (assertion only).
    """
    ddl_text = DB_MODELS.read_text(encoding="utf-8")
    match = re.search(
        r"DDL_SESSION_GROUP_MEMBERS\s*=\s*\"\"\"(.*?)\"\"\"",
        ddl_text,
        re.DOTALL,
    )
    assert match is not None, (
        "src/core/db_models.py no longer defines DDL_SESSION_GROUP_MEMBERS "
        "- this test needs updating, but first confirm the group "
        "membership table's key was not just silently changed underneath "
        "it without anyone reading this test."
    )
    table_sql = match.group(1)
    assert re.search(r"tmux_name\s+TEXT\s+PRIMARY\s+KEY", table_sql, re.IGNORECASE), (
        "the RETIRED v8 session_group_members table was edited. Schema "
        "v24 superseded it with session_group_membership and every "
        "migration step in this project is additive, so the old table is "
        "supposed to sit there unread and unchanged. If you meant to "
        "retire it for real, that is a schema decision with a rollback "
        "consequence - read src/core/db_steps.py's docstring first."
    )


def test_group_membership_is_now_keyed_on_durable_identity():
    """``session_group_membership`` keys on ``session_uuid``, not a name.

    Description: the live contract, asserted on the DDL text directly
      rather than inferred from behaviour. The whole point of the v24
      re-key is that two sessions sharing one tmux name hold two
      memberships, and that a session with NO tmux name - one imported
      from a transcript - can hold one at all. A key that drifted back
      to the name would silently take both of those away.
    Output: n/a (assertion only).
    """
    ddl_text = DB_MODELS.read_text(encoding="utf-8")
    match = re.search(
        r"DDL_SESSION_GROUP_MEMBERSHIP\s*=\s*\"\"\"(.*?)\"\"\"",
        ddl_text,
        re.DOTALL,
    )
    assert match is not None, (
        "src/core/db_models.py no longer defines DDL_SESSION_GROUP_MEMBERSHIP "
        "- the v24 membership table. Confirm the durable key was not "
        "removed before updating this test."
    )
    table_sql = match.group(1)
    assert re.search(
        r"session_uuid\s+TEXT\s+PRIMARY\s+KEY", table_sql, re.IGNORECASE
    ), (
        "session_group_membership no longer keys on session_uuid. A tmux "
        "name is not durable identity - see this module's docstring."
    )
    assert re.search(r"\bposition\s+INTEGER\b", table_sql, re.IGNORECASE), (
        "session_group_membership lost its `position` column, which is "
        "the durable order within a group."
    )
