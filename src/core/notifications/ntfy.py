"""ntfy.sh push backend — plain module, NOT an ABC (YAGNI).

ntfy.sh API contract (https://docs.ntfy.sh/publish/):
- POST to ``{base_url}/{topic}`` with body as plain text.
- Metadata via ``X-*`` headers (or unprefixed: ``Title``, ``Priority``,
  ``Tags``, ``Click``). Headers MUST be ASCII (latin-1 encodable).
- ``Priority``: integer 1 (min) - 5 (urgent). Default 3.
- ``Tags``: comma-separated; ntfy maps known names (``warning``,
  ``rotating_light``, ``heavy_check_mark``, etc.) to emoji prefixes
  in the rendered notification.
- ``Click``: URL to open when user taps the notification.

Privacy contract (Plan v3.1 correction #2):
- Title/Body/Tags carry NO project name and NO session_slug.
- The slug DOES appear in the Click URL — accepted trade-off.

Failure policy:
- Network errors, timeouts, 4xx, 5xx all caught and logged ONCE at
  WARN. Never raised — fire-and-forget by contract.
- Topic unset → log once and refuse (caller is the router).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import httpx
import structlog

from src.core.notifications.events import (
    EventType,
    NotificationEvent,
    build_deep_link,
)

logger = structlog.get_logger()


# --- Module-level singleton state ----------------------------------------
# A plain module beats a class for a single-backend MVP. If we ever ship
# a second backend, refactor to ABC then.
_client: Optional[httpx.AsyncClient] = None
_base_url: str = ""
_topic: str = ""
_initialized: bool = False


# --- Per-event presentation table ----------------------------------------
# Generic Title — NEVER mention project name or session_slug. The Body
# is also generic; we lean on the Click URL to take the user somewhere
# that DOES have the context.
#
# Priority scale (ntfy):
#   1 = min, 2 = low, 3 = default, 4 = high, 5 = urgent.
_EVENT_PRESENTATION: dict[EventType, dict[str, object]] = {
    EventType.PERMISSION_PROMPT: {
        "title": "Cloude: permission requested",
        "body": "Tap to open session.",
        "priority": 5,
        "tags": "warning",
        "has_link": True,
    },
    EventType.INPUT_REQUIRED: {
        "title": "Cloude: input required",
        "body": "Tap to open session.",
        "priority": 4,
        "tags": "speech_balloon",
        "has_link": True,
    },
    EventType.TASK_COMPLETE: {
        "title": "Cloude: task complete",
        "body": "Tap to open session.",
        "priority": 3,
        "tags": "heavy_check_mark",
        "has_link": True,
    },
    EventType.ERROR: {
        "title": "Cloude: error",
        "body": "Check the terminal.",
        "priority": 3,
        "tags": "rotating_light",
        "has_link": True,
    },
    EventType.BUILD_COMPLETE: {
        "title": "Cloude: build complete",
        "body": "Tap to open session.",
        "priority": 3,
        "tags": "package",
        "has_link": True,
    },
    EventType.TEST_RESULT: {
        "title": "Cloude: tests finished",
        "body": "Tap to open session.",
        "priority": 3,
        "tags": "test_tube",
        "has_link": True,
    },
    EventType.TUNNEL_CREATED: {
        "title": "Cloude: tunnel ready",
        "body": "Public URL is up.",
        "priority": 3,
        "tags": "globe_with_meridians",
        "has_link": False,
    },
}


async def init(base_url: str, topic: str) -> None:
    """Initialize the module-level httpx client and target.

    Idempotent: re-init closes the prior client first.

    Args:
        base_url: e.g. ``"https://ntfy.sh"`` or self-hosted URL.
        topic: ntfy topic name (treat as a secret — anyone with this
            string can read your notifications).
    """
    global _client, _base_url, _topic, _initialized

    if _client is not None:
        # Re-init path — close old client to free the connection pool.
        try:
            await _client.aclose()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("ntfy.client_close_error_on_reinit", error=str(e))

    _base_url = base_url.rstrip("/")
    _topic = topic
    # Connect timeout slightly longer than read timeout — TLS handshake
    # to ntfy.sh over a flaky LAN can dawdle.
    _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=5.0))
    _initialized = True
    logger.info(
        "ntfy.initialized",
        base_url=_base_url,
        topic_set=bool(_topic),
    )


async def shutdown() -> None:
    """Close the module-level client. Safe to call when uninitialized."""
    global _client, _initialized

    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ntfy.client_close_error", error=str(e))
    _client = None
    _initialized = False
    logger.info("ntfy.shutdown")


async def rotate_topic(new_topic: str) -> None:
    """Swap the active topic at runtime.

    Used by ``setup_auth.py --rotate-topic`` after the user re-subscribes
    in the ntfy mobile app. The httpx client and base URL stay put.
    """
    global _topic
    old_set = bool(_topic)
    _topic = new_topic
    logger.info(
        "ntfy.topic_rotated",
        had_previous=old_set,
        new_topic_set=bool(new_topic),
    )


async def send(
    event: NotificationEvent,
    public_base_url: Optional[str] = None,
) -> None:
    """Fire-and-forget POST to ntfy. Catches and logs ALL errors.

    Composes a generic Title/Body that does NOT contain the
    session_slug or any project-identifying data (privacy contract,
    Plan v3.1 correction #2). The slug only appears in the Click URL.

    Args:
        event: the typed notification payload.
        public_base_url: when set, drives the Click deep link. When
            unset, the notification fires without a Click header.
    """
    if _client is None or not _initialized:
        # init() was never called — should be impossible if the router
        # is in charge, but log loudly rather than crash.
        logger.warning("ntfy.send_called_before_init")
        return

    if not _topic:
        # Topic empty (config not yet generated by setup_auth.py). The
        # router should have refused at emit-time; if we got here log
        # once and bail.
        logger.warning("ntfy.send_skipped_no_topic", kind=event.kind.value)
        return

    presentation = _EVENT_PRESENTATION.get(event.kind)
    if presentation is None:
        # Defensive: a new EventType slipped through without a table
        # entry. Send a minimal generic notification rather than drop.
        presentation = {
            "title": "Cloude: notification",
            "body": "Check the terminal.",
            "priority": 3,
            "tags": "bell",
            "has_link": True,
        }

    # Headers MUST be latin-1 / ASCII for httpx. All values in the
    # table above are ASCII; we do not interpolate event fields here.
    headers: dict[str, str] = {
        "Title": str(presentation["title"]),
        "Priority": str(presentation["priority"]),
        "Tags": str(presentation["tags"]),
    }

    if presentation.get("has_link"):
        link = build_deep_link(event, public_base_url)
        if link:
            headers["Click"] = link

    body = str(presentation["body"])

    # URL-encode the topic in case someone configures one with reserved
    # chars (path-traversal defense).
    url = f"{_base_url}/{quote(_topic, safe='')}"

    try:
        response = await _client.post(url, content=body, headers=headers)
        # 2xx = OK. 4xx/5xx = log and move on. ntfy returns 200 on
        # accepted publish; some self-hosted setups may return 202.
        if response.status_code >= 400:
            logger.warning(
                "ntfy.send_http_error",
                status=response.status_code,
                kind=event.kind.value,
            )
    except httpx.TimeoutException:
        logger.warning("ntfy.send_timeout", kind=event.kind.value)
    except httpx.RequestError as e:
        # Connection errors, DNS failures, etc.
        logger.warning(
            "ntfy.send_request_error",
            kind=event.kind.value,
            error=str(e),
        )
    except Exception as e:  # pragma: no cover - defensive last-resort
        logger.warning(
            "ntfy.send_unexpected_error",
            kind=event.kind.value,
            error=str(e),
        )
