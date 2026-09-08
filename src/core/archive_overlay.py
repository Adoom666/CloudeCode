"""Reading the presentation overlay and layering it over the archive.

This module NEVER writes. It computes the identity key, loads the overlay
table, and applies it to project nodes that
:func:`src.core.archive_merged_tree.merged_projects` already produced.
Writes live in :mod:`src.core.archive_overlay_write`, which is the only
module in this feature that opens a writable connection - the split is
the guarantee, not a convention: nothing importable from a GET route can
write, because nothing here can.

THE FAILURE THAT THIS MODULE EXISTS TO NOT COMMIT. If the overlay cannot
be read - the table is missing on a database migrated by an older build,
the query errors, the file is locked - the tempting behaviour is to carry
on and render the archive's original names. That is a WRONG ANSWER WEARING
A RIGHT ONE'S CLOTHES: a project the owner renamed and a project he never
touched would render identically, and a project he HID would silently
reappear. So an overlay read failure is a CANNOT DETERMINE for the whole
response - ``result: None``, the reason named in ``unevaluated`` - and
never a quiet fallback to the un-overlaid list. :func:`load_overlay`
returns a three-outcome result object for exactly this reason; it has no
"return an empty map on error" path.

THE THREE STATES A PROJECT CAN BE IN, and they are three, not two:

  ``none``               no overlay row. The owner has never said
                         anything about this project. Its display name is
                         the archive's own derived name, untouched.
  ``applied``            an overlay row exists and was applied. What it
                         changed is itemised in ``overlay.applied``, so a
                         client can show "renamed from X" rather than
                         having to diff.
  ``cannot_determine``   the overlay could not be read. This never
                         appears on a per-node basis, because a failure
                         to read the table is a failure for every node at
                         once; it is reported at the response level and
                         the node list is not rendered at all.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.archive_overlay_ddl import OVERLAY_TABLE

#: Identity kinds, matching the CHECK constraint on the table.
IDENTITY_KIND_CWD: str = "cwd"
IDENTITY_KIND_PROJECT_ID: str = "project_id"

#: Prefixes for the two key forms. These are the SAME discriminated key
#: ``archive_project_names.merge_projects`` buckets on. They are written
#: here as constants rather than as literals at four call sites, and
#: ``tests/test_archive_overlay.py`` asserts that the keys this module
#: produces partition a real project row set exactly as the merge does -
#: two declarations of one rule have to be audited, not trusted.
KEY_PREFIX_CWD: str = "cwd:"
KEY_PREFIX_PROJECT_ID: str = "pid:"

#: Per-node overlay states. See this module's header for what each means.
OVERLAY_STATUS_NONE: str = "none"
OVERLAY_STATUS_APPLIED: str = "applied"
OVERLAY_STATUS_CANNOT_DETERMINE: str = "cannot_determine"

#: The ``unevaluated.subject`` used when the overlay cannot be read. One
#: string, so the route's status mapping and the test agree.
OVERLAY_SUBJECT: str = "archive:overlay"

#: Shipped in ``meta`` so a client cannot present an overlaid name as
#: though it were the archive's own, which is the one way this feature
#: could become dishonest without any code being wrong.
OVERLAY_MEANS: str = (
    "display_name here is a PRESENTATION override the owner set; it is "
    "stored in a separate table and the archive is not modified by it. "
    "archive_display_name is always carried alongside, unchanged, so the "
    "original is never lost and a client can show both. overlay_status "
    "is 'none' when the owner has said nothing about a project and "
    "'applied' when a row was found - those are different facts and a "
    "client must not infer 'none' from an unchanged name, because "
    "renaming a project to its own folder name is a thing a person may "
    "do. hidden projects are EXCLUDED from this list unless "
    "include_hidden is set; nothing about them is deleted and unhiding "
    "restores them exactly."
)


def identity_key(
    observed_cwd: Optional[str], project_id: Optional[int]
) -> Tuple[str, str]:
    """Compute the logical-project key an overlay row attaches to.

    Description: the ONE definition of project identity for this
      feature, and deliberately the same rule
      ``archive_project_names.merge_projects`` buckets on - a real path
      when there is one, the local project id when there is not. A
      non-blank ``observed_cwd`` wins because it is the only value that
      means the same thing on two machines; the id fallback is local to
      this database and is labelled as such so a caller is never left to
      infer portability from the shape of a string.
    Inputs: observed_cwd (str|None) - message_projects.observed_cwd, the
      real absolute path. project_id (int|None) - message_projects.id.
    Output: tuple[str, str] - (identity_key, identity_kind).
    Raises: ValueError - neither a usable path nor an id, which is a row
      that cannot be addressed at all and must not be given a key that
      collides with the next such row.
    Example: identity_key('/Users/j/CloudeCode', 4)
             -> ('cwd:/Users/j/CloudeCode', 'cwd')
    """
    if isinstance(observed_cwd, str) and observed_cwd.strip():
        return f"{KEY_PREFIX_CWD}{observed_cwd}", IDENTITY_KIND_CWD
    if project_id is not None:
        return f"{KEY_PREFIX_PROJECT_ID}{project_id}", IDENTITY_KIND_PROJECT_ID
    raise ValueError(
        "a project with neither observed_cwd nor a project id cannot be "
        "given an identity key; it is not addressable"
    )


def key_for_node(node: Dict[str, Any]) -> Tuple[str, str]:
    """Compute the identity key for one merged project node.

    Description: reads the node's own ``observed_cwd`` and
      ``project_id``, which ``merge_projects`` sets from the FIRST member
      of the bucket - the same values it keyed the bucket on, so this
      reproduces the merge's own key rather than approximating it.
    Inputs: node (dict) - one merged project node.
    Output: tuple[str, str] - (identity_key, identity_kind).
    Raises: ValueError - the node is not addressable; see identity_key.
    Example: key_for_node(nodes[0])[1] -> 'cwd'
    """
    return identity_key(node.get("observed_cwd"), node.get("project_id"))


class OverlayLoad:
    """The three-outcome result of trying to read the overlay table.

    Description: a load either produced a map or it did not, and the
      difference is not expressible as an empty dict - an archive where
      nothing has been renamed and an archive whose overlay could not be
      opened both have zero usable rows. ``ok`` is the discriminator and
      ``rows`` is None, not {}, when ``ok`` is False, so a caller that
      forgets to check gets a TypeError rather than a confident empty
      answer.
    Inputs: ok (bool), rows (dict|None) keyed by identity_key, reason
      (str|None) - why the read failed, for ``unevaluated``.
    Output: instance.
    Example: OverlayLoad(ok=False, rows=None, reason='no such table')
    """

    __slots__ = ("ok", "rows", "reason")

    def __init__(
        self,
        *,
        ok: bool,
        rows: Optional[Dict[str, Dict[str, Any]]],
        reason: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.rows = rows
        self.reason = reason


def load_overlay(conn: sqlite3.Connection) -> OverlayLoad:
    """Read every overlay row into a map keyed by identity_key.

    Description: the whole table in one scan. It is one row per project
      the owner has said something about - bounded by the project count,
      77 today - so paging it would add a failure mode to save nothing.
      A ``sqlite3.Error`` (missing table on an older database, a locked
      file, a corrupt page) becomes ``ok=False`` WITH the driver's own
      message, not an empty map: see this module's header for why an
      empty map here would be the exact false-green this codebase is
      written against.
    Inputs: conn (sqlite3.Connection) - read-only is fine and expected.
    Output: OverlayLoad.
    Example: load_overlay(conn).rows['cwd:/Users/j/x']['display_name']
    """
    try:
        cursor = conn.execute(
            f"SELECT identity_key, identity_kind, display_name, group_name, "
            f"hidden, created_at, updated_at FROM {OVERLAY_TABLE}"
        )
        rows = {
            str(row["identity_key"]): {
                "identity_key": str(row["identity_key"]),
                "identity_kind": str(row["identity_kind"]),
                "display_name": row["display_name"],
                "group_name": row["group_name"],
                "hidden": bool(row["hidden"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in cursor.fetchall()
        }
    except sqlite3.Error as exc:
        return OverlayLoad(ok=False, rows=None, reason=f"{type(exc).__name__}: {exc}")
    return OverlayLoad(ok=True, rows=rows)


def _apply_to_node(
    node: Dict[str, Any], row: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Layer one overlay row over one project node.

    Description: NON-DESTRUCTIVE by construction. The archive's own
      derived name is copied to ``archive_display_name`` BEFORE
      ``display_name`` is overwritten, so the original is still in the
      response and a client can render "shown as X, really Y" without a
      second request. Every node gains the overlay fields whether or not
      it has a row, because a field that appears only sometimes forces a
      client to treat absence as a value.
    Inputs: node (dict) - a merged project node. row (dict|None) - its
      overlay row, or None when the owner has said nothing about it.
    Output: dict - a NEW node dict; the input is not mutated.
    Example: _apply_to_node(node, None)['overlay']['status'] -> 'none'
    """
    out = dict(node)
    archive_name = node.get("display_name")
    out["archive_display_name"] = archive_name
    try:
        key, kind = key_for_node(node)
    except ValueError:
        # Unaddressable, so no overlay row can ever attach to it. That is
        # a real state and it is reported, not defaulted to 'none' -
        # 'none' means "nothing said", this means "nothing CAN be said".
        out["overlay"] = {
            "status": OVERLAY_STATUS_CANNOT_DETERMINE,
            "identity_key": None,
            "identity_kind": None,
            "reason": "project has neither observed_cwd nor a project id",
            "group": None,
            "hidden": False,
            "applied": [],
        }
        return out

    if row is None:
        out["overlay"] = {
            "status": OVERLAY_STATUS_NONE,
            "identity_key": key,
            "identity_kind": kind,
            "reason": None,
            "group": None,
            "hidden": False,
            "applied": [],
        }
        return out

    applied: List[str] = []
    if row.get("display_name") is not None:
        out["display_name"] = row["display_name"]
        applied.append("display_name")
    if row.get("group_name") is not None:
        applied.append("group")
    if row.get("hidden"):
        applied.append("hidden")
    out["overlay"] = {
        "status": OVERLAY_STATUS_APPLIED,
        "identity_key": key,
        "identity_kind": kind,
        "reason": None,
        "group": row.get("group_name"),
        "hidden": bool(row.get("hidden")),
        "applied": applied,
        "updated_at": row.get("updated_at"),
    }
    return out


