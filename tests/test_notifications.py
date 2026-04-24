"""Tests for src.core.notifications — events + ntfy + router (Item 6).

Run with:
    python3 -m pytest tests/test_notifications.py -v

All tests are offline. The httpx layer is mocked; no real network is
required to exercise ntfy.send / NotificationRouter dispatch.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import List
from unittest import mock

import pytest


# ---- env bootstrap (same pattern as the other test modules) ---------------
# pydantic Settings sys.exit(1)s without these — inject before imports.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_notif_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_notif_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.notifications import ntfy
from src.core.notifications.events import (
    EventType,
    NotificationEvent,
    build_deep_link,
)
from src.core.notifications.router import NotificationRouter, _QUEUE_MAXSIZE


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeNotificationsConfig:
    """Stand-in for AuthConfig.notifications — only the fields the
    router reads matter.

    Rate-limit fields (Item 8) are set PERMISSIVELY here so existing
    router tests see uncapped dispatch. Item 8's dedicated test file
    (``tests/test_rate_limiter.py``) exercises the limiter with the
    real defaults.
    """
    enabled: bool = True
    ntfy_topic: str = "test-topic"
    ntfy_base_url: str = "https://ntfy.sh"
    public_base_url: str = "http://lan.local:8000"
    # Permissive: huge bucket + zero cooldown → all emits pass.
    rate_limit_global_cap: int = 10_000
    rate_limit_window_seconds: float = 60.0
    rate_limit_per_kind_cooldown_seconds: float = 0.0


def _make_event(
    kind: EventType = EventType.TASK_COMPLETE,
    slug: str = "secret-project-slug",
) -> NotificationEvent:
    return NotificationEvent(
        kind=kind,
        session_slug=slug,
        timestamp=0.0,
        snippet="internal log only",
    )


@pytest.fixture(autouse=True)
def _reset_ntfy_module_state():
    """Each test starts with the module in a clean state."""
    # Snapshot + restore so other tests in the suite are not polluted.
    saved = (ntfy._client, ntfy._base_url, ntfy._topic, ntfy._initialized)
    ntfy._client = None
    ntfy._base_url = ""
    ntfy._topic = ""
    ntfy._initialized = False
    yield
    ntfy._client = saved[0]
    ntfy._base_url = saved[1]
    ntfy._topic = saved[2]
    ntfy._initialized = saved[3]


# ---------------------------------------------------------------------------
# events.build_deep_link
# ---------------------------------------------------------------------------


def test_build_deep_link_basic():
    event = _make_event(slug="my-project")
    url = build_deep_link(event, "http://mac.lan:8000")
    assert url == "http://mac.lan:8000/session/my-project"


def test_build_deep_link_strips_trailing_slash():
    event = _make_event(slug="my-project")
    url = build_deep_link(event, "http://mac.lan:8000/")
    assert url == "http://mac.lan:8000/session/my-project"


def test_build_deep_link_url_encodes_slug():
    # Item 9: build_deep_link now re-runs `_slugify` defensively before
    # URL-encoding. That means a hostile raw slug like
    # `weird slug/with?chars#frag` is first reduced to an ASCII-safe
    # underscore-separated form, then URL-encoded. The original injection
    # claim still holds — no raw `?` or `#` can reach the path segment —
    # but the exact percent-encoded form no longer shows up because the
    # dangerous chars are collapsed to `_` before encoding.
    event = _make_event(slug="weird slug/with?chars#frag")
    url = build_deep_link(event, "http://mac.lan:8000")
    # Defense in depth: the entire deep link must be path-safe.
    path_part = url.split("/session/", 1)[1]
    assert "?" not in path_part
    assert "#" not in path_part
    assert "/" not in path_part
    assert " " not in path_part
    # After slugify, the four non-alphanumerics (space, `/`, `?`, `#`)
    # each collapse to `_`, yielding the string below. Update this if
    # the slug rules in `src/core/tmux_backend._slugify` ever change.
    assert url == "http://mac.lan:8000/session/weird_slug_with_chars_frag"


def test_build_deep_link_returns_none_when_base_unset():
    event = _make_event()
    assert build_deep_link(event, "") is None
    assert build_deep_link(event, None) is None


# ---------------------------------------------------------------------------
# ntfy.send — mocked httpx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ntfy_send_posts_correct_url_and_headers():
    """Title (generic), Priority, Click headers all set; URL is base/topic."""
    await ntfy.init("https://ntfy.sh", "test-topic-abc")
    event = _make_event(EventType.PERMISSION_PROMPT, slug="secret-slug")

    captured = {}

    async def fake_post(url, content=None, headers=None):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(event, public_base_url="http://lan.local:8000")

    # URL: base + topic
    assert captured["url"] == "https://ntfy.sh/test-topic-abc"
    # Headers: generic title (NO project name, NO slug), priority 5 for
    # permission prompts, tags = warning, Click contains the slug.
    headers = captured["headers"]
    assert headers["Title"] == "Cloude: permission requested"
    assert headers["Priority"] == "5"
    assert headers["Tags"] == "warning"
    assert headers["Click"] == "http://lan.local:8000/session/secret-slug"
    # Body: short, generic.
    assert captured["content"] == "Tap to open session."


@pytest.mark.asyncio
async def test_ntfy_send_title_body_tags_never_contain_slug():
    """Privacy contract: NO ntfy-bound field carries session_slug."""
    await ntfy.init("https://ntfy.sh", "topic")
    test_slug = "very-identifying-project-name-DO-NOT-LEAK"
    event = _make_event(EventType.TASK_COMPLETE, slug=test_slug)

    captured = {}

    async def fake_post(url, content=None, headers=None):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(event, public_base_url="http://lan:8000")

    # Title, body, tags — explicit privacy assertion.
    assert test_slug not in captured["headers"]["Title"]
    assert test_slug not in captured["headers"]["Tags"]
    assert test_slug not in (captured["content"] or "")
    # Slug IS allowed in the Click URL (accepted trade-off).
    assert test_slug in captured["headers"]["Click"]


@pytest.mark.asyncio
async def test_ntfy_send_swallows_500():
    """5xx responses are logged but never raised."""
    await ntfy.init("https://ntfy.sh", "topic")

    async def fake_post(url, content=None, headers=None):
        resp = mock.MagicMock()
        resp.status_code = 500
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        # Must not raise.
        await ntfy.send(_make_event(), public_base_url="http://lan:8000")


@pytest.mark.asyncio
async def test_ntfy_send_swallows_timeout():
    """httpx.TimeoutException is caught and logged."""
    import httpx
    await ntfy.init("https://ntfy.sh", "topic")

    async def fake_post(url, content=None, headers=None):
        raise httpx.TimeoutException("simulated timeout")

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        # Must not raise.
        await ntfy.send(_make_event(), public_base_url="http://lan:8000")


@pytest.mark.asyncio
async def test_ntfy_send_swallows_connection_error():
    """httpx.ConnectError (a RequestError) is caught and logged."""
    import httpx
    await ntfy.init("https://ntfy.sh", "topic")

    async def fake_post(url, content=None, headers=None):
        raise httpx.ConnectError("simulated connection refused")

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(_make_event(), public_base_url="http://lan:8000")


@pytest.mark.asyncio
async def test_ntfy_send_skipped_when_topic_empty():
    """Topic empty → log + return; do NOT touch httpx."""
    await ntfy.init("https://ntfy.sh", "")  # empty topic

    called = {"hit": False}

    async def fake_post(*a, **kw):
        called["hit"] = True
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(_make_event(), public_base_url="http://lan:8000")

    assert called["hit"] is False


@pytest.mark.asyncio
async def test_ntfy_send_omits_click_when_no_public_url():
    """No public_base_url → no Click header."""
    await ntfy.init("https://ntfy.sh", "topic")
    captured = {}

    async def fake_post(url, content=None, headers=None):
        captured["headers"] = headers
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(_make_event(), public_base_url="")

    assert "Click" not in captured["headers"]


# ---------------------------------------------------------------------------
# ntfy.rotate_topic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_topic_swaps_target():
    """rotate_topic updates module state; the next send goes to the new topic."""
    await ntfy.init("https://ntfy.sh", "old-topic")
    await ntfy.rotate_topic("new-topic")

    captured = {}

    async def fake_post(url, content=None, headers=None):
        captured["url"] = url
        resp = mock.MagicMock()
        resp.status_code = 200
        return resp

    with mock.patch.object(ntfy._client, "post", side_effect=fake_post):
        await ntfy.send(_make_event(), public_base_url="http://lan:8000")

    assert captured["url"] == "https://ntfy.sh/new-topic"


# ---------------------------------------------------------------------------
# NotificationRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_emit_drops_oldest_on_overflow():
    """Full queue → oldest is evicted, new event is enqueued."""
    cfg = _FakeNotificationsConfig()
    loop = asyncio.get_running_loop()
    router = NotificationRouter(cfg, loop)
    # Don't start the worker — we want to inspect the queue state directly.

    # Fill the queue to capacity with a sentinel kind so we can tell
    # the dropped one apart from the new one.
    for _ in range(_QUEUE_MAXSIZE):
        router._queue.put_nowait(_make_event(kind=EventType.BUILD_COMPLETE))

    assert router._queue.full() is True

    # Emit a NEW event of a different kind. Drop-oldest then re-put.
    new_event = _make_event(kind=EventType.PERMISSION_PROMPT, slug="new")
    router.emit(new_event)

    # Queue still at capacity, but now contains the new event somewhere.
    assert router._queue.qsize() == _QUEUE_MAXSIZE
    # Drain and verify the new event is present and a BUILD_COMPLETE was
    # evicted (so we have N-1 BUILD_COMPLETE + 1 PERMISSION_PROMPT).
    drained: List[NotificationEvent] = []
    while not router._queue.empty():
        drained.append(router._queue.get_nowait())
        router._queue.task_done()

    permission_count = sum(
        1 for e in drained if e.kind == EventType.PERMISSION_PROMPT
    )
    build_count = sum(
        1 for e in drained if e.kind == EventType.BUILD_COMPLETE
    )
    assert permission_count == 1
    assert build_count == _QUEUE_MAXSIZE - 1


@pytest.mark.asyncio
async def test_router_worker_drains_queue():
    """Emit 5 events → worker calls ntfy.send 5 times."""
    cfg = _FakeNotificationsConfig()
    loop = asyncio.get_running_loop()
    router = NotificationRouter(cfg, loop)

    call_count = {"n": 0}

    async def fake_send(event, public_base_url=None):
        call_count["n"] += 1

    with mock.patch.object(ntfy, "send", side_effect=fake_send):
        await router.start()
        for i in range(5):
            router.emit(_make_event(slug=f"sess-{i}"))

        # Wait for queue drain. join() blocks until task_done has been
        # called for every put.
        await asyncio.wait_for(router._queue.join(), timeout=2.0)
        await router.stop()

    assert call_count["n"] == 5


@pytest.mark.asyncio
async def test_router_emit_noop_when_disabled():
    """enabled=False → emit drops silently, no queue activity."""
    cfg = _FakeNotificationsConfig(enabled=False)
    loop = asyncio.get_running_loop()
    router = NotificationRouter(cfg, loop)

    router.emit(_make_event())
    assert router._queue.empty()


@pytest.mark.asyncio
async def test_router_emit_noop_when_topic_missing():
    """Empty topic → router refuses to enqueue (won't reach ntfy.send)."""
    cfg = _FakeNotificationsConfig(ntfy_topic="")
    loop = asyncio.get_running_loop()
    router = NotificationRouter(cfg, loop)

    router.emit(_make_event())
    assert router._queue.empty()


@pytest.mark.asyncio
async def test_router_start_stop_clean():
    """start() spawns worker; stop() cancels and clears it."""
    cfg = _FakeNotificationsConfig()
    loop = asyncio.get_running_loop()
    router = NotificationRouter(cfg, loop)

    await router.start()
    assert router._worker_task is not None
    assert not router._worker_task.done()

    await router.stop()
    assert router._worker_task is None
