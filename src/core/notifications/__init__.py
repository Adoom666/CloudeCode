"""Notification subsystem for Cloude Code.

YAGNI scope (Item 6): started as a single push backend (ntfy.sh) wired
through a fire-and-forget asyncio dispatcher, no ABC, no plugin
registry. Slack shipped as the second backend (v0.7.0 Part 4); Pushover
is the third - exactly the "Pushover, Slack, etc." this docstring
originally anticipated. Three plain modules still don't justify the
ABC/registry refactor; revisit if a fourth shows up.

Public surface:
- ``NotificationRouter`` - synchronous ``emit()`` from any caller (PTY
  handler, IdleWatcher, lifespan hook), async worker drains a bounded
  queue and dispatches to every configured backend.
- ``NotificationEvent`` / ``EventType`` - typed payload.
- ``ntfy`` module - plain HTTP POST sender; init/shutdown/send/rotate_topic.
- ``slack`` module - incoming-webhook sender; init/shutdown/send.
- ``pushover`` module - Pushover API sender; init/shutdown/send.
- ``build_deep_link`` - helper for Click-header / url-param composition.

See ``/Users/Adam/.claude/plans/i-want-you-to-graceful-narwhal.md``
"Item 6 (notifications module) - refined" for the design contract.
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
