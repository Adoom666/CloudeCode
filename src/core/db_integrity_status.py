"""The reporting face of the database integrity check: verdict plus age.

WHY THIS IS SEPARATE FROM ``src/core/db_integrity.py``. That module RUNS
the check and owns the artifact; this one only READS it and decides what
may honestly be said. Keeping them apart is what lets the request path
depend on a module that cannot open a database at all, and it mirrors the
split between ``corpus_ingest_service.py`` and ``corpus_status.py``.

THE RULE THIS MODULE EXISTS TO ENFORCE. A cached verdict has a failure
mode a live one does not: it can be MISSING. So two orthogonal facts are
published as two fields and never collapsed:

  ``verdict``    ``ok`` | ``failed`` | ``cannot_determine``
  ``freshness``  ``current`` | ``stale`` | ``never_ran`` | ``cannot_determine``

  * the pragma returned "ok" AND the record is ``current``  -> ``ok``
  * the pragma returned anything else                       -> ``failed``,
    at any age. A known failure does not become unknown by ageing; the
    age is published beside it so a reader can see it is old.
  * no artifact, an unparseable stamp, a stamp in the future, or a record
    past the freshness window                               -> ``cannot_determine``,
    with a ``reason`` naming which of those it was.

``ok`` is therefore returned ONLY when an age was actually measured and a
check actually ran. A design in which "never checked" rendered the same
as "checked and sound" would be a false green manufactured by the check
meant to prevent one.

NOTHING HERE RUNS A PRAGMA. Every function is a read of one small JSON
file, which is the entire reason ``GET /api/v1/version`` is cheap again.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.corpus_ingest_state import (
    FRESHNESS_CANNOT_DETERMINE,
    FRESHNESS_NEVER_RAN,
    FRESHNESS_STALE,
    classify_freshness,
)
from src.core.db_integrity import (
    RUN_FAILED,
    RUN_OK,
    integrity_check_enabled,
    latest_path,
    read_verdict,
    resolve_interval_seconds,
    resolve_stale_after_seconds,
)

#: Verdicts as published on ``GET /api/v1/version``'s ``data.integrity``.
#: Three, never two. See the module docstring.
VERDICT_OK = "ok"
VERDICT_FAILED = "failed"
VERDICT_CANNOT_DETERMINE = "cannot_determine"

#: The pragma's complaint can be one row per damaged page. It is
#: truncated before it reaches an HTTP response so a badly corrupted file
#: cannot turn a status endpoint into a megabyte of text; the full text
#: is in the artifact on disk.
MAX_DETAIL_CHARS = 2000


def _classify(
    record: Optional[Dict[str, Any]], freshness: str, reason: str,
) -> Dict[str, str]:
    """Reduce a record and its freshness to one verdict plus a reason.

    Description: the reduction described in the module docstring. ``ok``
      requires BOTH a run that returned ok AND a measured age inside the
      window, so a missing or stale artifact can never read as a clean
      bill of health. A recorded failure stays ``failed`` at any age.
    Inputs: record (dict | None as returned by :func:`read_verdict`),
      freshness (str - one of the FRESHNESS_* constants), reason (str -
      the human sentence classify_freshness produced).
    Output: dict with "verdict" and "reason".
    Example: _classify(None, FRESHNESS_NEVER_RAN, "no artifact")["verdict"]
      -> 'cannot_determine'
    """
    status = str((record or {}).get("status") or "")
    if status == RUN_FAILED:
        return {
            "verdict": VERDICT_FAILED,
            "reason": f"the last integrity check FAILED; {reason}",
        }
    if freshness == FRESHNESS_NEVER_RAN:
        return {
            "verdict": VERDICT_CANNOT_DETERMINE,
            "reason": (
                "no integrity check has ever completed in this state "
                "directory, so the database has NOT been verified - this is "
                "not a claim that it is sound"
            ),
        }
    if freshness == FRESHNESS_CANNOT_DETERMINE:
        return {"verdict": VERDICT_CANNOT_DETERMINE, "reason": reason}
    if freshness == FRESHNESS_STALE:
        return {
            "verdict": VERDICT_CANNOT_DETERMINE,
            "reason": (
                f"the cached verdict is out of date: {reason}. It says "
                f"{status or 'nothing'}, but it is too old to stand for the "
                "database as it is now"
            ),
        }
    if status == RUN_OK:
        return {"verdict": VERDICT_OK, "reason": reason}
    return {
        "verdict": VERDICT_CANNOT_DETERMINE,
        "reason": (
            f"the last check finished with status {status or 'unknown'}, "
            "which is not a verdict on the database"
        ),
    }


#: The full key set published on ``data.integrity``, in the order
#: :func:`integrity_block` builds them. This is the single source of
#: truth both builders below read: neither one is allowed to hand-roll
#: its own key list, or the two paths can drift apart the way the
#: unresolvable-state-dir path once did (see :func:`unresolvable_integrity_block`).
_INTEGRITY_BLOCK_KEYS = (
    "verdict",
    "reason",
    "freshness",
    "age_seconds",
    "stale_after_seconds",
    "checked_at",
    "last_run_status",
    "duration_seconds",
    "detail",
    "enabled",
    "interval_seconds",
    "artifact",
)


def _base_integrity_fields() -> Dict[str, Any]:
    """Return every ``data.integrity`` key defaulted to "not known".

    Description: the single source of truth for the block's shape. Every
      builder in this module starts from a copy of this dict and only
      overrides the keys it can honestly answer, so ``integrity_block``
      (the normal, artifact-backed path) and
      ``unresolvable_integrity_block`` (the degraded path used when the
      state directory itself could not be resolved) are structurally
      incapable of publishing different key sets.
    Inputs: none.
    Output: dict - one entry per name in ``_INTEGRITY_BLOCK_KEYS``, each
      set to ``None``.
    Example: sorted(_base_integrity_fields()) == sorted(_INTEGRITY_BLOCK_KEYS)
      -> True
    """
    return {key: None for key in _INTEGRITY_BLOCK_KEYS}


def unresolvable_integrity_block(reason: str) -> Dict[str, Any]:
    """Build ``data.integrity`` for when the state directory itself failed.

    Description: this is the path taken before ``integrity_block`` could
      even be called - there is no state_dir to read an artifact from, so
      most fields genuinely are not known and stay ``None``. It publishes
      the SAME key set as :func:`integrity_block` (via
      :func:`_base_integrity_fields`) so a consumer reading
      ``.enabled``, ``.artifact``, ``.last_run_status``,
      ``.duration_seconds`` or ``.interval_seconds`` gets an honest
      ``None`` here instead of ``undefined``. ``verdict`` and
      ``freshness`` are still meaningful even without a state_dir, and
      both are ``cannot_determine`` - not having looked is not a fault,
      the same rule :func:`_classify` applies on the normal path.
    Inputs: reason (str - one sentence naming why the state directory
      could not be resolved).
    Output: dict - see this module's docstring for the full shape.
    Example: unresolvable_integrity_block("boom")["enabled"] -> None
    """
    fields = _base_integrity_fields()
    fields["verdict"] = VERDICT_CANNOT_DETERMINE
    fields["freshness"] = FRESHNESS_CANNOT_DETERMINE
    fields["reason"] = reason
    return fields


def integrity_block(
    state_dir: Path, *, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the ``data.integrity`` block for GET /api/v1/version.

    Description: a pure read of one small JSON file. Runs NO pragma, and
      that is the entire point of this module - the request path costs a
      file read, not a walk of every page in the database. Never raises:
      an unreadable artifact is a named ``cannot_determine``, because a
      status endpoint that 500s tells the user less than one that says
      "I could not read it".
    Inputs: state_dir (Path), now (datetime | None - injected by tests so
      staleness can be exercised without sleeping).
    Output: dict carrying ``verdict``, ``freshness``, ``age_seconds``,
      ``checked_at``, ``detail``, ``reason``, ``enabled``,
      ``interval_seconds``, ``stale_after_seconds`` and ``artifact``.
    Example: integrity_block(Path("/nonexistent"))["verdict"]
      -> 'cannot_determine'
    """
    interval = resolve_interval_seconds()
    stale_after = resolve_stale_after_seconds(interval)
    record = read_verdict(state_dir)
    freshness, age, why = classify_freshness(
        record, now=now, stale_after_seconds=stale_after,
    )
    decided = _classify(record, freshness, why)
    detail = (record or {}).get("detail")
    if isinstance(detail, str) and len(detail) > MAX_DETAIL_CHARS:
        detail = detail[:MAX_DETAIL_CHARS] + " ... (truncated)"
    fields = _base_integrity_fields()
    fields.update({
        "verdict": decided["verdict"],
        "reason": decided["reason"],
        "freshness": freshness,
        "age_seconds": None if age is None else round(age, 1),
        "stale_after_seconds": stale_after,
        "checked_at": (record or {}).get("finished_at"),
        "last_run_status": (record or {}).get("status"),
        "duration_seconds": (record or {}).get("duration_seconds"),
        "detail": detail,
        "enabled": integrity_check_enabled(),
        "interval_seconds": interval,
        "artifact": str(latest_path(state_dir)),
    })
    return fields


def cached_failure_detail(state_dir: Path) -> Optional[str]:
    """Return the recorded complaint when the cached verdict is a FAILURE.

    Description: the one hook ``src/core/db_health.py`` needs. A cached
      FAILED verdict must still degrade the datastore's reported status,
      exactly as the old per-request pragma did; every other state
      (never ran, stale, unevaluable) must NOT, or a fresh install would
      boot degraded. Returning ``None`` for those states is what keeps
      those two cases apart at the call site.
    Inputs: state_dir (Path).
    Output: str | None - the pragma's complaint when the cached verdict
      is ``failed``, otherwise None.
    Example: cached_failure_detail(Path("/nonexistent")) -> None
    """
    record = read_verdict(state_dir)
    if not record or record.get("status") != RUN_FAILED:
        return None
    detail = record.get("detail")
    text = str(detail) if detail else "PRAGMA integrity_check reported problems"
    stamp = record.get("finished_at")
    return f"{text} (recorded {stamp or 'at an unknown time'})"
