"""v0.7.0 Part 3 — Claude Code lifecycle hook settings management.

This module owns the idempotent merge of cloudecode's hook block into
``~/.claude/settings.json``. Called once at FastAPI startup. The hooks
themselves are tiny shell one-liners that POST the event payload (read
from the hook's stdin) to cloudecode's loopback ``/api/v1/hooks/claude-event``
endpoint, carrying the per-session env vars
(``CLOUDECODE_SESSION_ID`` / ``CLOUDECODE_HOOK_TOKEN`` / ``CLOUDECODE_HOOK_URL``)
that were injected into the spawned ``claude`` process's environment via
``TmuxBackend.start(env=...)``.

Security model:
    The hook subprocess can't carry a JWT — there's no place for the user
    to authenticate the hook. Instead the hook proves identity via the
    HMAC-bearer token that ONLY the cloudecode process and the spawned
    agent share (env-injected at tmux session birth). The route ALSO
    requires loopback (127.0.0.1) — defense in depth.

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
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


# Literal marker embedded into every managed hook's command string. Used
# to identify (and replace) cloudecode's own hooks on re-run without
# touching anything the user added by hand or via another tool.
CLOUDECODE_HOOKS_MARKER = "# cloudecode-managed"

# Events we wire up. Ordered for deterministic JSON output diff-stability.
_MANAGED_EVENTS = ("Stop", "Notification", "PermissionRequest")


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
        # unchanged. ``-sS`` = silent except on error. ``-m 3`` caps total
        # time at 3s. ``> /dev/null 2>&1 &`` backgrounds + silences so
        # Claude Code's own loop is never blocked waiting on us.
        "(cat | curl -sS -m 3 -X POST \"$CLOUDECODE_HOOK_URL\" "
        "-H \"X-Cloudecode-Session: $CLOUDECODE_SESSION_ID\" "
        "-H \"X-Cloudecode-Token: $CLOUDECODE_HOOK_TOKEN\" "
        f"-H \"X-Cloudecode-Event: {event_kind}\" "
        "-H \"Content-Type: application/json\" "
        "--data-binary @-) > /dev/null 2>&1 & "
        f": {CLOUDECODE_HOOKS_MARKER}"
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
        # Append our canonical block AFTER user matchers — Claude Code
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
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("claude_hooks_disabled_check_failed", error=str(exc))
        return False


def ensure_hook_settings(
    settings_path: Path | None = None,
) -> bool:
    """Idempotently merge cloudecode's hook block into ``~/.claude/settings.json``.

    Called once at FastAPI startup (from ``src/main.py``'s lifespan).

    Args:
        settings_path: Override for tests. When None (production), uses
            ``~/.claude/settings.json``.

    Returns:
        True on success (file written or no-op when disabled), False on
        any handled failure (parse error, write failure). The caller
        should LOG and CONTINUE; hook integration is best-effort and
        must never block server boot.
    """
    if _hooks_disabled():
        logger.info("claude_hooks_disabled_by_config")
        return True

    path = settings_path or (Path.home() / ".claude" / "settings.json")

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
            # — bail loud rather than clobber.
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
