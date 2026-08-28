"""The setup / upgrade wizard: one code path, two modes.

WHAT THIS REPLACES

A menu item that shelled out to scripts/config_upgrade.py and dumped its
stdout into a macOS alert saying "Some settings need your attention". The
user's reaction was that it told him nothing, and he was right: it named
fields and printed values with no statement of what any of them are, what
happens if he keeps his, or what happens if he takes the new default.

THE TWO MODES ARE ONE PAGE

    First-run setup   - setup is not finished. There is no credential to
                        authenticate with yet, so the wizard answers without
                        one. The server is pinned to loopback for exactly this
                        window; see src/core/setup_state.py.
    Upgrade review    - setup is finished. The same page, the same endpoints,
                        now behind the same TOTP session every other API route
                        requires.

Deliberately not two pages. A separate "first run" page is a second code path
that has to be independently remembered, re-secured, and deleted, and the
industry's supply of forgotten install.php files is the argument against it.
Here the mode is a property of measured state, so it flips on its own.

HOW THE AUTH GATE WORKS, AND WHAT IT IS NOT

``guard_wizard_access`` requires a valid access token when, and only when,
setup is complete. It is not a second auth system: on the protected side it
delegates to the very same ``require_auth`` the rest of the API uses, so a
change to token handling cannot leave this route behind on an old rule.

The HTML shell at ``/setup`` is served unconditionally and contains no state
at all - every fact on the page arrives from the guarded JSON endpoints below,
so an unauthenticated fetch of the shell after setup reveals nothing beyond
the fact that this software is running, which the login page already reveals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from src.api.auth import require_auth
from src.config import settings
from src.core.config_defaults import (
    effective_shipped_defaults,
    supported_keys as model_supported_keys,
)
from src.core.config_merge import (
    CANNOT_DETERMINE,
    CONFLICT,
    REMOVED_UPSTREAM,
    load_json,
    merge_config,
)
from src.core.setup_state import (
    current_bind_report,
    current_exposure,
    current_setup_state,
    mark_setup_complete,
)

logger = structlog.get_logger()

#: JSON endpoints. Mounted under /api/v1 in src/main.py.
router = APIRouter(tags=["setup"])

#: The HTML shell, mounted at the app root so the menu bar can open a plain
#: /setup URL in the user's default browser.
page_router = APIRouter(tags=["setup"])

#: Optional bearer, so the guard can tell "no credential offered" apart from
#: "bad credential" without require_auth's own 401 firing first.
_optional_bearer = HTTPBearer(auto_error=False)

#: Where config.example.json lives, relative to this file.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Plain-language explanation for each merge outcome the wizard shows. Keys are
#: the outcome constants from src/core/config_merge.py. Every entry says what
#: happens on each choice, because "needs your attention" without that is the
#: complaint this whole page exists to answer.
OUTCOME_GUIDANCE: dict[str, dict[str, str]] = {
    CONFLICT: {
        "headline": "You changed this, and the shipped default also changed",
        "what_it_means": (
            "Both your value and the default moved since your last upgrade, "
            "so there is no way to combine them without picking a winner. "
            "Nothing has been merged."
        ),
        "keep_yours": "Your setting stays exactly as it is now.",
        "take_new": (
            "Your customisation is replaced by the new shipped default and "
            "cannot be recovered from this screen. A timestamped backup of "
            "the whole file is written before anything is applied."
        ),
    },
    CANNOT_DETERMINE: {
        "headline": "Cannot determine whether you changed this or the default did",
        "what_it_means": (
            "Your value differs from the shipped default, and there is no "
            "record of what the default used to be, so the two cases are "
            "indistinguishable. This is not a warning about your setting - "
            "it is this screen refusing to guess. Applying anything records "
            "the missing record, and this stops appearing."
        ),
        "keep_yours": "Your setting stays exactly as it is now.",
        "take_new": "Your value is replaced by the shipped default.",
    },
    REMOVED_UPSTREAM: {
        "headline": "This setting is no longer read by the current version",
        "what_it_means": (
            "The configuration loader does not read this key any more. It is "
            "being kept in your file rather than deleted on your behalf, "
            "because a merge that deletes settings is not a merge."
        ),
        "keep_yours": "The key stays in your file. It has no effect.",
        "take_new": "The key is removed from your file.",
    },
}


class SetupDecision(BaseModel):
    """One per-item choice from the wizard.

    Attributes:
        path: Dotted config path the decision applies to.
        choice: "keep" to hold the user's value, "take_new" to adopt the
            shipped default.
    """

    path: str = Field(min_length=1)
    choice: str = Field(pattern="^(keep|take_new)$")


class ApplyRequest(BaseModel):
    """The body of POST /api/v1/setup/apply.

    Attributes:
        decisions: Per-item choices. Anything not listed keeps the user's
            value, because silence is not consent to overwrite.
    """

    decisions: list[SetupDecision] = []


async def guard_wizard_access(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> bool:
    """Allow the wizard through only under a state this instance can defend.

    The rule in one sentence: authentication is required exactly when setup is
    complete. Before that there is no credential in existence to present, so
    demanding one would make first-run setup impossible; after it, the wizard
    can rewrite configuration and must be behind the same session as
    everything else that can.

    This is safe to leave open during setup only because the server is not
    reachable off-host during setup. That is not a separate promise made
    elsewhere and hoped for - ``resolve_exposure`` decides both facts together
    and raises rather than produce an open wizard on a reachable address.

    Args:
        request: The incoming request, used only for logging.
        credentials: Bearer credentials, when offered.

    Returns:
        True when access is permitted.

    Raises:
        HTTPException: 401 when setup is complete and no valid access token
            was presented.
    """
    exposure = current_exposure()
    if not exposure.wizard_requires_auth:
        logger.info(
            "setup_wizard_open_access",
            path=request.url.path,
            bind_host=exposure.bind_host,
        )
        return True

    # Setup is complete. Delegate to the SAME dependency the rest of the API
    # uses rather than re-implementing token checking here.
    return await require_auth(credentials)


def _config_path() -> Path:
    """Resolve the live config.json path.

    Returns:
        The expanded path from settings.
    """
    return Path(settings.auth_config_file).expanduser()


def _state_dir() -> Path:
    """Resolve the durable state directory holding the recorded merge base.

    Returns:
        The state directory path.
    """
    return Path(settings.get_state_dir())


def _compute_plan() -> dict[str, Any]:
    """Build the merge plan the wizard renders.

    Uses the same inputs and the same rules as scripts/config_upgrade.py -
    including the model-derived shipped defaults, so a stale
    config.example.json cannot make a live setting look deleted here either.

    Returns:
        A mapping with ``had_base``, ``items`` (one per field needing a human
        decision, each carrying its guidance), and ``adopting`` (defaults being
        taken automatically because the user never touched them).
    """
    config_path = _config_path()
    try:
        mine = load_json(config_path) or {}
    except json.JSONDecodeError as exc:
        # THREE-OUTCOME RULE: an unparseable config is not "no changes needed".
        return {
            "had_base": False,
            "unreadable": (
                f"{config_path} is not valid JSON ({exc}), so no merge can be "
                "computed. Fix or move the file; nothing here has touched it."
            ),
            "items": [],
            "adopting": [],
        }

    example = load_json(_REPO_ROOT / "config.example.json") or {}
    base_path = _state_dir() / "config-base.json"
    try:
        base = load_json(base_path)
    except json.JSONDecodeError:
        base = None

    effective = effective_shipped_defaults(example, mine)
    stale_roots = frozenset(k for k in effective if k not in example)
    result = merge_config(
        mine,
        effective,
        base,
        supported_keys=model_supported_keys(),
        stale_roots=stale_roots,
    )

    items = []
    for decision in result.needing_attention():
        guidance = OUTCOME_GUIDANCE.get(decision.outcome, {})
        items.append(
            {
                "path": decision.path,
                "outcome": decision.outcome,
                "headline": guidance.get("headline", decision.outcome),
                "what_it_means": guidance.get("what_it_means", decision.note),
                "yours": decision.mine,
                "shipped_default": decision.theirs,
                "if_you_keep_yours": guidance.get("keep_yours", ""),
                "if_you_take_the_new_default": guidance.get("take_new", ""),
                "can_take_new_default": decision.outcome != REMOVED_UPSTREAM,
            }
        )

    adopting = [
        {"path": d.path, "shipped_default": d.theirs, "note": d.note}
        for d in result.changes()
    ]

    return {
        "had_base": result.had_base,
        "unreadable": None,
        "items": items,
        "adopting": adopting,
    }


@router.get("/setup/state")
async def setup_state(_: bool = Depends(guard_wizard_access)) -> dict[str, Any]:
    """Everything the wizard needs to render, in one call.

    Returns:
        The mode, the setup checks, the exposure in force, and the merge plan.
    """
    state = current_setup_state()
    return {
        "mode": "first_run" if not state.is_complete else "upgrade_review",
        "setup": {
            "status": state.status,
            "checks": [
                {
                    "key": c.key,
                    "title": c.title,
                    # None stays None across the wire. A JSON `false` here
                    # would turn "could not evaluate" into a definite failure.
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in state.checks
            ],
        },
        # Same rule as GET /health: the effective bind is the startup record
        # or it is unknown. Re-resolving the exposure here would tell the
        # wizard the user is reachable on an address nothing is listening on.
        "exposure": current_bind_report(),
        "plan": _compute_plan(),
    }


@router.post("/setup/apply")
async def setup_apply(
    body: ApplyRequest,
    _: bool = Depends(guard_wizard_access),
) -> dict[str, Any]:
    """Apply the user's per-item decisions to config.json.

    Only paths explicitly marked ``take_new`` are changed. Everything else
    keeps the user's value, including anything he did not mention: a blanket
    accept is exactly the behaviour the old dialog was criticised for, and an
    omitted item is not a decision.

    Args:
        body: The decisions.

    Returns:
        What was changed and where the backup went.

    Raises:
        HTTPException: 409 when config.json cannot be parsed, 500 when the
            backup or write fails. Never a partial write reported as success.
    """
    config_path = _config_path()
    try:
        mine = load_json(config_path) or {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"config.json is not valid JSON ({exc}); refusing to merge.",
        )

    take_new = {d.path for d in body.decisions if d.choice == "take_new"}
    if not take_new:
        return {"changed": [], "backup": None, "note": "No changes requested."}

    example = load_json(_REPO_ROOT / "config.example.json") or {}
    effective = effective_shipped_defaults(example, mine)

    changed: list[str] = []
    updated = json.loads(json.dumps(mine))
    for path in sorted(take_new):
        parts = path.split(".")
        source: Any = effective
        for part in parts:
            if not isinstance(source, dict) or part not in source:
                source = None
                break
            source = source[part]
        if source is None:
            # The shipped default for this path could not be resolved. Skip it
            # and say so rather than writing a null over a real setting.
            continue

        cursor = updated
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = source
        changed.append(path)

    from scripts.config_upgrade import back_up_config  # noqa: PLC0415

    state_dir = _state_dir()
    try:
        backup_target, outcome = back_up_config(config_path, state_dir)
        config_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"apply failed: {exc}")

    # Record the effective defaults as the new base, so the fields just
    # resolved do not report CANNOT DETERMINE forever.
    try:
        (state_dir / "config-base.json").write_text(
            json.dumps(effective, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("setup_base_record_failed", error=str(exc))

    logger.info("setup_apply", changed=changed)
    return {
        "changed": changed,
        "backup": f"{outcome} {backup_target}",
        "note": (
            "Everything you did not explicitly change was left alone."
        ),
    }


@router.post("/setup/finish")
async def setup_finish(_: bool = Depends(guard_wizard_access)) -> dict[str, Any]:
    """Mark setup complete and report what that changes.

    Writing the sentinel flips the wizard to its authenticated mode
    immediately, but it does NOT move the listening socket: uvicorn binds once
    at startup and has no in-place rebind, so the configured address only takes
    effect on the next start. This endpoint says so explicitly rather than
    leaving the user believing he is reachable on an address he is not.

    Returns:
        The new setup status and whether a restart is still needed.

    Raises:
        HTTPException: 409 when setup is not actually finishable yet, listing
            what is outstanding. 500 when the sentinel cannot be written.
    """
    before = current_setup_state()
    blocking = [
        c for c in before.outstanding() if c.key in ("totp_secret", "jwt_secret")
    ]
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                "Setup cannot be completed yet: "
                + "; ".join(f"{c.title} - {c.detail}" for c in blocking)
            ),
        )

    config_path = _config_path()
    try:
        mark_setup_complete(config_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"could not write the setup sentinel ({exc}); the instance "
                "stays on loopback until this succeeds."
            ),
        )

    after = current_setup_state()
    exposure = current_exposure()
    restart_needed = settings.host != exposure.bind_host or settings.host != "127.0.0.1"

    logger.info("setup_finished", status=after.status, configured_host=settings.host)
    return {
        "status": after.status,
        "configured_host": settings.host,
        "currently_bound_host": "127.0.0.1",
        "restart_required": restart_needed,
        "message": (
            "Setup is complete. This server is still listening on 127.0.0.1, "
            f"because it bound that address at startup. Restart it to listen "
            f"on {settings.host}."
            if restart_needed
            else "Setup is complete. No restart is needed; 127.0.0.1 is the "
            "address you configured."
        ),
    }


@page_router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page() -> HTMLResponse:
    """Serve the wizard shell.

    Unconditionally served and deliberately stateless: every fact on the page
    comes from the guarded endpoints above, so this response reveals nothing
    an unauthenticated caller could not already learn by finding the login
    screen.

    Returns:
        The wizard HTML.
    """
    html = (_REPO_ROOT / "client" / "setup.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, must-revalidate"},
    )
