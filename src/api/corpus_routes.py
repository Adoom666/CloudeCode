"""``/api/v1/corpus/*`` - the transcript archive's status and manual trigger.

ITS OWN MODULE, like ``status_routes.py``, for the same reason: nothing
new lands in ``src/api/routes.py``.

AUTH IS NOT OPTIONAL. The status object names the corpus root, the state
directory, project-derived rooting counts and the paths of files the
last run could not read. That is a filesystem map of the owner's work,
so both routes carry ``Depends(require_auth)`` exactly like every other
``/api/v1`` route.

TWO ROUTES, AND THE SECOND ONE IS NOT A SECOND INGESTER. ``GET
/corpus/status`` is a pure read and never triggers work. ``POST
/corpus/ingest`` runs ONE pass synchronously, on the same code path the
background scheduler uses (``run_ingest_once``), so a manual run and a
scheduled run can never diverge in behaviour. It exists because "run it
now and tell me what happened" is the first thing anybody wants when a
status reads stale, and the alternative is a second implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends, Query, Request

from src.api.auth import require_auth
from src.config import settings
from src.core import corpus_status
from src.core.corpus_ingest_service import run_ingest_once

logger = structlog.get_logger()

router = APIRouter(tags=["corpus-archive"])

#: Upper bound on ``POST /corpus/ingest?byte_verify_sample=``. A
#: reconstruction is real work; an unbounded parameter would let one
#: request pin the process for minutes.
MAX_BYTE_VERIFY_SAMPLE = 500


def _scheduler(request: Request) -> Optional[Any]:
    """Return the process's ingest scheduler, or None when unmounted.

    Description: None is a REPORTED state, not an error - see
      :func:`src.core.corpus_status.build_status`, which renders it as
      "nothing is keeping the archive current here" rather than as a
      disabled feature.
    Inputs: request (Request).
    Output: CorpusIngestScheduler | None.
    Example: _scheduler(request) is None  # before lifespan mounts it
    """
    return getattr(request.app.state, "corpus_ingest_scheduler", None)


@router.get(
    "/corpus/status",
    response_model=None,
    dependencies=[Depends(require_auth)],
)
async def get_corpus_status(request: Request) -> Dict[str, Any]:
    """Report what the app can honestly say about its transcript archive.

    Args:
        request: the incoming request, for ``app.state``.

    Returns:
        The object built by :func:`src.core.corpus_status.build_status`.
        Every block carries its own status, and freshness is one of
        ``current``, ``stale``, ``never_ran`` or ``cannot_determine`` -
        never a bare boolean.
    """
    snapshot = corpus_status.build_status(
        Path(settings.get_state_dir()), scheduler=_scheduler(request),
    )
    logger.info(
        "corpus_status_collected",
        verdict=snapshot["overall"]["verdict"],
        freshness=snapshot["freshness"]["verdict"],
    )
    return snapshot


@router.post(
    "/corpus/ingest",
    response_model=None,
    dependencies=[Depends(require_auth)],
)
async def post_corpus_ingest(
    request: Request,
    byte_verify_sample: int = Query(
        0, ge=0, le=MAX_BYTE_VERIFY_SAMPLE,
        description=(
            "How many of the newest archives to reconstruct and check "
            "against their stored hash. 0 means do not verify, which the "
            "report states as 'not_run' rather than as a pass."
        ),
    ),
) -> Dict[str, Any]:
    """Run one incremental ingest pass now and return its report.

    Description: uses the same ``run_ingest_once`` the scheduler uses,
      so there is one behaviour, not two. Runs in a worker thread so the
      event loop keeps serving terminals while it works. Never raises:
      every failure is a named status on the returned report, and the
      liveness artifact is published either way.

    Args:
        request: the incoming request, for ``app.state``.
        byte_verify_sample: newest-archive sample to byte-verify.

    Returns:
        ``{"report": <run record>, "status": <run status>}``.
    """
    import asyncio

    report = await asyncio.to_thread(
        run_ingest_once,
        Path(settings.get_state_dir()),
        byte_verify_sample=byte_verify_sample,
    )
    scheduler = _scheduler(request)
    if scheduler is not None:
        scheduler.last_report = report
    logger.info(
        "corpus_ingest_manual_run", status=report.status,
        ingested=report.ingested, discovered=report.discovered,
    )
    return {"status": report.status, "report": report.to_record()}
