"""Slack incoming-webhook push backend — plain module (mirror of ntfy.py).

Slack incoming webhook contract
(https://api.slack.com/messaging/webhooks):
- POST JSON to the webhook URL. Body is a Slack message payload —
  ``text`` (fallback) plus optional ``blocks``. No auth header — the
  URL itself is the credential.
- Single channel: the webhook is bound to one channel at creation
  time on the Slack side. We do not pick channels here.

Privacy / surface contract:
- We do NOT carry deep links the way ntfy does (Click header). The
  Slack message text includes the event's snippet truncated to 200
  chars; that's it. No project name composed by us, but the snippet
  itself MAY contain free-form caller-supplied text — callers are
  responsible for what they pass in.

Failure policy:
- Fire-and-forget. Network errors, timeouts, 4xx, 5xx are all caught
  and logged ONCE at WARN. Never raised — the toast pipeline must
  never die because Slack is down.
- Empty webhook URL → channel is silently disabled (logged once at
  init). Subsequent ``send`` calls are no-ops.
"""

from __future__ import annotations

from typing import Optional

import httpx
import structlog

from src.core.notifications.events import EventType, NotificationEvent

logger = structlog.get_logger()


# --- Module-level singleton state ----------------------------------------
# Mirrors ntfy.py's pattern. A plain module beats a class for a single
# webhook MVP. If we ever ship per-session or per-project webhooks, refactor.
_client: Optional[httpx.AsyncClient] = None
_webhook_url: str = ""


# --- Per-event presentation table ----------------------------------------
# Generic emoji + title per EventType. We keep this lean — Slack messages
# carry the event's snippet directly, so we don't need ntfy-style copy.
_PRESENTATIONS: dict[EventType, dict[str, str]] = {
    EventType.CLAUDE_STOP: {"emoji": ":white_check_mark:", "title": "Claude finished"},
    EventType.CLAUDE_NOTIFICATION: {"emoji": ":hourglass_flowing_sand:", "title": "Claude is waiting"},
    EventType.CLAUDE_PERMISSION_REQUEST: {"emoji": ":lock:", "title": "Permission needed"},
    EventType.PERMISSION_PROMPT: {"emoji": ":lock:", "title": "Permission needed"},
    EventType.TASK_COMPLETE: {"emoji": ":white_check_mark:", "title": "Task complete"},
    EventType.INPUT_REQUIRED: {"emoji": ":hourglass_flowing_sand:", "title": "Input required"},
    EventType.ERROR: {"emoji": ":x:", "title": "Error"},
    EventType.BUILD_COMPLETE: {"emoji": ":hammer:", "title": "Build complete"},
    EventType.TEST_RESULT: {"emoji": ":test_tube:", "title": "Test result"},
}


def _presentation_for_kind(kind: EventType) -> dict[str, str]:
    """Lookup with a safe :mega: fallback for unmapped kinds."""
    return _PRESENTATIONS.get(kind, {"emoji": ":mega:", "title": kind.value})


def _build_payload(event: NotificationEvent) -> dict:
    """Compose the Slack JSON payload.

    Returns a dict with both ``text`` (fallback for clients without
    block rendering) and ``blocks`` (rich rendering on web/desktop/mobile).
    """
    presentation = _presentation_for_kind(event.kind)
    # Snippet bounded to 200 chars defensively — Slack accepts more but
    # there's no value flooding a chat channel with a giant blob.
    snippet = (event.snippet or "")[:200]
    text = f"{presentation['emoji']} *{presentation['title']}* — {snippet}".rstrip(" —")
    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ],
    }


async def init(webhook_url: str) -> None:
    """Initialize the Slack channel with the incoming webhook URL.

    Idempotent: re-init closes the prior client first.

    Args:
        webhook_url: ``https://hooks.slack.com/services/...`` — empty
            string (or whitespace-only) disables this channel silently
            (logged once at info level).
    """
    global _client, _webhook_url

    if _client is not None:
        try:
            await _client.aclose()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("slack.client_close_error_on_reinit", error=str(e))
        _client = None

    _webhook_url = (webhook_url or "").strip()
    if not _webhook_url:
        logger.info("slack.disabled_no_webhook")
        return

    # Connect timeout matches read timeout — Slack's edge is fast; 5s
    # covers TLS + a typical RTT on flaky LAN.
    _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=5.0))
    logger.info("slack.initialized")


async def shutdown() -> None:
    """Close the module-level client. Safe to call when uninitialized."""
    global _client, _webhook_url

    if _client is None:
        _webhook_url = ""
        return
    try:
        await _client.aclose()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("slack.client_close_error", error=str(e))
    _client = None
    _webhook_url = ""
    logger.info("slack.shutdown")


async def send(event: NotificationEvent) -> None:
    """Fire-and-forget POST to Slack. Catches and logs ALL errors.

    No-ops when the webhook URL is unset OR when ``init`` was never
    called (the router is the only legitimate caller — these checks
    are defense in depth).

    Args:
        event: the typed notification payload. ``event.snippet`` is
            placed verbatim into the Slack message text (truncated
            to 200 chars).
    """
    if not _webhook_url:
        # Channel disabled by config. Quiet by design.
        return
    if _client is None:
        # Webhook URL was set but client never built. Should be
        # impossible if init ran; log loudly rather than crash.
        logger.warning("slack.send_called_before_init")
        return

    try:
        payload = _build_payload(event)
        response = await _client.post(_webhook_url, json=payload)
        # Slack returns 200 on success. Non-2xx is a delivery problem —
        # log and move on.
        if response.status_code >= 400:
            logger.warning(
                "slack.send_http_error",
                status=response.status_code,
                kind=event.kind.value,
            )
    except httpx.TimeoutException:
        logger.warning("slack.send_timeout", kind=event.kind.value)
    except httpx.RequestError as e:
        logger.warning(
            "slack.send_request_error",
            kind=event.kind.value,
            error=str(e),
        )
    except Exception as e:  # pragma: no cover - defensive last-resort
        logger.warning(
            "slack.send_error",
            kind=event.kind.value,
            error=str(e),
        )
