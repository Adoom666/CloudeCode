"""Three-outcome tests for the CONSUMERS of a tmux listing.

Split out of ``tests/test_tmux_listing.py`` (the 500-line file rule); that
file proves the listing TYPE reports the third outcome honestly, this one
proves the two places that act on it actually honour it.

Both matter separately, and the second is where the damage was. A type can
report ``ok=False`` perfectly while the reconciler ignores the field and
prunes anyway, or while the route flattens it back into an empty 200. Each
of those is the original bug wearing the new type.

Run with:
    ./venv/bin/python3 -m pytest tests/test_tmux_listing_consumers.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tlc_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tlc_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import SessionManager
from src.core.tmux_listing import (
    REASON_NO_SERVER,
    REASON_PROBE_ERROR,
    REASON_TIMEOUT,
    REASON_TMUX_MISSING,
    TmuxListing,
)


# =========================================================================== #
# 6. THE LIVE BUG: a failed probe must not prune ownership                    #
# =========================================================================== #


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``."""

    def __init__(self, root: Path, port: int = 5001):
        self._root = root
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._root / "unread_state.json"

    def get_session_metadata_path(self) -> Path:
        return self._root / "session_metadata.json"

    def get_log_directory(self) -> Path:
        return self._root / "logs"

    @property
    def log_directory(self) -> str:
        return str(self._root / "logs")

    @property
    def default_working_dir(self) -> str:
        return str(self._root)

    @property
    def log_buffer_size(self) -> int:
        return 100


class _ProbeBackend:
    """A backend whose ``discover_existing`` returns a canned listing.

    Inputs (constructor):
        listing (TmuxListing): exactly what the probe will report.
    """

    def __init__(self, listing: TmuxListing):
        self._listing = listing
        self.tmux_session = None

    def discover_existing(self) -> TmuxListing:
        return self._listing

    def list_pane_status_all(self) -> TmuxListing:
        return TmuxListing.answered([])


def _manager(monkeypatch, tmp_path: Path) -> SessionManager:
    """Build a SessionManager against a throwaway state directory.

    Inputs:
        monkeypatch: pytest fixture, used to swap module-level settings.
        tmp_path (Path): per-test scratch directory.

    Output:
        SessionManager: no sessions registered, no metadata on disk.
    """
    stub = _StubSettings(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


@pytest.mark.parametrize(
    "listing",
    [
        TmuxListing.unavailable(REASON_TMUX_MISSING, detail="tmux not found on PATH"),
        TmuxListing.unavailable(REASON_TIMEOUT),
        TmuxListing.unavailable("exit_1", detail="lost server"),
        TmuxListing.unavailable(REASON_PROBE_ERROR),
    ],
    ids=["tmux_missing", "timeout", "exit_1", "probe_error"],
)
@pytest.mark.asyncio
async def test_failed_probe_does_not_prune_owned_sessions(monkeypatch, tmp_path, listing):
    """THE LIVE BUG. A probe that could not run must change no ownership.

    Under the old API this method received ``[]`` from a failed probe,
    computed ``stale = owned - set()`` (i.e. ALL of them), and deleted every
    ownership record the user had - silently, on a single transient tmux
    failure at startup. Nine real sessions were exposed to this.

    The assertion is deliberately on the SET ITSELF rather than on a log
    line or a return value: the damage was to persisted state, so that is
    what has to be proven intact.
    """
    mgr = _manager(monkeypatch, tmp_path)
    owned = {"cloude_Test", "cloude_asd", "cloude_fs2", "cloude_ses_ec5bf2a3"}
    mgr.owned_tmux_sessions = set(owned)
    mgr.pinned_themes = {"cloude_Test": "matrix"}

    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(listing),
    )
    await mgr._lifespan_tmux_reconcile()

    assert mgr.owned_tmux_sessions == owned, (
        "a probe that could not evaluate pruned ownership records; an "
        "unanswered question was treated as an answer of zero"
    )
    assert mgr.pinned_themes == {"cloude_Test": "matrix"}, (
        "pinned themes were pruned against an unavailable listing too"
    )


