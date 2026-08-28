#!/usr/bin/env python3
"""Live end-to-end verification of the TMUX/EXTERNAL ownership badge.

Runs the REAL FastAPI app in-process against a DEDICATED tmux socket
(``cloudeverify``, pinned in-process below) so it cannot see or touch any
session on the app's normal ``cloude`` socket. No port is bound.

WHAT CHANGED AT S7, AND WHY THE OLD ASSERTION WAS RETIRED RATHER THAN
RELAXED. Until S7 an adoption was recorded NOWHERE - the name was
deliberately kept out of ``owned_tmux_sessions``, so an adopted session
was permanently EXTERNAL and this script asserted exactly that. S7 makes
adoption durable in ``sessions.origin``, and the product decision it
implements is that ADOPTING A SESSION MAKES IT OURS, PERMANENTLY. So the
five "adopted session badges external" checks below are now
"adopted session badges OURS". That is a change of TRUTH, not a
weakening of the test: every one of them still asserts an exact value,
and the script now proves MORE than it did, because the pre-adopt
external state is asserted separately from the post-adopt owned state.
The one thing that must never regress - a badge that FLIPS when the user
merely opens, closes or reloads a session - is still checked at every
step, and is now checked in both directions.

What it proves, in order:
  1. a session CREATED through POST /sessions badges owned;
  2. a session made directly in tmux badges EXTERNAL before adoption;
  3. ADOPTING it flips it to OURS, once, at the moment of the claim;
  4. opening and closing either one does not flip its badge;
  5. RESTARTING the server (a full lifespan shutdown + startup, which
     rebuilds SessionManager from disk and re-attaches through the adopt
     path) leaves both badges unchanged. That is the whole point: the
     restart is what re-mints ``adopted:`` ids for sessions we still own,
     and after S7 it is also what proves the adoption is on DISK rather
     than in a set that dies with the process.

Usage:
    venv/bin/python3 scripts/verify_session_ownership_badge.py

Exits 0 and prints ALL PASS on success; 1 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

_WORKDIR = tempfile.mkdtemp(prefix="cc_verify_wd_")
_LOGDIR = tempfile.mkdtemp(prefix="cc_verify_logs_")
_STATEDIR = tempfile.mkdtemp(prefix="cc_verify_state_")
os.environ["DEFAULT_WORKING_DIR"] = _WORKDIR
os.environ["LOG_DIRECTORY"] = _LOGDIR
# feat/state-directory - without this, importing src.main below (which
# runs Settings.get_state_dir() at module load) would create and write
# into the real ~/Library/Application Support/CloudeCode on the machine
# running this script, instead of a throwaway directory.
os.environ["CLOUDE_STATE_DIR"] = _STATEDIR
os.environ.setdefault("TOTP_SECRET", "verifysecretnotreal")
os.environ.setdefault("JWT_SECRET", "verifyjwtnotreal")

# ruff: noqa: E402
import httpx

from src.api.auth import require_auth
from src.main import app

SOCKET = "cloudeverify"
EXTERNAL_NAME = "verifyext"
CREATED_PROJECT = "verifyown"

app.dependency_overrides[require_auth] = lambda: True

# Pin EVERY TmuxBackend this process builds to the verification socket.
# config.json's ``session.tmux_socket_name`` is not enough on its own:
# ``src.core.session_backend.build_backend`` never threads it through, so
# the create + probe paths would fall back to the shared default socket
# and this script would operate on real sessions. Belt and braces.
from src.core import tmux_backend as _tmux_backend  # noqa: E402

_ORIGINAL_TMUX_INIT = _tmux_backend.TmuxBackend.__init__


def _pinned_socket_init(self, *args, **kwargs) -> None:
    """TmuxBackend.__init__ wrapper that forces the verification socket."""
    _ORIGINAL_TMUX_INIT(self, *args, **kwargs)
    self.socket_name = SOCKET


_tmux_backend.TmuxBackend.__init__ = _pinned_socket_init

_failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    """Record one comparison as pass or fail.

    Args:
        label: human-readable description of what was checked.
        actual: observed value.
        expected: required value.
    """
    if actual == expected:
        print(f"ok   - {label} (= {actual!r})")
    else:
        _failures.append(label)
        print(f"FAIL - {label}: expected {expected!r}, got {actual!r}")


def tmux(*args: str, check_rc: bool = False) -> subprocess.CompletedProcess:
    """Run tmux against the verification socket only.

    Args:
        *args: tmux arguments after ``-L <socket>``.
        check_rc: raise on non-zero exit when True.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args],
        capture_output=True,
        text=True,
        check=check_rc,
    )


async def badges(client: httpx.AsyncClient) -> dict[str, bool]:
    """Merge /sessions/list and /sessions/attachable the way the client does.

    Args:
        client: an ASGI-bound httpx client for the app.

    Returns:
        Mapping of tmux session name -> created_by_cloude.
    """
    out: dict[str, bool] = {}
    resp = await client.get("/sessions/attachable")
    resp.raise_for_status()
    for row in resp.json():
        out[row["name"]] = row["created_by_cloude"]
    resp = await client.get("/sessions/list")
    resp.raise_for_status()
    for info in resp.json():
        name = info.get("tmux_session")
        if name:
            out[name] = info["created_by_cloude"]
    return out


