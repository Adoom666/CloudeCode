"""Shared plumbing for the ``/api/v1/archive/*`` route modules.

THREE ROUTE MODULES, ONE STATUS RULE. The archive routes are split across
``archive_routes.py``, ``archive_search_routes.py`` and
``archive_export_routes.py`` only because the repo caps a file at 500
lines. Splitting them created exactly one real risk - that "a malformed
cursor is a 400" ends up true on one module and false on the next - so
the mapping from an envelope to an HTTP status lives here, once, and
every module calls it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

import structlog
from fastapi.responses import JSONResponse

from src.config import settings
from src.core.archive_read import http_status_for

logger = structlog.get_logger()

#: ``unevaluated`` subjects that name something the CLIENT sent. A
#: cannot_determine on one of these is a client error and gets a 400, so
#: the caller's tooling notices; anything else (a datastore that would
#: not open, for instance) is a 200 carrying an honest refusal, because
#: the server did answer - it answered "I could not evaluate this".
#: Subjects beginning ``filter:`` are included by prefix, which is how
#: ``src.core.archive_lines`` names an unknown role/record_type/model.
CLIENT_PARAM_SUBJECTS: frozenset = frozenset({
    "cursor", "q", "limit", "scope", "scan_budget", "scan_bytes",
    "project_id", "transcript_id", "max_page_bytes", "start_line",
})
CLIENT_PARAM_PREFIX: str = "filter:"


def state_dir() -> Path:
    """Resolve the app's state directory for this request.

    Description: read through ``settings.get_state_dir()`` on every call
      rather than captured at import, so a test that repoints the state
      directory is actually followed.
    Inputs: none. Output: Path.
    Example: state_dir() / "cloude.db"
    """
    return Path(settings.get_state_dir())


def is_client_error(envelope_dict: Mapping[str, Any]) -> bool:
    """Decide whether a cannot_determine blames the caller's parameters.

    Description: one named rule, applied identically on every route, so
      "malformed cursor is a 400" cannot be true on one endpoint and
      false on the next. It reads the envelope's own ``unevaluated``
      subjects - the same field the client is shown - rather than a
      second, invisible classification.
    Inputs: envelope_dict (Mapping) - a three-outcome envelope.
    Output: bool - True when every named subject is a client parameter.
    Example: is_client_error({"unevaluated": [{"subject": "cursor",
             "reason": "..."}]}) -> True
    """
    entries = envelope_dict.get("unevaluated") or []
    if not entries:
        return False
    return all(
        str(entry.get("subject", "")) in CLIENT_PARAM_SUBJECTS
        or str(entry.get("subject", "")).startswith(CLIENT_PARAM_PREFIX)
        for entry in entries
    )


def respond(envelope_dict: Dict[str, Any], **log_fields: Any) -> JSONResponse:
    """Return an envelope at the HTTP status its own result_status implies.

    Description: the ONE place a status code is chosen, so no route can
      answer 200 for a not_found or swallow a bad cursor into a page-1
      reset. 404 for not_found; 400 for a cannot_determine that names a
      client parameter; 200 otherwise - including a cannot_determine the
      SERVER is responsible for, because the server did respond and its
      answer is "I could not evaluate this".
    Inputs: envelope_dict (dict) - built by ``archive_read.envelope``.
      log_fields (Any) - structured log context. Never a body, a snippet
      or a query's matched text.
    Output: JSONResponse.
    Example: respond(env, route="hosts").status_code -> 200
    """
    result_status = str(envelope_dict["result_status"])
    status = http_status_for(
        result_status, cursor_error=is_client_error(envelope_dict)
    )
    logger.info(
        "archive_read",
        result_status=result_status,
        scope_status=envelope_dict.get("scope_status"),
        http_status=status,
        unevaluated=len(envelope_dict.get("unevaluated") or []),
        **log_fields,
    )
    return JSONResponse(status_code=status, content=envelope_dict)
