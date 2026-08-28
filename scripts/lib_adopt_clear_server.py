"""The real prompt route and the real adoption write, over one database.

Extracted from scripts/verify_adopt_clears_prompt.py so that file stays
under this project's 500-line ceiling. It holds the SERVER half of that
verifier: a FastAPI TestClient over the real router, a real migrated
SQLite database in a throwaway directory, and the real persistence call
the adopt route ends in.

THE ONE THING IT FAKES IS TMUX. persist_adoption takes the listing as an
argument, so the liveness fact can be supplied from a synthetic object
while every database effect stays real. Nothing here opens a socket, and
in particular nothing here touches the user's live 'cloude' socket.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

#: The throwaway socket name every row and every query is keyed on. It is
#: deliberately NOT 'cloude': a verifier that shared a socket name with
#: the running app would be one misconfigured state dir away from writing
#: into real rows.
SOCKET = "cloude-verify-adopt-clears"
STAMP = "2026-08-24T13:32:31Z"
NAMES = ["cloude_alpha", "cloude_bravo", "cloude_charlie"]
EPOCHS = {name: 1755000000 + i for i, name in enumerate(NAMES)}


class Server:
    """The REAL prompt route and the REAL adoption write, over one db.

    Description: the harness page's window.API calls land here through
      Playwright bindings, so the browser is talking to the same code
      the app runs, not to a canned shape this file invented.
    Inputs (constructor): state_dir (Path) - a throwaway state directory.
    Output: a Server instance.
    """

    def __init__(self, state_dir: Path) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api import routes
        from src.api.auth import require_auth

        self.state_dir = state_dir
        routes.settings.__class__.get_state_dir = lambda _self: state_dir

        class _Manager:
            def tmux_socket_name(_self):
                return SOCKET

        app = FastAPI()
        app.include_router(routes.router, prefix="/api/v1")
        app.state.session_manager = _Manager()
        app.dependency_overrides[require_auth] = lambda: True
        self.client = TestClient(app)
        self._adopts = 0
        self._declines = 0

    def seed(self) -> None:
        """One unattributed snapshot and one 'observed' row per name."""
        from src.core.db import connect, db_path_for, set_meta, transaction
        from src.core.db_migration import ensure_db_migrated
        from src.core.db_models import (
            META_SESSION_IMPORT_UNATTRIBUTED,
            SESSION_ORIGIN_OBSERVED,
        )
        from src.core.session_identity import record_instance

        ensure_db_migrated(self.state_dir, 4, "0.8.2")
        with closing(connect(db_path_for(self.state_dir))) as conn:
            with transaction(conn):
                for name in NAMES:
                    record_instance(
                        conn,
                        socket=SOCKET,
                        name=name,
                        epoch=EPOCHS[name],
                        origin=SESSION_ORIGIN_OBSERVED,
                        now=STAMP,
                    )
                set_meta(
                    conn,
                    META_SESSION_IMPORT_UNATTRIBUTED,
                    json.dumps(
                        [
                            {
                                "tmux_name": name,
                                "epoch": EPOCHS[name],
                                "hints": ["its name matches the auto-generated form"],
                                "reason": "no_admissible_evidence",
                            }
                            for name in NAMES
                        ],
                        sort_keys=True,
                    ),
                )

    def counts(self) -> dict:
        """How many adopt and decline writes this server has served."""
        return {"adopts": self._adopts, "declines": self._declines}

    def prompt(self) -> dict:
        """GET /sessions/attribution-prompt, verbatim."""
        return self.client.get("/api/v1/sessions/attribution-prompt").json()

    def adopt(self, name: str) -> dict:
        """The real adoption write, against a SYNTHETIC tmux listing.

        Description: POST /sessions/adopt would need a live tmux socket,
          which this harness must never open. persist_adoption is the
          function that route's write path ends in, and it takes the
          listing as an argument, so the liveness fact can be supplied
          without a socket while every database effect stays real.
        Inputs: name (str) - the tmux session name.
        Output: dict - {'ok': bool, 'outcome': str}.
        """
        from src.core.db import connect, db_path_for, transaction
        from src.core.session_adopt_persist import persist_adoption
        from src.core.tmux_listing import TmuxListing

        listing = TmuxListing(
            ok=True,
            sessions=[
                {"name": n, "created_at_epoch": EPOCHS[n], "attached": False}
                for n in NAMES
            ],
        )
        with closing(connect(db_path_for(self.state_dir))) as conn:
            with transaction(conn):
                result = persist_adoption(
                    conn, socket=SOCKET, name=name, listing=listing, now=STAMP
                )
        self._adopts += 1
        return {"ok": bool(result.persisted), "outcome": result.outcome}

    def decline(self, names: list) -> dict:
        """POST /sessions/attribution-decline, verbatim."""
        out = self.client.post(
            "/api/v1/sessions/attribution-decline", json={"tmux_names": list(names)}
        ).json()
        self._declines += len(names)
        return out

    def origins(self) -> dict:
        """Every seeded name's CURRENT origin, read straight from SQL."""
        from src.core.db import connect, db_path_for

        with closing(connect(db_path_for(self.state_dir), create=False)) as conn:
            return {
                str(r[0]): str(r[1])
                for r in conn.execute(
                    "SELECT tmux_name, origin FROM sessions WHERE tmux_socket = ?",
                    (SOCKET,),
                ).fetchall()
            }
