"""Notification subsystem for Cloude Code.

YAGNI scope (Item 6): a single push backend (ntfy.sh) wired through a
fire-and-forget asyncio dispatcher. No ABC, no plugin registry — when a
second backend ships (Pushover, Slack, etc.) refactor then.

Public surface:
- ``NotificationRouter`` — synchronous ``emit()`` from any caller (PTY
  handler, IdleWatcher, lifespan hook), async worker drains a bounded
  queue and dispatches via the active backend.
- ``NotificationEvent`` / ``EventType`` — typed payload.
- ``ntfy`` module — plain HTTP POST sender; init/shutdown/send/rotate_topic.
- ``build_deep_link`` — helper for Click-header URL composition.

See ``/Users/Adam/.claude/plans/i-want-you-to-graceful-narwhal.md``
"Item 6 (notifications module) — refined" for the design contract.
"""

from src.core.notifications.events import (
    EventType,
    NotificationEvent,
    build_deep_link,
)
from src.core.notifications.idle_watcher import IdleState, IdleWatcher
from src.core.notifications.router import NotificationRouter

__all__ = [
    "EventType",
    "IdleState",
    "IdleWatcher",
    "NotificationEvent",
    "NotificationRouter",
    "build_deep_link",
]
