"""Tests for src.core.notifications.slack — v0.7.0 Part 4.

Covers:
- init / disabled / send no-op paths
- payload shape (text + blocks)
- exception swallowing (timeout, request error, generic)
- router fanout (ntfy + slack both fire, ntfy first)
- session_manager.record_toast emits NotificationEvent for known kinds
- session_manager.record_toast skips emit for unknown kinds
- presentation fallback for unmapped EventType

Run with:
    python3 -m pytest tests/test_slack_send.py -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from unittest import mock

import pytest


# ---- env bootstrap (same pattern as the other test modules) ---------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_slack_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_slack_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.notifications import ntfy, slack
from src.core.notifications.events import EventType, NotificationEvent
from src.core.notifications.router import NotificationRouter


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeNotificationsConfig:
    """Stand-in for AuthConfig.notifications — only the fields the
    router reads matter. Permissive rate-limit defaults so emits flow."""
    enabled: bool = True
    ntfy_topic: str = "test-topic"
    ntfy_base_url: str = "https://ntfy.sh"
    public_base_url: str = "http://lan.local:8000"
    slack_webhook_url: str = "https://hooks.slack.com/services/T/B/test"
    rate_limit_global_cap: int = 10_000
    rate_limit_window_seconds: float = 60.0
    rate_limit_per_kind_cooldown_seconds: float = 0.0


def _make_event(
    kind: EventType = EventType.TASK_COMPLETE,
    slug: str = "test-slug",
    snippet: str = "internal snippet",
) -> NotificationEvent:
    return NotificationEvent(
        kind=kind,
        session_slug=slug,
        timestamp=0.0,
        snippet=snippet,
    )


@pytest.fixture(autouse=True)
def _reset_slack_module_state():
    """Each test starts and ends with slack module state clean."""
    saved = (slack._client, slack._webhook_url)
    slack._client = None
    slack._webhook_url = ""
    yield
    slack._client = saved[0]
    slack._webhook_url = saved[1]


@pytest.fixture(autouse=True)
def _reset_ntfy_module_state():
    """Mirror the ntfy reset fixture from test_notifications so the
    router-fanout test can stand up ntfy alongside slack without
    state bleeding across the suite."""
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
# slack.init — disabled / enabled paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_init_empty_url_disables_channel():
    """init("") sets _webhook_url empty AND does not build a client."""
    await slack.init("")
    assert slack._webhook_url == ""
    assert slack._client is None


@pytest.mark.asyncio
async def test_slack_init_whitespace_url_disables_channel():
    """Whitespace-only URL is treated as empty (defensive trim)."""
    await slack.init("   ")
    assert slack._webhook_url == ""
    assert slack._client is None


@pytest.mark.asyncio
async def test_slack_init_real_url_builds_client():
    """A real-looking URL builds the httpx client."""
    await slack.init("https://hooks.slack.com/services/A/B/C")
    try:
        assert slack._webhook_url == "https://hooks.slack.com/services/A/B/C"
        assert slack._client is not None
    finally:
        await slack.shutdown()


# ---------------------------------------------------------------------------
# slack.send — no-op paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_send_no_init_is_noop():
    """Never inited → send returns without raising or doing work."""
    # Module state is reset by the fixture; do not call init.
    await slack.send(_make_event())  # must not raise


@pytest.mark.asyncio
async def test_slack_send_disabled_returns_silently():
    """init('') → send is a no-op; no httpx call attempted."""
    await slack.init("")  # disabled

    # If somehow send did try to POST, this would explode (no client).
    # Just assert: returns cleanly + state untouched.
    await slack.send(_make_event())
    assert slack._client is None


# ---------------------------------------------------------------------------
# slack.send — payload shape + post wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_send_posts_correct_payload_shape():
    """init(url), send(event) → httpx client got POSTed with text+blocks."""
    await slack.init("https://hooks.slack.com/test")
    try:
        captured = {}

        async def fake_post(url, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            resp = mock.MagicMock()
            resp.status_code = 200
            return resp

        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            await slack.send(_make_event(EventType.CLAUDE_STOP, snippet="all done"))

        assert captured["url"] == "https://hooks.slack.com/test"
        payload = captured["json"]
        assert "text" in payload
        assert "blocks" in payload
        # Block shape: a section block with mrkdwn text.
        assert payload["blocks"][0]["type"] == "section"
        assert payload["blocks"][0]["text"]["type"] == "mrkdwn"
        # Snippet must show up in the rendered text.
        assert "all done" in payload["text"]
        # Per-kind title visible.
        assert "Claude finished" in payload["text"]
    finally:
        await slack.shutdown()


@pytest.mark.asyncio
async def test_slack_send_truncates_snippet_to_200():
    """Long snippets are bounded — Slack channels don't need 10KB blobs."""
    await slack.init("https://hooks.slack.com/test")
    try:
        captured = {}

        async def fake_post(url, json=None, **kwargs):
            captured["json"] = json
            resp = mock.MagicMock()
            resp.status_code = 200
            return resp

        long_snippet = "X" * 500
        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            await slack.send(_make_event(snippet=long_snippet))

        # 200-char snippet + presentation chrome. The X-run should be exactly 200.
        text = captured["json"]["text"]
        assert "X" * 200 in text
        assert "X" * 201 not in text
    finally:
        await slack.shutdown()