def apply_overlay(
    nodes: Sequence[Dict[str, Any]],
    load: OverlayLoad,
    *,
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """Layer the overlay over a merged project list.

    Description: pure. Takes the nodes the archive read produced and the
      overlay load, and returns the presented list plus the accounting a
      caller needs to report honestly. Raises on a failed load rather
      than degrading, because there is no correct list to return when the
      overlay is unknown - the caller must turn that into a
      cannot_determine envelope, and a return value it could accidentally
      render would let it not.

      HIDDEN PROJECTS ARE FILTERED, NOT DROPPED. They are counted in
      ``hidden_count`` and every one of them is returned in ``hidden`` so
      a restore UI needs no second query and no second code path that
      could disagree with this one about what "hidden" means.

      ORPHANS - overlay rows whose project is not in this corpus - are
      returned by key and are NEVER turned into nodes. See
      archive_overlay_ddl's header.
    Inputs: nodes (sequence of dict) - merged project nodes.
      load (OverlayLoad). include_hidden (bool) - when True the hidden
      projects stay in ``nodes`` as well as appearing in ``hidden``.
    Output: dict with ``nodes``, ``hidden``, ``hidden_count``,
      ``orphans``, ``groups``, ``applied_count``.
    Raises: ValueError - ``load.ok`` is False.
    Example: apply_overlay(nodes, load)['hidden_count'] -> 1
    """
    if not load.ok or load.rows is None:
        raise ValueError(
            "apply_overlay was called with a failed overlay load; the "
            "caller must emit cannot_determine rather than render nodes "
            f"over an unknown overlay ({load.reason})"
        )
    rows = load.rows
    seen_keys: set = set()
    presented: List[Dict[str, Any]] = []
    hidden: List[Dict[str, Any]] = []
    applied_count = 0

    for node in nodes:
        try:
            key, _kind = key_for_node(node)
        except ValueError:
            key = None
        row = rows.get(key) if key is not None else None
        if key is not None:
            seen_keys.add(key)
        merged = _apply_to_node(node, row)
        if merged["overlay"]["status"] == OVERLAY_STATUS_APPLIED:
            applied_count += 1
        if merged["overlay"]["hidden"]:
            hidden.append(merged)
            if not include_hidden:
                continue
        presented.append(merged)

    orphans = [
        {
            "identity_key": row["identity_key"],
            "identity_kind": row["identity_kind"],
            "display_name": row["display_name"],
            "group": row["group_name"],
            "hidden": row["hidden"],
            "updated_at": row["updated_at"],
        }
        for key, row in sorted(rows.items())
        if key not in seen_keys
    ]

    groups: Dict[str, int] = {}
    for node in presented if include_hidden else presented + hidden:
        group = node["overlay"].get("group")
        if group is not None:
            groups[group] = groups.get(group, 0) + 1

    return {
        "nodes": presented,
        "hidden": hidden,
        "hidden_count": len(hidden),
        "orphans": orphans,
        "groups": [
            {"group": name, "project_count": count}
            for name, count in sorted(groups.items(), key=lambda kv: kv[0].lower())
        ],
        "applied_count": applied_count,
    }
