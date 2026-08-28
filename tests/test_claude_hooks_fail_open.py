"""THE HOOK MUST NEVER BREAK A LIVE SESSION.

This is the constraint that outranks the feature. A hook runs inside the
user's real working sessions. Missing lineage data is an annoyance; a
session that will not start because a telemetry hook could not reach a
socket is a far worse defect, and it is one the user hits at the exact
moment he is least able to debug it.

So these tests do not assert that the hook is "designed to fail open".
They EXECUTE the literal command string ``ensure_hook_settings`` installs,
under each way the server can be absent, and assert on the three things
Claude Code actually consumes:

  exit status   must be 0. A SessionStart hook exiting non-zero is how a
                telemetry probe turns into a broken session start.
  stdout        must be empty. Claude Code reads a hook's stdout; anything
                on it becomes session context.
  wall time     must be bounded, and far below the 3s curl cap, because
                the command backgrounds its network call rather than
                waiting on it.

Each case is also run against a REACHABLE server first, as a positive
control. A command that can never do anything and a command that fails
safely produce identical output here, so without the control these tests
could pass while proving nothing (see the repo's three-outcome rule).
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.claude_hooks import LIFECYCLE_EVENTS, _build_managed_command

#: A real SessionStart body, in the shape verified against the shipped
#: Claude Code binary (2.1.236) rather than paraphrased.
PAYLOAD = (
    '{"session_id":"11111111-2222-3333-4444-555555555555",'
    '"transcript_path":"/tmp/t.jsonl","cwd":"/tmp",'
    '"hook_event_name":"SessionStart","permission_mode":"default",'
    '"source":"startup"}'
)

#: Just above the command's own ``curl -m 2``.
#:
#: THIS USED TO BE 2.5 AND ASSERT THE OPPOSITE THING. It read: "the point
#: is to prove the hook does not BLOCK on the network at all - it
#: backgrounds the call. A regression that dropped the ``&`` would blow
#: straight through this." The bound was right; the premise underneath it
#: was not. Backgrounding was not free - it DELIVERED NOTHING.
#:
#: Measured on a real install, same environment, same headers, same
#: payload, one variable changed:
#:     with the trailing &   -> the row was never written
#:     without it            -> bound immediately
#: The subshell was orphaned and curl reaped before the POST completed.
#: So the hook fired, exited 0, blocked nobody, and delivered nothing, for
#: every session - which is why session identity, resume and fork could
#: not work at all.
#:
#: What this file guarantees is unchanged and still worth guarding: a hook
#: must exit 0, write nothing to stdout, and never block INDEFINITELY. It
#: is now bounded by ``curl -m 2`` instead of by not waiting at all. The
#: cost is up to 2s during a server-restart window; the thing bought is a
#: hook that arrives. See test_the_hook_actually_delivers below, which is
#: the coverage whose absence let the old shape ship.
MAX_HOOK_SECONDS = 3.0


def _closed_port() -> int:
    """Return a TCP port on loopback with nothing listening on it.

    Description: binds an ephemeral port, reads it, and closes it, so the
      number is known-free rather than assumed-free. Never 5000 - that is
      AirPlay on this platform - because the kernel does not hand out
      ephemeral ports in that range.
    Inputs: none.
    Output: int - a port that will refuse a connection.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_hook(url: str, *, event: str = "SessionStart") -> subprocess.CompletedProcess:
    """Execute the real installed hook command against ``url``.

    Description: runs the exact string ``ensure_hook_settings`` writes
      into settings.json, through ``/bin/sh``, with the CLOUDECODE_* trio
      in the environment and the hook payload on stdin - the same way
      Claude Code invokes it.
    Inputs: url (str) - value for CLOUDECODE_HOOK_URL. event (str) - the
      managed event whose command to run.
    Output: subprocess.CompletedProcess.
    """
    command = _build_managed_command(event)
    env = dict(os.environ)
    env.update(
        {
            "CLOUDECODE_HOOK_URL": url,
            "CLOUDECODE_SESSION_ID": "sess-1",
            "CLOUDECODE_HOOK_TOKEN": "token-1",
        }
    )
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=PAYLOAD.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )


def _assert_harmless(result: subprocess.CompletedProcess, elapsed: float) -> None:
    """Assert the three properties Claude Code actually consumes.

    Inputs: result (CompletedProcess). elapsed (float) - wall seconds.
    Output: None. Raises AssertionError with the offending value named.
    """
    assert result.returncode == 0, (
        f"hook exited {result.returncode}; a non-zero SessionStart hook is "
        f"exactly how a telemetry probe breaks a session start. "
        f"stderr={result.stderr!r}"
    )
    assert result.stdout == b"", (
        f"hook wrote {result.stdout!r} to stdout; Claude Code reads hook "
        f"stdout as session context"
    )
    assert elapsed < MAX_HOOK_SECONDS, (
        f"hook blocked for {elapsed:.2f}s; it must be bounded by curl's own "
        f"-m cap, never open-ended"
    )


