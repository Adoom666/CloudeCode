"""Pushover push backend - plain module (mirror of ntfy.py / slack.py).

Anticipated by the module docstring in ``__init__.py``: "when a second
backend ships (Pushover, Slack, etc.) refactor then." Slack shipped
first (v0.7.0 Part 4); this is that anticipated Pushover backend, added
as a third plain module rather than triggering the ABC refactor - three
independent fire-and-forget senders still don't justify the interface
overhead.

Pushover API contract (https://pushover.net/api):
- POST form-encoded to ``https://api.pushover.net/1/messages.json``.
- Required: ``token`` (application/API token), ``user`` (user or group
  key), ``message``.
- Optional: ``title``, ``priority`` (-2 lowest .. 2 emergency, default
  0), ``url`` + ``url_title`` (supplementary link, NOT the same as
  ntfy's Click header but serves the same deep-link purpose), ``sound``.
- Success is HTTP 200 with JSON ``{"status": 1, "request": "..."}``.
  Failure is either a non-200 status or a 200 body with
  ``"status": 0`` and an ``"errors"`` array describing what was wrong
  (bad token, invalid user key, etc.) - both cases are logged and
  swallowed, never raised.

Privacy contract, same posture as ntfy.py:
- Title/message carry NO project name and NO session_slug.
- The slug DOES appear in the ``url`` param when a public_base_url is
  configured - same accepted trade-off as ntfy's Click header.

Failure policy:
- Network errors, timeouts, non-200, and status != 1 are all caught
  and logged ONCE at WARN. Never raised - fire-and-forget by contract.
- Token or user key unset → send is a silent no-op (caller is the
  router, which already gates on has_pushover before enqueueing).
"""

from __future__ import annotations

from typing import Optional

import httpx
import structlog

from src.core.notifications.events import (
    EventType,
    NotificationEvent,
    build_deep_link,
)

logger = structlog.get_logger()

_API_URL = "https://api.pushover.net/1/messages.json"


# --- Module-level singleton state ----------------------------------------
# Mirrors ntfy.py / slack.py's pattern. A plain module beats a class for
# a single-account MVP. If we ever ship multi-account Pushover, refactor.
_client: Optional[httpx.AsyncClient] = None
_token: str = ""
_user_key: str = ""