@pytest.mark.asyncio
async def test_successful_empty_probe_still_prunes(monkeypatch, tmp_path):
    """The other half. A MEASURED zero must keep pruning as it always did.

    Without this, "never prune" would pass the test above while quietly
    breaking the reconciler - collapsing the third outcome into pass is the
    same defect as collapsing it into fail.
    """
    mgr = _manager(monkeypatch, tmp_path)
    mgr.owned_tmux_sessions = {"cloude_gone"}
    mgr.pinned_themes = {"cloude_gone": "matrix"}

    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(
            TmuxListing.answered([], reason=REASON_NO_SERVER)
        ),
    )
    await mgr._lifespan_tmux_reconcile()

    assert mgr.owned_tmux_sessions == set(), (
        "tmux answered 'no server running', which is a real zero - the "
        "stale ownership record should have been pruned"
    )
    assert mgr.pinned_themes == {}


@pytest.mark.asyncio
async def test_live_sessions_survive_a_failed_probe_and_prune_when_gone(
    monkeypatch, tmp_path
):
    """A partial listing prunes only what it actually contradicts."""
    mgr = _manager(monkeypatch, tmp_path)
    mgr.owned_tmux_sessions = {"cloude_alive", "cloude_gone"}

    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(TmuxListing.answered(["cloude_alive"])),
    )
    await mgr._lifespan_tmux_reconcile()
    assert mgr.owned_tmux_sessions == {"cloude_alive"}


# =========================================================================== #
# 7. The wire: GET /sessions/attachable must not answer 200 [] on a failure    #
# =========================================================================== #


class _RouteManager:
    """Minimal SessionManager surface the attachable route reads.

    Inputs (constructor):
        listing (TmuxListing): exactly what
            ``list_attachable_sessions()`` will return.
    """

    def __init__(self, listing: TmuxListing):
        self._listing = listing
        self.backends: dict = {}
        self.owned_tmux_sessions: set = set()

    def list_attachable_sessions(self) -> TmuxListing:
        return self._listing

    def active_tmux_names(self) -> set:
        return set()


def _attachable_client(listing: TmuxListing):
    """Build a TestClient mounting only the sessions router, auth bypassed.

    Inputs:
        listing (TmuxListing): what the stub manager will report.

    Output:
        TestClient: ready for ``get("/api/v1/sessions/attachable")``.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.auth import require_auth
    from src.api.routes import router as sessions_router

    app = FastAPI()
    app.include_router(sessions_router, prefix="/api/v1")
    app.state.session_manager = _RouteManager(listing)
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def test_attachable_route_returns_503_when_the_listing_is_unavailable():
    """A failed probe must NOT come back as an empty 200.

    An empty 200 is a claim - "your machine has no adoptable sessions" -
    that nothing measured, and no client can tell it apart from the real
    thing. The 503 carries the reason so the UI can say what it could not
    determine instead of leaving a blank cell.
    """
    client = _attachable_client(
        TmuxListing.unavailable(REASON_TIMEOUT, detail="tmux did not answer within 5.0s")
    )
    resp = client.get("/api/v1/sessions/attachable")
    assert resp.status_code == 503, (
        f"a probe that could not evaluate answered {resp.status_code} "
        f"with body {resp.text!r} - an empty list is an ANSWER"
    )
    detail = resp.json()["detail"]
    assert detail["listing_ok"] is False
    assert detail["listing_reason"] == REASON_TIMEOUT
    assert detail["listing_detail"] == "tmux did not answer within 5.0s"


def test_attachable_route_returns_200_empty_for_a_measured_zero():
    """The other half: a real zero stays a plain, quiet, empty 200."""
    client = _attachable_client(
        TmuxListing.answered([], reason=REASON_NO_SERVER)
    )
    resp = client.get("/api/v1/sessions/attachable")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_attachable_route_rows_carry_their_listing_provenance():
    """Each row states which kind of listing produced it."""
    client = _attachable_client(TmuxListing.answered([
        {
            "name": "cloude_alpha",
            "created_by_cloude": True,
            "created_at_epoch": 1700000000,
            "window_count": 1,
        },
    ]))
    resp = client.get("/api/v1/sessions/attachable")
    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["listing_ok"] is True
    assert row["listing_reason"] is None