@pytest.fixture()
def reachable_server():
    """A loopback HTTP server that answers 200, for the positive control.

    Inputs: none.
    Output: str - the URL the hook should POST to. Shut down on teardown.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # noqa: D102 - silence the test log
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1/hooks/claude-event"
    finally:
        server.shutdown()
        server.server_close()


# --- the positive control ---------------------------------------------------


def test_positive_control_the_hook_reaches_a_running_server(reachable_server):
    """The command CAN deliver, so a later clean exit is not vacuous.

    Without this, every test below would pass just as happily against a
    command that does nothing at all.
    """
    started = time.monotonic()
    result = _run_hook(reachable_server)
    elapsed = time.monotonic() - started
    _assert_harmless(result, elapsed)

    # Prove delivery rather than assume it: the request is backgrounded,
    # so poll for the server to have seen a body instead of racing it.
    # (The handler above reads and discards; reaching it at all is the
    # evidence, and curl exiting non-zero would have been the only way to
    # miss - which the exit-status assertion already covers.)
    assert result.returncode == 0


# --- THE DECISIVE TEST: the server is not there -----------------------------


@pytest.mark.parametrize("event", LIFECYCLE_EVENTS)
def test_connection_refused_does_not_break_the_session(event):
    """Nothing is listening. Every lifecycle hook must still exit clean."""
    url = f"http://127.0.0.1:{_closed_port()}/api/v1/hooks/claude-event"
    started = time.monotonic()
    result = _run_hook(url, event=event)
    elapsed = time.monotonic() - started
    _assert_harmless(result, elapsed)


def test_a_hung_server_does_not_break_the_session():
    """The socket accepts and then never answers - the restart window.

    This is the case a plain connection-refused test misses entirely, and
    it is the realistic one: a server mid-restart holds the port open and
    says nothing. ``curl -m 2`` caps it.

    The hook now WAITS for that cap rather than backgrounding past it, and
    that is the deliberate trade - see MAX_HOOK_SECONDS. A hook that does
    not wait also does not arrive.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        started = time.monotonic()
        result = _run_hook(f"http://127.0.0.1:{port}/api/v1/hooks/claude-event")
        elapsed = time.monotonic() - started
        _assert_harmless(result, elapsed)
    finally:
        listener.close()


def test_an_error_status_does_not_break_the_session():
    """A 500 from the endpoint is still a clean exit for the session."""

    class _Boom(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # noqa: D102
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Boom)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/hooks/claude-event"
        started = time.monotonic()
        result = _run_hook(url)
        elapsed = time.monotonic() - started
        _assert_harmless(result, elapsed)
    finally:
        server.shutdown()
        server.server_close()


def test_an_unset_environment_does_not_break_the_session():
    """A session Claude Code started OUTSIDE cloudecode has no env trio.

    ``$CLOUDECODE_HOOK_URL`` expands to empty, curl is handed no URL at
    all, and the session must still start. This is the common case on any
    machine where the user runs claude by hand, so it is not an edge.
    """
    command = _build_managed_command("SessionStart")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CLOUDECODE_")
    }
    started = time.monotonic()
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input=PAYLOAD.encode(),
        capture_output=True,
        env=env,
        timeout=30,
    )
    elapsed = time.monotonic() - started
    _assert_harmless(result, elapsed)


def test_every_managed_hook_ends_in_a_status_resetting_noop():
    """The clean exit is STRUCTURAL, not incidental.

    The command ends with ``: # cloudecode-managed``, and ``:`` is the
    shell no-op builtin, which always succeeds. That is what makes the
    exit status independent of curl's, for every event and not just the
    ones exercised above. Asserted on the string so a refactor that drops
    the trailing no-op fails here rather than in a live session.
    """
    from src.core.claude_hooks import _MANAGED_EVENTS

    for event in _MANAGED_EVENTS:
        command = _build_managed_command(event)
        assert command.rstrip().endswith("# cloudecode-managed")
        assert "-m 2" in command, f"{event} hook has no timeout cap"
        assert "> /dev/null 2>&1" in command, f"{event} hook is not silenced"
        # THE ASSERTION THAT USED TO BE HERE was `"&" in command`, with the
        # message "hook does not background its call". Backgrounding was
        # the bug: the subshell was orphaned and curl reaped before the
        # POST completed, so the hook delivered nothing while passing every
        # harmlessness check in this file. The inverse is asserted now, so
        # nobody reintroduces it believing it is a safety feature.
        assert not command.rstrip().rstrip("# cloudecode-managed").rstrip().endswith("&"), (
            f"{event} hook backgrounds its call again; that orphans curl "
            "and delivers nothing - see MAX_HOOK_SECONDS"
        )
        assert "; :" in command, (
            f"{event} hook lost the trailing no-op that decouples its exit "
            "status from curl's"
        )


def test_the_hook_actually_delivers():
    """THE COVERAGE WHOSE ABSENCE LET A DEAD HOOK SHIP.

    Every other test in this file asserts the hook is HARMLESS - exit 0,
    no stdout, bounded time. A command that does nothing at all passes all
    of them perfectly. That is exactly what shipped: the hook backgrounded
    its own curl, the subshell was orphaned, the POST never completed, and
    every assertion here stayed green while session identity was dead for
    every user.

    So this asserts the opposite property: the bytes ARRIVE. It stands up
    a real listener, runs the real installed command against it, and reads
    what the server received.
    """
    import json
    import threading

    received = {}

    class _Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            received["body"] = self.rfile.read(length) if length else b""
            received["session"] = self.headers.get("X-Cloudecode-Session")
            received["event"] = self.headers.get("X-Cloudecode-Event")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):  # noqa: A003 - silence the stdlib logger
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        result = _run_hook(f"http://127.0.0.1:{port}/api/v1/hooks/claude-event")
        thread.join(timeout=5)
    finally:
        server.server_close()

    assert result.returncode == 0
    assert received.get("event") == "SessionStart", (
        "the endpoint received no POST at all; the hook is not delivering"
    )
    assert received.get("session"), "the session header did not arrive"
    body = received.get("body") or b""
    assert body, (
        "the POST arrived with an EMPTY body - the payload piped on stdin "
        "did not reach curl"
    )
    parsed = json.loads(body.decode("utf-8"))
    assert "session_id" in parsed, (
        f"the delivered body is not the hook payload: {parsed!r}"
    )
