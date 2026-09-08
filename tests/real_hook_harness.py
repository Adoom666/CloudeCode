"""A real claude, in a real tmux pane, POSTing real hooks at a real server.

WHAT THIS IS FOR. Every other test of the status LED asserts against a
hand-built row: a dict spelled the way the author believed the server
spells it. That proves the mapping and proves nothing about the pipeline,
and this project's whole hazard list is pipelines that look right. So this
harness stands the real thing up end to end - the app's own create path,
the production hook one-liner, a genuine ``claude`` binary, the real
``GET /sessions/list`` - and the assertions read what the USER would see.

FIVE THINGS WERE MEASURED RATHER THAN ASSUMED, on 2026-09-08 against
claude 2.1.265 and tmux 3.7c, and each one changed the design:

1. ``claude --dangerously-skip-permissions`` does NOT clear the workspace
   trust dialog. The pane sat on it for 40s and fired ZERO hooks. Bypass
   mode also has a one-time acceptance dialog of its own, so the flag adds
   a second prompt rather than removing the first.
2. ``CLAUDE_CONFIG_DIR`` DOES relocate the trust record - and loses
   authentication with it. The pane read "Not logged in / Run /login" and
   fired no hooks. So the config dir cannot be relocated, and trust cannot
   be pre-seeded hermetically.
3. Pre-seeding trust by editing ``~/.claude.json`` is REFUSED here. That
   file is read-modify-written by every live claude on the box, and a lost
   update would clobber the owner's config to save this test twenty
   seconds.
4. What is left is to ANSWER the dialog, which turns the obstacle into the
   first real assertion: a pane that is alive, has a real pid, and has
   fired no hook is exactly punchlist 19, and the startup gate must say
   ``awaiting_startup_prompt`` about it. Claude itself then writes the
   trust record, so there is no race.
5. ``date +%s`` does NOT raise a permission prompt - claude's own
   allowlist runs it. A ``permissions.ask`` rule in THIS run's settings
   file makes ``PermissionRequest`` deterministic and fast (measured 1.8s
   after the prompt, against ~60s for the idle ``Notification`` nudge).

HOW THE PANE LEARNS OUR HOOKS, without touching the owner's settings.
``tests/conftest.py`` points ``CLOUDE_CLAUDE_SETTINGS_PATH`` at a
throwaway and ``src/core/test_write_guard.py`` refuses a write to the real
one, so the app under test cannot install hooks where the pane's claude
reads them. Instead the block is built by the PRODUCTION
``claude_hooks._build_hook_block()`` - so the curl one-liner exercised is
byte-identical to the shipped one - written to a temp settings file, and
handed to the pane as ``claude --settings <file>``, which is additive.

MEASURED, because the obvious worry turned out not to happen: on a box
whose real ``~/.claude/settings.json`` already carries the live app's
managed block, ``--settings`` does NOT stack a second copy - each event
arrived exactly ONCE across a full run (18 POSTs, no kind duplicated at
the same timestamp). Were that to change, it would be harmless anyway:
hook events are documented as duplicable and every consumer in
``src/core/session_activity.py`` is idempotent, so a doubled delivery
would be a free test of that contract rather than a defect in this file.

WHAT THE RUN LEAVES BEHIND, said out loud: one ``~/.claude.json`` project
entry for a ``/var/folders`` path, and one small transcript in the owner's
corpus. Both are what running claude in a directory always does, the temp
prefix is inside the one the transcript importer already excludes, and
neither is this app's database or config - those are the throwaway state
dir conftest already installs.

SOCKETS. Every tmux command goes to ``tests.socket_guard.TEST_SOCKET_NAME``
and the installed subprocess guard raises on any argv it cannot prove is
aimed there. A second ad-hoc socket name would be classified unsafe by
that guard, which is why this file does not invent one.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# --------------------------------------------------------------------- #
# opt-in gate and the named skips
# --------------------------------------------------------------------- #

#: The opt-in switch. This suite spends real Claude turns and about a
#: minute of wall clock, so it never runs by accident.
OPT_IN_ENV: str = "CLOUDE_REAL_HOOK_TESTS"

#: How long any single state assertion may wait before it must report
#: what it actually saw.
ASSERT_TIMEOUT_SECONDS: float = 30.0

#: Poll interval while waiting for a state.
#:
#: 0.25s rather than something lazier because these states are measured
#: against a live agent and some of them are short. It was originally set
#: here because ``finished_unread`` existed for only about a second and a
#: half: measured on claude 2.1.265, ``Stop`` lands and ``SubagentStop``
#: follows it 1.50s later on a turn with no subagent in it, and the stray
#: used to re-arm the working heartbeat. That is punchlist item 4 and it
#: is fixed, so ``finished_unread`` now persists - but the fast poll is
#: what makes a REGRESSION visible as a wrong reading rather than as a
#: state that had already moved on before anyone looked.
POLL_INTERVAL_SECONDS: float = 0.25


def skip_reason() -> Optional[str]:
    """Say why this suite cannot run, or None when it can.

    Description: every refusal NAMES what was not measured. "skipped" on
      its own is the silent pass this project keeps paying for, so each
      branch says which half of the pipeline went unexercised.
    Inputs: none.
    Output: Optional[str] - the reason, or None to proceed.
    Example: skip_reason() -> "CLOUDE_REAL_HOOK_TESTS is not set ..."
    """
    if os.environ.get(OPT_IN_ENV) != "1":
        return (
            f"{OPT_IN_ENV} is not set to 1, so NOTHING about the real hook "
            "pipeline was measured. This suite spends real Claude turns and "
            "about a minute of wall clock; run it deliberately."
        )
    if shutil.which("tmux") is None:
        return (
            "tmux is not on PATH, so no pane could be created and NOTHING "
            "about the hook pipeline was measured."
        )
    if shutil.which("claude") is None:
        return (
            "the `claude` binary is not on PATH, so no agent could be "
            "launched and NO real hook was ever fired."
        )
    if shutil.which("node") is None:
        return (
            "node is not on PATH, so client/js/status-led.js could not be "
            "executed and the LED half of every assertion is unmeasured."
        )
    return None


# --------------------------------------------------------------------- #
# the hook ledger
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookSighting:
    """One hook POST as the server saw it arrive.

    Fields: elapsed (float) seconds since the harness started the pane;
      event (str) the ``X-Cloudecode-Event`` header; session_id (str) the
      ``X-Cloudecode-Session`` header; status (int) the response code the
      route produced.
    """

    elapsed: float
    event: str
    session_id: str
    status: int


@dataclass
class HookLedger:
    """Ordered record of every hook POST this run's server received.

    Description: exists so a timeout can NAME what arrived. "timed out
      waiting for finished_unread" is useless; "hooks seen: SessionStart
      +1.9s, UserPromptSubmit +5.5s" names the defect. Headers only - the
      body is never read here, because consuming the request stream in a
      middleware is how you break the route you are trying to observe.
    """

    origin: float = field(default_factory=time.monotonic)
    sightings: list[HookSighting] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, event: str, session_id: str, status: int) -> None:
        """Append one sighting. Inputs: header values + status. Output: None."""
        with self._lock:
            self.sightings.append(
                HookSighting(
                    elapsed=time.monotonic() - self.origin,
                    event=event,
                    session_id=session_id,
                    status=status,
                )
            )

    def events(self, session_id: Optional[str] = None) -> list[str]:
        """Event kinds accepted (2xx) so far, in arrival order.

        Inputs: session_id (Optional[str]) - restrict to one session.
        Output: list[str].
        """
        with self._lock:
            return [
                s.event
                for s in self.sightings
                if s.status < 300 and (session_id is None or s.session_id == session_id)
            ]

    def describe(self) -> str:
        """One-line-per-sighting rendering for a failure message.

        Inputs: none. Output: str (``"(no hook POST ever arrived)"`` when empty).
        """
        with self._lock:
            if not self.sightings:
                return "(no hook POST ever arrived)"
            return "; ".join(
                f"{s.event}+{s.elapsed:.2f}s->{s.status}" for s in self.sightings
            )


def free_port() -> int:
    """Return a currently-free loopback TCP port.

    Inputs: none. Output: int.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def led_state_for(row: dict[str, Any]) -> dict[str, str]:
    """Ask the SHIPPED client JS what LED this row paints.

    Description: pipes the row verbatim into
      ``tests/led_state_for.node.mjs``, which loads
      ``client/js/status-led.js`` in a bare sandbox and calls
      ``ledStateFor``. Nothing about the mapping is re-implemented here -
      the question is what the user's browser does with this exact row.
    Inputs: row (dict) - a ``/sessions/list`` entry, wrapper level.
    Output: dict with ``inner`` and ``outer`` keys.
    Raises: RuntimeError when node refuses, carrying node's stderr.
    Example: led_state_for({"activity_status": "question"})
             -> {"inner": "waiting-permission", "outer": "active"}
    """
    script = Path(__file__).resolve().parent / "led_state_for.node.mjs"
    proc = subprocess.run(
        ["node", str(script)],
        input=json.dumps(row),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node could not evaluate ledStateFor: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def poll_until(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    timeout: float = ASSERT_TIMEOUT_SECONDS,
) -> tuple[bool, Any]:
    """Poll ``probe`` until ``predicate`` holds or the deadline passes.

    Description: returns the LAST observation either way, because the
      failure message has to say what was actually seen. A bare False
      would make every timeout look identical.
    Inputs: probe (callable) - reads the current observation; predicate
      (callable) - the condition; timeout (float) seconds.
    Output: (bool matched, Any last_observation).
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        last = probe()
        if predicate(last):
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(POLL_INTERVAL_SECONDS)

