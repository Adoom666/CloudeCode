"""v0.7.0 Part 3 / feat/hook-driven-status - Claude Code lifecycle hook
settings management.

This module owns the idempotent merge of cloudecode's hook block into
``~/.claude/settings.json``. Called once at FastAPI startup. The hooks
themselves are tiny shell one-liners that POST the event payload (read
from the hook's stdin) to cloudecode's loopback ``/api/v1/hooks/claude-event``
endpoint, carrying the per-session env vars
(``CLOUDECODE_SESSION_ID`` / ``CLOUDECODE_HOOK_TOKEN`` / ``CLOUDECODE_HOOK_URL``)
that were injected into the spawned ``claude`` process's environment via
``TmuxBackend.start(env=...)``.

Security model:
    The hook subprocess can't carry a JWT - there's no place for the user
    to authenticate the hook. Instead the hook proves identity via the
    HMAC-bearer token that ONLY the cloudecode process and the spawned
    agent share (env-injected at tmux session birth). The route ALSO
    requires loopback (127.0.0.1) - defense in depth.

Idempotent merge:
    Each managed hook command embeds the literal marker
    ``# cloudecode-managed`` so on subsequent runs we can identify our
    own hooks vs. anything the user (or another tool) added. We replace
    every managed hook in place; non-managed hooks are left untouched.

Safety:
    - Existing settings file is parsed; if parse fails we LOG and BAIL
      (no clobber of unparseable user config).
    - Write is atomic: write-to-tmp + rename.
    - Opt-out via ``settings.notifications.disable_claude_hooks``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog

from src.core.test_write_guard import assert_test_write_allowed

logger = structlog.get_logger()


# Literal marker embedded into every managed hook's command string. Used
# to identify (and replace) cloudecode's own hooks on re-run without
# touching anything the user added by hand or via another tool.
CLOUDECODE_HOOKS_MARKER = "# cloudecode-managed"

# feat/hook-driven-status - events that are worth interrupting the user
# for (a toast). Unchanged from the original three; kept as its own tuple
# (rather than folded into ACTIVITY_ONLY_EVENTS below) because the hook
# endpoint (src/api/routes.py) branches on this exact set to decide
# whether to call ``SessionManager.record_toast`` + broadcast, in addition
# to always updating the activity tracker.
TOAST_EVENTS = ("Stop", "Notification", "PermissionRequest")

# feat/hook-driven-status - events that feed ONLY the activity-status state
# machine (src/core/session_activity.py), never a toast. PreToolUse and
# PostToolUse in particular fire on every single tool call - turning those
# into toasts would spam the user; they exist purely as the "working"
# heartbeat and the SubagentStart/SubagentStop pair.
ACTIVITY_ONLY_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "SubagentStart",
    "SubagentStop",
)

# feat/session-lineage - events that report WHICH CLAUDE CONVERSATION is
# running in this tmux session, and when it ended. Neither is a toast and
# neither feeds the activity state machine: they exist only to write
# ``sessions.claude_session_uuid`` / ``parent_session_id`` / ``fork_kind``
# (src/core/session_lineage.py).
#
# Kept as its own tuple for the same reason TOAST_EVENTS is: the endpoint
# branches on this exact set to decide whether to run the lineage write,
# so the routing cannot drift from what gets installed.
#
# PAYLOAD SHAPE, VERIFIED AGAINST THE SHIPPED BINARY (2.1.236) rather
# than taken from prose. Common to both: ``session_id``,
# ``transcript_path``, ``cwd``, ``hook_event_name``, ``permission_mode``.
# SessionStart additionally carries ``source``, whose enum is literally
# ["startup", "resume", "clear", "compact", "fork"], plus ``agent_type``,
# ``model`` and ``session_title`` - the last three are undocumented and
# are therefore read defensively, never required. SessionEnd additionally
# carries ``reason``.
LIFECYCLE_EVENTS = ("SessionStart", "SessionEnd")

# Every event we install a managed hook for. Ordered for deterministic
# JSON output diff-stability (toast-worthy first, matching the original
# three's historical order, then the new activity-only events, then the
# lifecycle pair).
_MANAGED_EVENTS = TOAST_EVENTS + ACTIVITY_ONLY_EVENTS + LIFECYCLE_EVENTS


def _build_managed_command(event_kind: str) -> str:
    """Build the curl one-liner for a given hook event.

    The hook reads its JSON payload from stdin, pipes it straight into
    curl's ``--data-binary @-`` body, attaches the cloudecode auth
    headers from env vars, fires the POST to the loopback URL, and
    backgrounds the whole thing so Claude's own flow is never blocked
    waiting for the hook response. ``-m 3`` caps each call at 3 seconds
    even if the server is hung.

    The literal ``# cloudecode-managed`` comment is appended (as a
    no-op tail in the shell command) purely as a marker we can grep for
    on subsequent merges. Shells treat it as a comment so it has zero
    runtime effect.
    """
    return (
        # ``cat`` reads the hook's stdin JSON and pipes it into curl's
        # ``--data-binary @-`` so the full payload reaches the endpoint
        # unchanged. ``-sS`` = silent except on error. ``-m 3`` caps TOTAL
        # time at 3s, and that cap is the ONLY thing this needs in order to
        # be safe to run synchronously.
        #
        # IT USED TO END IN ``& :`` AND THAT SILENTLY DELIVERED NOTHING.
        # The reasoning was "background it so Claude Code's own loop is
        # never blocked waiting on us", which sounds prudent and was fatal:
        # the shell returned instantly, the backgrounded subshell was
        # orphaned, and curl was reaped before the POST completed. The hook
        # ran, exited 0, and wrote nothing anywhere - so every layer
        # reported success while claude_session_uuid was never bound, and
        # resume and fork could not work for any session.
        #
        # Measured rather than reasoned about, same env, same headers, same
        # payload, one variable changed:
        #     with the trailing &   -> row unchanged
        #     without it            -> uuid bound immediately
        #
        # The cost of dropping it is that Claude waits up to 3s on an
        # unreachable endpoint. That is the trade ``-m 3`` was already
        # there to make, and a hook that never delivers is not faster, it
        # is absent.
        "(cat | curl -sS -m 2 -X POST \"$CLOUDECODE_HOOK_URL\" "
        "-H \"X-Cloudecode-Session: $CLOUDECODE_SESSION_ID\" "
        "-H \"X-Cloudecode-Token: $CLOUDECODE_HOOK_TOKEN\" "
        f"-H \"X-Cloudecode-Event: {event_kind}\" "
        "-H \"Content-Type: application/json\" "
        "--data-binary @-) > /dev/null 2>&1 "
        f"; : {CLOUDECODE_HOOKS_MARKER}"
    )


def _build_hook_block() -> dict[str, list[dict[str, Any]]]:
    """Build the canonical cloudecode hook block.

    Structure matches Claude Code's documented hook schema::

        {
          "Stop": [
            {
              "matcher": "*",
              "hooks": [
                {"type": "command", "command": "<one-liner>"}
              ]
            }
          ],
          ...
        }
    """
    block: dict[str, list[dict[str, Any]]] = {}
    for event in _MANAGED_EVENTS:
        block[event] = [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": _build_managed_command(event),
                    }
                ],
            }
        ]
    return block


def _is_managed_command(cmd: Any) -> bool:
    """True iff a hook command string carries the cloudecode marker.

    Tolerates non-string ``command`` values (just returns False rather
    than raising) so a user's custom hook with an unexpected shape can't
    crash our merge.
    """
    return isinstance(cmd, str) and CLOUDECODE_HOOKS_MARKER in cmd


def _filter_user_matchers(
    matchers: list[Any], event: str
) -> list[Any]:
    """Drop every cloudecode-managed entry from a matcher list.

    A matcher entry is the ``{"matcher": "*", "hooks": [...]}`` dict.
    We classify it as managed if AT LEAST ONE of its inner ``hooks``
    bears our marker. (Mixed user/managed matcher dicts shouldn't
    happen if everyone respects the marker convention, but if they do
    we err on the side of letting the user's entry through and
    re-appending our own clean entry below.)
    """
    keep: list[Any] = []
    for matcher_entry in matchers:
        if not isinstance(matcher_entry, dict):
            keep.append(matcher_entry)
            continue
        inner_hooks = matcher_entry.get("hooks", [])
        if not isinstance(inner_hooks, list):
            keep.append(matcher_entry)
            continue
        # If ANY hook in this matcher is managed, drop the entry. We're
        # going to re-add our canonical entry below, so dropping the
        # whole matcher prevents partial-duplicate states.
        if any(
            isinstance(h, dict) and _is_managed_command(h.get("command"))
            for h in inner_hooks
        ):
            continue
        keep.append(matcher_entry)
    return keep


def _merge_hooks(
    existing: dict[str, Any], managed: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Return ``existing`` with managed hooks added/replaced in place.

    User-added hooks under the same event are preserved. Managed
    hooks (identified by marker) are stripped and re-added so we end
    up with EXACTLY one canonical cloudecode entry per event.

    Always operates on a fresh shallow copy of the top-level dict so
    callers' references aren't mutated.
    """
    merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

    hooks_block = merged.get("hooks")
    if not isinstance(hooks_block, dict):
        hooks_block = {}
    else:
        hooks_block = dict(hooks_block)

    for event, managed_matchers in managed.items():
        existing_matchers = hooks_block.get(event, [])
        if not isinstance(existing_matchers, list):
            existing_matchers = []
        # Strip any previously-managed entries so re-running the merge
        # doesn't duplicate hooks. User entries pass through untouched.
        user_matchers = _filter_user_matchers(existing_matchers, event)
        # Append our canonical block AFTER user matchers - Claude Code
        # runs matchers in order, and we'd rather user hooks fire first
        # (their decisions can short-circuit ours via exit codes).
        hooks_block[event] = user_matchers + list(managed_matchers)

    merged["hooks"] = hooks_block
    return merged


