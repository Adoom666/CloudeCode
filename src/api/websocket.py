"""WebSocket endpoints for real-time PTY communication."""

# JWT auth via Sec-WebSocket-Protocol: client sends ['cloude.jwt.v1', <token>].

import asyncio
import json
import base64
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Optional, Set
import structlog

from src.models import (
    WSMessageType,
    WSPTYDataMessage,
    WSPTYInputMessage,
    WSPTYResizeMessage,
    WSErrorMessage
)
from src.api.deps import verify_jwt_from_subprotocol, SUBPROTOCOL_MARKER

logger = structlog.get_logger()

router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        """Initialize connection manager."""
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        """
        Accept and register a new WebSocket connection.

        NOTE: As of the subprotocol-auth change (Item 3), the handler is
        responsible for calling `websocket.accept(subprotocol=...)` BEFORE
        invoking this method — the browser requires the server to echo the
        negotiated subprotocol, so accept() must happen at the auth site.
        This method now only registers an already-accepted socket.

        Args:
            websocket: WebSocket connection to register (already accepted)
        """
        self.active_connections.add(websocket)
        logger.info("websocket_connected", total_connections=len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        """
        Unregister a WebSocket connection.

        Args:
            websocket: WebSocket connection to unregister
        """
        self.active_connections.discard(websocket)
        logger.info("websocket_disconnected", total_connections=len(self.active_connections))

    async def broadcast(self, message: str):
        """
        Broadcast a message to all connected clients.

        Args:
            message: Message to broadcast (JSON string)
        """
        for connection in self.active_connections.copy():
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error("broadcast_failed", error=str(e))
                self.active_connections.discard(connection)


# Global connection manager
connection_manager = ConnectionManager()


@router.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    WebSocket endpoint for real-time PTY terminal streaming.

    Provides bidirectional communication:
    - Server -> Client: PTY output data (base64 encoded)
    - Client -> Server: User input, resize events

    Args:
        websocket: WebSocket connection

    Auth protocol:
        Client opens WS with subprotocols=["cloude.jwt.v1", <jwt_token>].
        Server validates the JWT, accepts the handshake echoing the marker
        as the negotiated subprotocol. Failures close with:
          - 4401 on missing marker / missing token / invalid token
          - 4400 on malformed Sec-WebSocket-Protocol header
    """
    # Validate auth BEFORE accepting. If we close pre-accept, FastAPI sends
    # HTTP 403 (the browser sees the handshake fail), which is the correct
    # behavior — no WS connection is ever established with an invalid token.
    ok, detail = verify_jwt_from_subprotocol(websocket)
    if not ok:
        # Close codes in the 4xxx app range per RFC 6455 / IANA registry.
        # 4401 = auth failure (our convention, modeled on HTTP 401).
        # 4400 = bad request — header present but malformed (empty /
        #        whitespace-only). Absence of the header is an auth failure
        #        (client simply didn't present credentials), not a protocol
        #        error.
        raw_header = websocket.headers.get("sec-websocket-protocol")
        header_present_but_empty = (
            raw_header is not None
            and (not raw_header.strip() or all(not p.strip() for p in raw_header.split(",")))
        )
        code = 4400 if header_present_but_empty else 4401
        logger.warning(
            "websocket_auth_failed",
            reason=detail,
            close_code=code,
            has_header=raw_header is not None,
        )
        await websocket.close(code=code, reason=detail or "auth failed")
        return

    # Echo the subprotocol marker back — required by RFC 6455 § 4.1. If we
    # accept() without a matching subprotocol the browser will drop the
    # connection client-side even though the TCP handshake "succeeded".
    await websocket.accept(subprotocol=SUBPROTOCOL_MARKER)
    await connection_manager.connect(websocket)

    # Get app state
    session_manager = websocket.app.state.session_manager
    auto_tunnel = websocket.app.state.auto_tunnel
    log_monitor = websocket.app.state.log_monitor

    # Subscribe to PTY output
    pty_output_queue = session_manager.subscribe_output()

    # Subscribe to tunnel events (keep for port detection)
    tunnel_queue = auto_tunnel.subscribe()

    # Subscribe to log events (keep for system messages)
    log_queue = log_monitor.subscribe()

    # Send initial connection message
    try:
        welcome_msg = {
            "type": "log",
            "content": "[SYSTEM] WebSocket connected - PTY terminal ready",
            "timestamp": datetime.utcnow().isoformat()
        }
        await websocket.send_text(json.dumps(welcome_msg))
    except Exception as e:
        logger.error("failed_to_send_welcome", error=str(e))

    # ---- Resize handshake (replaces legacy scrollback replay) ----
    #
    # Why the handshake: historical scrollback was captured at the pane's
    # PREVIOUS geometry. If the reconnecting client's viewport is different
    # (common — rotation, window resize, different device), replaying those
    # frozen bytes paints them at the wrong coordinates and you get visible
    # character shrapnel until the next full app redraw.
    #
    # New contract:
    #   1. Server -> Client:  {"type": "request_dims"}
    #   2. Client -> Server:  {"type": "pty_resize", cols, rows}  (bypasses
    #                         the 100ms debounce client-side — this is the
    #                         handshake path, not a normal user-driven
    #                         resize)
    #   3. Server applies backend.resize(cols, rows)
    #   4. Server sleeps ~150ms so SIGWINCH reaches the pane's foreground
    #      process (Claude/bash/etc.) and that process has a chance to
    #      finish any in-flight ANSI write before we stomp its buffer.
    #   5. Server writes Ctrl+L (0x0c) to the pane. Claude/bash/readline
    #      treat Ctrl+L as "redraw" — the app clears its own screen and
    #      re-renders at the NEW size. Live-stream bytes then arrive via
    #      pipe-pane as usual.
    #
    # Trade-off: user loses historical scrollback on reconnect. Accepted
    # because a clean screen beats a corrupted one, and xterm.js retains
    # its own client-side scrollback within a single page load anyway.
    try:
        await websocket.send_text(json.dumps({
            "type": WSMessageType.REQUEST_DIMS,
        }))
        logger.debug("ws_request_dims_sent")

        # Wait for the client's handshake pty_resize. We accept the NEXT
        # pty_resize message we see and ignore binary input and other
        # control frames until it arrives. Bounded timeout: if the client
        # never replies, we still proceed (backend stays at birth dims +
        # the app redraw still fires via Ctrl+L at whatever size that is).
        handshake_cols: Optional[int] = None
        handshake_rows: Optional[int] = None
        handshake_deadline_s = 2.0  # generous but bounded
        handshake_start = asyncio.get_event_loop().time()
        while True:
            remaining = handshake_deadline_s - (
                asyncio.get_event_loop().time() - handshake_start
            )
            if remaining <= 0:
                logger.warning("ws_handshake_timeout")
                break
            try:
                raw = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.warning("ws_handshake_timeout")
                break

            if "text" in raw and raw["text"]:
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == WSMessageType.PTY_RESIZE:
                    try:
                        handshake_cols = int(msg["cols"])
                        handshake_rows = int(msg["rows"])
                    except (KeyError, ValueError, TypeError):
                        logger.warning("ws_handshake_bad_dims", msg=msg)
                    break
                # Ignore other control frames during the handshake window
                # (ping, etc.); they'll be processed by receive_messages
                # once the loop starts.
                continue
            # Drop binary frames that arrive before the handshake — the
            # user can't have typed anything yet. In practice clients
            # don't send binary before their first resize, but be safe.

        if handshake_cols and handshake_rows:
            logger.info(
                "ws_handshake_resize",
                cols=handshake_cols,
                rows=handshake_rows,
            )
            try:
                session_manager.resize_terminal(handshake_cols, handshake_rows)
            except Exception as exc:
                logger.error("ws_handshake_resize_failed", error=str(exc))

            # Let SIGWINCH propagate + foreground app finish any mid-flight
            # write. 150ms is empirically enough for tmux -> pane delivery
            # and for Claude/bash to ack the signal. We use asyncio.sleep
            # so the event loop keeps draining other tasks.
            await asyncio.sleep(0.15)

            # Force a redraw at the new size. Ctrl+L is readline / tmux /
            # Claude's "clear + repaint" convention — the app owns the
            # repaint, which means it paints at its CURRENT (post-resize)
            # cell grid, not the stale grid any cached output was drawn in.
            if session_manager.backend is not None:
                try:
                    await session_manager.backend.write(b"\x0c")
                    logger.debug("ws_handshake_ctrl_l_sent")
                except Exception as exc:
                    logger.warning("ws_handshake_ctrl_l_failed", error=str(exc))
    except WebSocketDisconnect:
        # Client bailed during the handshake. Let the outer handler deal
        # with cleanup; no point proceeding to the live-stream loop.
        logger.info("ws_handshake_client_disconnected")
        session_manager.unsubscribe_output(pty_output_queue)
        auto_tunnel.unsubscribe(tunnel_queue)
        log_monitor.unsubscribe(log_queue)
        connection_manager.disconnect(websocket)
        return
    except Exception as exc:
        logger.error("ws_handshake_error", error=str(exc))

    try:
        # Create tasks for receiving and sending
        receive_task = asyncio.create_task(
            receive_messages(websocket, session_manager)
        )
        send_pty_task = asyncio.create_task(
            send_pty_output(websocket, pty_output_queue, log_monitor)
        )
        send_tunnels_task = asyncio.create_task(
            send_queue_messages(websocket, tunnel_queue)
        )
        send_logs_task = asyncio.create_task(
            send_queue_messages(websocket, log_queue)
        )

        # Wait for any task to complete (or fail)
        done, pending = await asyncio.wait(
            [receive_task, send_pty_task, send_tunnels_task, send_logs_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.info("websocket_client_disconnected")
    except Exception as e:
        logger.error("websocket_error", error=str(e))
    finally:
        # Cleanup
        session_manager.unsubscribe_output(pty_output_queue)
        auto_tunnel.unsubscribe(tunnel_queue)
        log_monitor.unsubscribe(log_queue)
        connection_manager.disconnect(websocket)


async def receive_messages(websocket: WebSocket, session_manager):
    """
    Receive messages from the WebSocket client.

    Args:
        websocket: WebSocket connection
        session_manager: SessionManager instance
    """
    try:
        while True:
            # Try to receive binary data first (PTY input)
            try:
                message = await websocket.receive()

                # Handle binary PTY input
                if "bytes" in message:
                    data = message["bytes"]
                    logger.debug("pty_input_received", data_len=len(data))
                    try:
                        # Convert bytes to string for PTY input
                        text = data.decode('utf-8')
                        await session_manager.send_input(text)
                    except Exception as e:
                        logger.error("input_failed", error=str(e))
                        error_msg = WSErrorMessage(
                            error="input_failed",
                            message=str(e)
                        )
                        await websocket.send_text(error_msg.model_dump_json())

                # Handle text messages (control messages)
                elif "text" in message:
                    data = message["text"]
                    try:
                        msg = json.loads(data)
                        msg_type = msg.get("type")

                        if msg_type == WSMessageType.PTY_RESIZE:
                            # Handle terminal resize
                            resize_msg = WSPTYResizeMessage(**msg)
                            logger.info("terminal_resize_request", cols=resize_msg.cols, rows=resize_msg.rows)
                            try:
                                session_manager.resize_terminal(resize_msg.cols, resize_msg.rows)
                            except Exception as e:
                                logger.error("resize_failed", error=str(e))

                        elif msg_type == WSMessageType.PING:
                            # Respond to ping
                            pong_msg = {"type": WSMessageType.PONG}
                            await websocket.send_text(json.dumps(pong_msg))

                    except json.JSONDecodeError:
                        logger.warning("invalid_json_received", data=data[:100])
                    except Exception as e:
                        logger.error("message_processing_error", error=str(e))

            except RuntimeError as e:
                # Handle the case where websocket is already disconnected
                if "Cannot call \"receive\" once a disconnect message has been received" in str(e):
                    logger.debug("websocket_already_disconnected", error=str(e))
                    break
                else:
                    logger.error("receive_error", error=str(e))
                    raise
            except Exception as e:
                logger.error("receive_error", error=str(e))
                raise

    except WebSocketDisconnect:
        raise
    except Exception as e:
        logger.error("receive_messages_error", error=str(e))
        raise


async def send_pty_output(websocket: WebSocket, queue: asyncio.Queue, log_monitor=None):
    """
    Send PTY output from queue to the WebSocket client as binary frames.

    Args:
        websocket: WebSocket connection
        queue: Queue containing PTY output (base64 encoded strings)
        log_monitor: Optional LogMonitor for pattern detection
    """
    try:
        while True:
            # Wait for PTY output (base64 encoded)
            encoded_data = await queue.get()

            try:
                # Decode base64 to raw bytes
                raw_bytes = base64.b64decode(encoded_data)

                # Pattern detection + idle watching. We skip both when the
                # backend is in replay mode so replayed scrollback doesn't
                # look like "new" activity to downstream consumers.
                sm = websocket.app.state.session_manager
                in_replay = (
                    sm is not None
                    and sm.backend is not None
                    and getattr(sm.backend, "replay_in_progress", False)
                )
                if log_monitor and not in_replay:
                    try:
                        text = raw_bytes.decode('utf-8', errors='replace')
                        # Run pattern detection on the output (Item 6 wiring)
                        log_monitor._detect_patterns(text)
                    except Exception as e:
                        # Don't let pattern detection errors break output streaming
                        logger.debug("pattern_detection_error", error=str(e))

                # Item 7: feed the per-session IdleWatcher. It buffers the
                # tail, classifies, and fires PERMISSION_PROMPT synchronously
                # / TASK_COMPLETE from its background poll. Errors are
                # swallowed — terminal streaming is load-bearing, notifications
                # are not.
                if sm is not None and sm.idle_watcher is not None and not in_replay:
                    try:
                        await sm.idle_watcher.handle_chunk(raw_bytes)
                    except Exception as e:
                        logger.debug("idle_watcher_chunk_error", error=str(e))

                # Send as binary frame directly
                await websocket.send_bytes(raw_bytes)
            except Exception as e:
                logger.error("send_pty_output_error", error=str(e))
                raise

    except asyncio.CancelledError:
        # Task was cancelled, exit gracefully
        pass
    except Exception as e:
        logger.error("send_pty_output_error", error=str(e))
        raise


async def send_queue_messages(websocket: WebSocket, queue: asyncio.Queue):
    """
    Send messages from a queue to the WebSocket client.

    Args:
        websocket: WebSocket connection
        queue: Queue to read messages from
    """
    try:
        while True:
            # Wait for message in queue
            message = await queue.get()

            try:
                # Send to client
                await websocket.send_text(message)
            except Exception as e:
                logger.error("send_message_error", error=str(e))
                raise

    except asyncio.CancelledError:
        # Task was cancelled, exit gracefully
        pass
    except Exception as e:
        logger.error("send_queue_messages_error", error=str(e))
        raise