# --- Per-event presentation table ----------------------------------------
# Generic Title/body - NEVER mention project name or session_slug, same
# rule as ntfy. Priority scale (Pushover): -2 lowest, -1 low, 0 normal,
# 1 high (bypasses quiet hours), 2 emergency (requires ack, not used here
# since nothing in this app needs a retry/ack loop).
_EVENT_PRESENTATION: dict[EventType, dict[str, object]] = {
    EventType.PERMISSION_PROMPT: {
        "title": "Cloude: permission requested",
        "message": "Tap to open session.",
        "priority": 1,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.INPUT_REQUIRED: {
        "title": "Cloude: input required",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.TASK_COMPLETE: {
        "title": "Cloude: task complete",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.ERROR: {
        "title": "Cloude: error",
        "message": "Check the terminal.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.BUILD_COMPLETE: {
        "title": "Cloude: build complete",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.TEST_RESULT: {
        "title": "Cloude: tests finished",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.CLAUDE_STOP: {
        "title": "Claude finished",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.CLAUDE_NOTIFICATION: {
        "title": "Claude is waiting",
        "message": "Tap to open session.",
        "priority": 0,
        "sound": "pushover",
        "has_link": True,
    },
    EventType.CLAUDE_PERMISSION_REQUEST: {
        "title": "Permission needed",
        "message": "Tap to open session.",
        "priority": 1,
        "sound": "pushover",
        "has_link": True,
    },
}


async def init(token: str, user_key: str) -> None:
    """Initialize the module-level httpx client and credentials.

    Idempotent: re-init closes the prior client first. Mirrors slack.py's
    pattern where an incomplete config silently disables the channel
    rather than raising.

    Args:
        token: Pushover application/API token. Treat as a secret.
        user_key: Pushover user or group key. Treat as a secret.
    """
    global _client, _token, _user_key

    if _client is not None:
        try:
            await _client.aclose()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("pushover.client_close_error_on_reinit", error=str(e))
        _client = None

    _token = (token or "").strip()
    _user_key = (user_key or "").strip()

    if not (_token and _user_key):
        # Either half missing - Pushover requires both. Disable silently,
        # same posture as slack.py's empty-webhook-url path.
        logger.info(
            "pushover.disabled_incomplete_credentials",
            token_set=bool(_token),
            user_key_set=bool(_user_key),
        )
        return

    # Connect timeout matches read timeout - same 5s budget as the other
    # two backends, tuned for a flaky LAN + TLS handshake.
    _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=5.0))
    logger.info("pushover.initialized")


async def shutdown() -> None:
    """Close the module-level client. Safe to call when uninitialized."""
    global _client, _token, _user_key

    if _client is None:
        _token = ""
        _user_key = ""
        return
    try:
        await _client.aclose()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("pushover.client_close_error", error=str(e))
    _client = None
    _token = ""
    _user_key = ""
    logger.info("pushover.shutdown")


async def send(
    event: NotificationEvent,
    public_base_url: Optional[str] = None,
) -> None:
    """Fire-and-forget POST to Pushover. Catches and logs ALL errors.

    No-ops when credentials are unset OR when ``init`` was never called
    (the router is the only legitimate caller - these checks are
    defense in depth, same as the other two backends).

    Args:
        event: the typed notification payload.
        public_base_url: when set, populates the ``url``/``url_title``
            params with a deep link back to the session. When unset,
            the notification fires without a link.
    """
    if not (_token and _user_key):
        # Credentials incomplete. Quiet by design.
        return
    if _client is None:
        # Credentials were set but client never built. Should be
        # impossible if init ran; log loudly rather than crash.
        logger.warning("pushover.send_called_before_init")
        return

    presentation = _EVENT_PRESENTATION.get(event.kind)
    if presentation is None:
        # Defensive: a new EventType slipped through without a table
        # entry. Send a minimal generic notification rather than drop.
        presentation = {
            "title": "Cloude: notification",
            "message": "Check the terminal.",
            "priority": 0,
            "sound": "pushover",
            "has_link": True,
        }

    data: dict[str, str] = {
        "token": _token,
        "user": _user_key,
        "message": str(presentation["message"]),
        "title": str(presentation["title"]),
        "priority": str(presentation["priority"]),
        "sound": str(presentation["sound"]),
    }

    if presentation.get("has_link"):
        link = build_deep_link(event, public_base_url)
        if link:
            data["url"] = link
            data["url_title"] = "Open session"

    try:
        response = await _client.post(_API_URL, data=data)
        if response.status_code >= 400:
            logger.warning(
                "pushover.send_http_error",
                status=response.status_code,
                kind=event.kind.value,
            )
            return

        # Pushover returns 200 even for some validation failures - the
        # real success signal is the JSON body's "status" field.
        try:
            body = response.json()
        except Exception:
            # Non-JSON 200 body would be unusual; log and move on rather
            # than raise on a malformed-but-technically-ok response.
            logger.warning("pushover.send_response_not_json", kind=event.kind.value)
            return

        if body.get("status") != 1:
            logger.warning(
                "pushover.send_api_error",
                kind=event.kind.value,
                errors=body.get("errors"),
            )
    except httpx.TimeoutException:
        logger.warning("pushover.send_timeout", kind=event.kind.value)
    except httpx.RequestError as e:
        # Connection errors, DNS failures, etc.
        logger.warning(
            "pushover.send_request_error",
            kind=event.kind.value,
            error=str(e),
        )
    except Exception as e:  # pragma: no cover - defensive last-resort
        logger.warning(
            "pushover.send_unexpected_error",
            kind=event.kind.value,
            error=str(e),
        )
