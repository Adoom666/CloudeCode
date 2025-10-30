"""WebSocket endpoints for real-time PTY communication."""

import asyncio
import json
import base64
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set
import structlog

from src.models import (
    WSMessageType,
    WSPTYDataMessage,
    WSPTYInputMessage,
    WSPTYResizeMessage,
    WSErrorMessage
)
from src.api.auth import verify_jwt_token

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

        Args:
            websocket: WebSocket connection to register
        """
        await websocket.accept()
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
    """
    # Check authentication via query parameter
    token = websocket.query_params.get("token")
    if not token or not verify_jwt_token(token):
        logger.warning("websocket_auth_failed", query_params=websocket.query_params.keys())
        await websocket.close(code=1008, reason="Authentication required")
        return

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

                # Pattern detection: decode and analyze output
                if log_monitor:
                    try:
                        text = raw_bytes.decode('utf-8', errors='replace')
                        # Run pattern detection on the output
                        log_monitor._detect_patterns(text)
                    except Exception as e:
                        # Don't let pattern detection errors break output streaming
                        logger.debug("pattern_detection_error", error=str(e))

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
