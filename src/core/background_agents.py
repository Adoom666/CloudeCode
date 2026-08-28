"""Background Claude sessions - the ones CloudeCode cannot otherwise see.

WHY THIS EXISTS. `/fork` inside a session creates a BACKGROUND AGENT: a
real Claude session, with its own conversation and its own transcript,
that runs without a tmux session of its own. Measured 2026-08-28 - a
`/fork` produced `kind: "background"`, `name: "Base"`, `status: "idle"`,
and CloudeCode recorded it as a lineage row with `tmux_created_epoch`
NULL, which by our own listing predicate means NOT LISTED.

That was defensible and still wrong in effect: a user who forks from
inside a session has created work the GUI will never show them. Claude's
own `/resume` picker marks these `bg`; we had no equivalent at all.

WHERE THE DATA COMES FROM, and why it is not the out-of-band channel this
project deliberately restricts. `claude agents --json` is a READ. It
starts no conversation, resumes nothing, and writes no transcript record
- unlike `claude -p --resume ... "/rename"`, which appends to a shared
conversation and is therefore reserved for a write that cannot be made
any other way. Listing is a read, so it takes the read-only path.

THREE OUTCOMES. A subprocess that times out, is missing, or returns
something unparseable is NOT "there are no background agents". An empty
list and a failed query are different facts and this module keeps them
apart, because rendering "none" for "could not ask" is precisely how a
user concludes nothing is running while an agent burns tokens.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

#: The listing spawns a process, so it is capped. Measured on the
#: reference box the call returns in well under a second; 10s is slack
#: for a loaded machine, not an expectation.
QUERY_TIMEOUT_SECONDS: int = 10

QUERY_OK = "ok"
QUERY_UNAVAILABLE = "unavailable"
QUERY_UNPARSEABLE = "unparseable"

#: The value of `kind` that marks a session with no terminal of its own.
#: Interactive sessions are the tmux ones CloudeCode already lists; only
#: these are invisible to it.
KIND_BACKGROUND = "background"


@dataclass(frozen=True)
class BackgroundAgent:
    """One background Claude session, as the CLI reports it."""

    session_id: str
    name: Optional[str]
    cwd: Optional[str]
    status: Optional[str]
    state: Optional[str]
    pid: Optional[int]
    started_at_ms: Optional[int]

    @property
    def short_id(self) -> str:
        return self.session_id[:8] if self.session_id else ""


@dataclass(frozen=True)
class AgentsResult:
    """The listing, and whether it could be made at all."""

    agents: List[BackgroundAgent] = field(default_factory=list)
    status: str = QUERY_OK
    detail: Optional[str] = None

    @property
    def measured(self) -> bool:
        """True only when the list describes reality.

        A caller MUST branch on this rather than on ``not agents``: an
        empty list and a failed query are different facts, and rendering
        the second as "no background sessions" tells the user something
        nobody established.
        """
        return self.status == QUERY_OK


def _parse_agent(raw: Dict[str, Any]) -> Optional[BackgroundAgent]:
    """Build one BackgroundAgent from a CLI record, defensively.

    Description: every field is read with ``.get``. The CLI is another
      program on its own release schedule, and a listing that throws
      because one record gained or lost a key would take the whole
      feature down for a cosmetic change.
    Inputs: raw (dict) - one element of the JSON array.
    Output: BackgroundAgent | None - None when there is no session id,
      which is the only field this cannot do without.
    """
    session_id = raw.get("sessionId") or raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    pid = raw.get("pid")
    started = raw.get("startedAt")
    return BackgroundAgent(
        session_id=session_id,
        name=raw.get("name"),
        cwd=raw.get("cwd"),
        status=raw.get("status"),
        state=raw.get("state"),
        pid=pid if isinstance(pid, int) else None,
        started_at_ms=started if isinstance(started, int) else None,
    )


def list_background_agents(
    *, cwd: Optional[str] = None, claude_path: Optional[str] = None
) -> AgentsResult:
    """List Claude background sessions. Never raises.

    Description: runs ``claude agents --json`` and keeps only records
      whose ``kind`` is ``background`` - interactive ones are the tmux
      sessions CloudeCode already shows, and listing them twice would be
      worse than not listing them at all.

      Returns a NAMED failure rather than an empty list whenever the
      question could not be answered. See ``AgentsResult.measured``.
    Inputs: cwd (str | None) - restrict to sessions started under this
      path. claude_path (str | None) - resolved binary; looked up when
      omitted.
    Output: AgentsResult.
    Example: list_background_agents(cwd='/x').measured -> True
    """
    if not claude_path:
        try:
            from src.core.server_status import collect_claude_cli

            info = collect_claude_cli() or {}
            claude_path = info.get("path")
        except Exception as exc:  # noqa: BLE001 - see docstring
            return AgentsResult(
                status=QUERY_UNAVAILABLE, detail=f"claude cli lookup failed: {exc}"
            )
    if not claude_path:
        return AgentsResult(
            status=QUERY_UNAVAILABLE, detail="claude cli not found on any known path"
        )

    argv = [claude_path, "agents", "--json"]
    if cwd:
        argv += ["--cwd", cwd]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return AgentsResult(
            status=QUERY_UNAVAILABLE,
            detail=f"claude agents did not answer within {QUERY_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return AgentsResult(status=QUERY_UNAVAILABLE, detail=str(exc))

    if proc.returncode != 0:
        return AgentsResult(
            status=QUERY_UNAVAILABLE,
            detail=f"claude agents exited {proc.returncode}",
        )
    try:
        parsed = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        return AgentsResult(status=QUERY_UNPARSEABLE, detail=str(exc))
    if not isinstance(parsed, list):
        return AgentsResult(
            status=QUERY_UNPARSEABLE, detail="expected a JSON array"
        )

    agents = []
    for raw in parsed:
        if not isinstance(raw, dict):
            continue
        if raw.get("kind") != KIND_BACKGROUND:
            continue
        agent = _parse_agent(raw)
        if agent is not None:
            agents.append(agent)
    return AgentsResult(agents=agents, status=QUERY_OK)