async def ids(client: httpx.AsyncClient) -> dict[str, str]:
    """Map each live tmux session name to its current session id."""
    resp = await client.get("/sessions/list")
    resp.raise_for_status()
    return {
        i["tmux_session"]: i["session"]["id"]
        for i in resp.json()
        if i.get("tmux_session")
    }


def make_client() -> httpx.AsyncClient:
    """Build an httpx client bound to the app over ASGI (no port bound)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://verify.local/api/v1",
    )


async def phase_one() -> tuple[str, dict[str, bool]]:
    """First server run: create, adopt, open, close.

    Returns:
        (created tmux session name, badge map at end of the run).
    """
    async with app.router.lifespan_context(app):
        async with make_client() as client:
            resp = await client.post(
                "/sessions",
                json={
                    "working_dir": _WORKDIR,
                    "auto_start_claude": False,
                    "project_name": CREATED_PROJECT,
                },
            )
            resp.raise_for_status()
            created_name = resp.json()["tmux_session"]
            print(f"created via POST /sessions: {created_name}")

            tmux("new-session", "-d", "-s", EXTERNAL_NAME)
            print(f"created directly in tmux: {EXTERNAL_NAME}")

            b = await badges(client)
            check("created session badges owned (before adopt)",
                  b.get(created_name), True)
            check("external session badges external (before adopt)",
                  b.get(EXTERNAL_NAME), False)

            resp = await client.post(
                "/sessions/adopt",
                json={"session_name": EXTERNAL_NAME, "confirm_detach": False},
            )
            resp.raise_for_status()

            b = await badges(client)
            check("created session still owned (external now OPEN)",
                  b.get(created_name), True)
            # S7: the claim is the moment ownership changes, and it
            # changes exactly once. Before the POST above this same
            # session asserted False; it must now be True and stay True
            # through every open, close and restart below.
            check("adopted session badges OURS once ADOPTED",
                  b.get(EXTERNAL_NAME), True)

            live_ids = await ids(client)
            print(f"ids while open: {live_ids}")

            # CLOSE both (detach = the app's close; tmux keeps running).
            for sid in live_ids.values():
                await client.post("/sessions/detach", params={"session_id": sid})
            b = await badges(client)
            check("created session still owned once CLOSED",
                  b.get(created_name), True)
            check("adopted session still OURS once CLOSED",
                  b.get(EXTERNAL_NAME), True)

            # RE-OPEN both through the adopt path (what the UI does).
            for name in (created_name, EXTERNAL_NAME):
                resp = await client.post(
                    "/sessions/adopt",
                    json={"session_name": name, "confirm_detach": False},
                )
                resp.raise_for_status()
            b = await badges(client)
            check("created session still owned once RE-OPENED",
                  b.get(created_name), True)
            check("adopted session still OURS once RE-OPENED",
                  b.get(EXTERNAL_NAME), True)

            return created_name, b


async def phase_two(created_name: str, before: dict[str, bool]) -> None:
    """Second server run: the RESTART. Badges must be identical.

    After S7 this is the load-bearing phase: the adopted badge is only
    True here because the claim was written to the datastore. A restart
    rebuilds SessionManager from disk and the legacy in-memory owned set
    starts empty, so an adoption that lived only in memory would read
    External here and fail.

    Args:
        created_name: tmux name of the session made via POST /sessions.
        before: badge map captured at the end of the first run.
    """
    async with app.router.lifespan_context(app):
        async with make_client() as client:
            # A restart re-attaches only the persisted session; the UI
            # re-opens the rest through adopt, which is what mints the
            # `adopted:` ids that broke the old derivation.
            for name in (created_name, EXTERNAL_NAME):
                try:
                    await client.post(
                        "/sessions/adopt",
                        json={"session_name": name, "confirm_detach": False},
                    )
                except Exception:  # already live after rehydrate
                    pass

            live_ids = await ids(client)
            print(f"ids after restart: {live_ids}")
            check("restart re-minted an adopted: id for the OWNED session",
                  str(live_ids.get(created_name, "")).startswith("adopted:"),
                  True)

            after = await badges(client)
            check("created session STILL owned after restart",
                  after.get(created_name), before.get(created_name))
            check("adopted session STILL ours after restart",
                  after.get(EXTERNAL_NAME), before.get(EXTERNAL_NAME))
            check("owned badge value after restart", after.get(created_name), True)
            # THE POINT OF S7. An in-memory set cannot survive this. The
            # only way this reads True after a full lifespan restart is
            # if origin='adopted' is a row on disk keyed on the tmux
            # instance triple.
            check("adopted badge value after restart",
                  after.get(EXTERNAL_NAME), True)


async def main() -> int:
    """Run both phases, then tear the verification socket down."""
    try:
        created_name, before = await phase_one()
        await phase_two(created_name, before)
    finally:
        tmux("kill-server")
        print(f"killed tmux socket {SOCKET}")

    print(f"\n{len(_failures)} failed")
    if _failures:
        for f in _failures:
            print(f"  FAILED: {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