# ---------------------------------------------------------------------------
# slack.send — exception swallowing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_send_swallows_timeout():
    """httpx.TimeoutException is caught + logged. No raise."""
    import httpx
    await slack.init("https://hooks.slack.com/test")
    try:
        async def fake_post(url, json=None, **kwargs):
            raise httpx.TimeoutException("simulated timeout")

        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            # Must not raise.
            await slack.send(_make_event())
    finally:
        await slack.shutdown()


@pytest.mark.asyncio
async def test_slack_send_swallows_connection_error():
    """httpx.ConnectError (RequestError subclass) is caught + logged."""
    import httpx
    await slack.init("https://hooks.slack.com/test")
    try:
        async def fake_post(url, json=None, **kwargs):
            raise httpx.ConnectError("simulated connection refused")

        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            await slack.send(_make_event())
    finally:
        await slack.shutdown()


@pytest.mark.asyncio
async def test_slack_send_swallows_generic_exception():
    """Any other exception is caught + logged at WARN. No raise."""
    await slack.init("https://hooks.slack.com/test")
    try:
        async def fake_post(url, json=None, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            await slack.send(_make_event())
    finally:
        await slack.shutdown()


@pytest.mark.asyncio
async def test_slack_send_swallows_5xx():
    """5xx responses are logged but never raised."""
    await slack.init("https://hooks.slack.com/test")
    try:
        async def fake_post(url, json=None, **kwargs):
            resp = mock.MagicMock()
            resp.status_code = 503
            return resp

        with mock.patch.object(slack._client, "post", side_effect=fake_post):
            await slack.send(_make_event())
    finally:
        await slack.shutdown()


# ---------------------------------------------------------------------------
# slack._presentation_for_kind — fallback
# ---------------------------------------------------------------------------


def test_presentation_for_unknown_event_kind_has_safe_fallback():
    """An EventType missing from the table still produces a sane dict."""
    # Re-use an existing EventType but force it to bypass the table by
    # temporarily yanking it. The cleaner path: call the helper directly
    # with each known kind to confirm none crash, and then temporarily
    # delete one entry to exercise the fallback branch.
    saved = slack._PRESENTATIONS.pop(EventType.ERROR, None)
    try:
        result = slack._presentation_for_kind(EventType.ERROR)
        assert ":mega:" in result["emoji"]
        assert "error" == result["title"]  # kind.value as title fallback
    finally:
        if saved is not None:
            slack._PRESENTATIONS[EventType.ERROR] = saved


# ---------------------------------------------------------------------------
# Router fanout — ntfy + slack both fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_emits_to_slack_after_ntfy():
    """A single emit fans out to BOTH channels; ntfy.send precedes slack.send."""
    await ntfy.init("https://ntfy.sh", "test-topic")
    await slack.init("https://hooks.slack.com/test")
    try:
        cfg = _FakeNotificationsConfig()
        router = NotificationRouter(cfg, asyncio.get_running_loop())
        await router.start()
        try:
            call_order = []

            async def fake_ntfy_send(event, public_base_url=None):
                call_order.append("ntfy")

            async def fake_slack_send(event):
                call_order.append("slack")

            with mock.patch.object(ntfy, "send", side_effect=fake_ntfy_send), \
                 mock.patch.object(slack, "send", side_effect=fake_slack_send):
                router.emit(_make_event())
                # Wait for the worker to drain.
                await asyncio.wait_for(router._queue.join(), timeout=2.0)

            assert call_order == ["ntfy", "slack"]
        finally:
            await router.stop()
    finally:
        await slack.shutdown()
        await ntfy.shutdown()


@pytest.mark.asyncio
async def test_router_slack_only_config_still_dispatches():
    """ntfy_topic empty but slack_webhook_url set → emit still flows;
    slack.send fires, ntfy.send still gets called (it self-skips on empty
    topic, so harmless). Verifies the relaxed emit gate."""
    await ntfy.init("https://ntfy.sh", "")  # ntfy disabled
    await slack.init("https://hooks.slack.com/test")
    try:
        cfg = _FakeNotificationsConfig(ntfy_topic="")
        router = NotificationRouter(cfg, asyncio.get_running_loop())
        await router.start()
        try:
            slack_calls = []

            async def fake_slack_send(event):
                slack_calls.append(event)

            with mock.patch.object(slack, "send", side_effect=fake_slack_send):
                router.emit(_make_event())
                await asyncio.wait_for(router._queue.join(), timeout=2.0)

            assert len(slack_calls) == 1
        finally:
            await router.stop()
    finally:
        await slack.shutdown()
        await ntfy.shutdown()


@pytest.mark.asyncio
async def test_router_drops_emit_when_no_channel_configured():
    """Both ntfy_topic and slack_webhook_url empty → emit is a no-op."""
    await ntfy.init("https://ntfy.sh", "")
    await slack.init("")
    try:
        cfg = _FakeNotificationsConfig(ntfy_topic="", slack_webhook_url="")
        router = NotificationRouter(cfg, asyncio.get_running_loop())
        await router.start()
        try:
            # Mock both so we'd detect any leak.
            with mock.patch.object(ntfy, "send") as mock_ntfy, \
                 mock.patch.object(slack, "send") as mock_slack:
                router.emit(_make_event())
                # Give the worker a tick to NOT do anything.
                await asyncio.sleep(0.05)
                assert mock_ntfy.call_count == 0
                assert mock_slack.call_count == 0
        finally:
            await router.stop()
    finally:
        await slack.shutdown()
        await ntfy.shutdown()


# ---------------------------------------------------------------------------
# session_manager.record_toast — router emit
# ---------------------------------------------------------------------------


def _build_session_manager_with_session(tmp_path):
    """Spin up a SessionManager and inject a single fake Session so
    record_toast doesn't ValueError. We don't actually start a backend —
    record_toast only touches self.sessions, accent resolution (which
    tolerates a missing working_dir), and the optional router.
    """
    from src.core.session_manager import SessionManager
    from src.models import Session

    sm = SessionManager()
    session = Session(
        id="sess-abc-123",
        pid=12345,
        status="running",
        working_dir=str(tmp_path),
        tmux_session=None,
        backend="pty",
    )
    sm.sessions["sess-abc-123"] = session
    return sm


def test_record_toast_emits_notification_event(tmp_path):
    """kind='Stop' → router.emit called with EventType.CLAUDE_STOP."""
    sm = _build_session_manager_with_session(tmp_path)
    fake_router = mock.MagicMock()
    sm.attach_notification_router(fake_router)

    sm.record_toast(
        session_id="sess-abc-123",
        kind="Stop",
        title="Claude stopped",
        body="task done",
    )

    assert fake_router.emit.call_count == 1
    emitted = fake_router.emit.call_args.args[0]
    assert emitted.kind == EventType.CLAUDE_STOP
    assert emitted.session_slug == "sess-abc-123"
    # snippet falls back to body.
    assert emitted.snippet == "task done"


def test_record_toast_permissionrequest_maps_to_permission_request(tmp_path):
    sm = _build_session_manager_with_session(tmp_path)
    fake_router = mock.MagicMock()
    sm.attach_notification_router(fake_router)

    sm.record_toast(
        session_id="sess-abc-123",
        kind="PermissionRequest",
        title="Permission",
        body="tool foo",
    )

    emitted = fake_router.emit.call_args.args[0]
    assert emitted.kind == EventType.CLAUDE_PERMISSION_REQUEST


def test_record_toast_notification_maps_to_claude_notification(tmp_path):
    sm = _build_session_manager_with_session(tmp_path)
    fake_router = mock.MagicMock()
    sm.attach_notification_router(fake_router)

    sm.record_toast(
        session_id="sess-abc-123",
        kind="Notification",
        title="Heads up",
        body="needs input",
    )

    emitted = fake_router.emit.call_args.args[0]
    assert emitted.kind == EventType.CLAUDE_NOTIFICATION


def test_record_toast_unknown_kind_skips_router_emit(tmp_path):
    """A kind not in the map → no router.emit call. Toast still recorded."""
    sm = _build_session_manager_with_session(tmp_path)
    fake_router = mock.MagicMock()
    sm.attach_notification_router(fake_router)

    toast = sm.record_toast(
        session_id="sess-abc-123",
        kind="UnknownKind",
        title="??",
        body="whatever",
    )

    assert fake_router.emit.call_count == 0
    # Toast still in the bucket.
    assert toast.kind == "UnknownKind"


def test_record_toast_no_router_attached_is_safe(tmp_path):
    """No router attached → record_toast returns the Toast without crashing."""
    sm = _build_session_manager_with_session(tmp_path)
    # Do NOT call attach_notification_router.

    toast = sm.record_toast(
        session_id="sess-abc-123",
        kind="Stop",
        title="t",
        body="b",
    )
    assert toast.id  # ran to completion