def _hooks_disabled() -> bool:
    """Read the opt-out flag from the live ``settings.load_auth_config()``.

    Defaults to False (= hooks enabled) when the config is missing the
    field or any read raises. The whole hook subsystem is best-effort;
    we never let a config glitch take down server startup.
    """
    try:
        from src.config import settings

        auth_cfg = settings.load_auth_config()
        notif_cfg = getattr(auth_cfg, "notifications", None)
        if notif_cfg is None:
            return False
        return bool(getattr(notif_cfg, "disable_claude_hooks", False))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("claude_hooks_disabled_check_failed", error=str(exc))
        return False


#: Env var that overrides the production settings destination. Exists so
#: ``tests/conftest.py`` can point the DEFAULT at a temp file, which makes
#: the ordinary path safe as well as guarded. Production leaves it unset.
SETTINGS_PATH_ENV_VAR = "CLOUDE_CLAUDE_SETTINGS_PATH"


def default_settings_path() -> Path:
    """Resolve the settings file the production caller means.

    This used to be an inline ``or`` fallback inside
    :func:`ensure_hook_settings`, which meant a caller that passed
    nothing INHERITED the developer's real ``~/.claude/settings.json``
    without ever saying so. A plain ``pytest`` run then merged
    CloudeCode's managed hook block into the developer's live Claude Code
    configuration, silently and successfully.

    Making it a named function with no caller-side default turns that
    into a decision somebody has to write down.

    Inputs:
        None. Reads :data:`SETTINGS_PATH_ENV_VAR` from the environment.
    Outputs:
        Path - the override when set, otherwise
        ``~/.claude/settings.json``.
    Example:
        >>> default_settings_path()
        PosixPath('/Users/someone/.claude/settings.json')
    """
    override = os.environ.get(SETTINGS_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "settings.json"


def ensure_hook_settings(settings_path: Path) -> bool:
    """Idempotently merge cloudecode's hook block into a settings file.

    Called once at FastAPI startup (from ``src/main.py``'s lifespan) as
    ``ensure_hook_settings(default_settings_path())``.

    ``settings_path`` is REQUIRED and has no default on purpose. See
    :func:`default_settings_path`.

    Args:
        settings_path: The settings file to merge into. Never optional.

    Returns:
        True on success (file written or no-op when disabled), False on
        any handled failure (parse error, write failure). The caller
        should LOG and CONTINUE; hook integration is best-effort and
        must never block server boot.

    Raises:
        OutsideTempWriteError: only during a test run, and only for a
            destination outside every temp root. Deliberately NOT caught
            here and deliberately NOT folded into the ``False`` return:
            a harness violation must fail the test loudly rather than
            degrade into a handled failure nobody reads.
    """
    if _hooks_disabled():
        logger.info("claude_hooks_disabled_by_config")
        return True

    path = settings_path

    # Blast-radius control. Inert in production; under pytest this
    # refuses any destination outside a temp root, which is the only
    # check that survives a caller importing this module directly, a
    # test building its own app, or a subprocess.
    assert_test_write_allowed(path)

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                existing = json.loads(raw)
                if not isinstance(existing, dict):
                    logger.warning(
                        "claude_settings_not_object",
                        path=str(path),
                        type=type(existing).__name__,
                    )
                    return False
        except json.JSONDecodeError as exc:
            # User's file is corrupted/unparseable. We CANNOT safely merge
            # - bail loud rather than clobber.
            logger.warning(
                "claude_settings_unparseable",
                path=str(path),
                error=str(exc),
            )
            return False
        except OSError as exc:
            logger.warning(
                "claude_settings_read_failed",
                path=str(path),
                error=str(exc),
            )
            return False

    managed = _build_hook_block()
    merged = _merge_hooks(existing, managed)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".cloudecode-tmp")
        tmp.write_text(
            json.dumps(merged, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info(
            "claude_hooks_settings_written",
            path=str(path),
            events=list(managed.keys()),
        )
        return True
    except OSError as exc:
        logger.warning(
            "claude_hooks_settings_write_failed",
            path=str(path),
            error=str(exc),
        )
        return False
