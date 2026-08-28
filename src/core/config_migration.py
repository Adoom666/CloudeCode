"""Versioned, idempotent config.json migration.

Each ``_step_vN_to_vM`` function advances the config by exactly one
version; ``migrate_config_dict`` runs whichever steps a given config still
needs and stamps ``config_version`` once at the end. See
``CURRENT_CONFIG_VERSION`` for the version history. Every step must be
ADDITIVE: no key is ever renamed or removed, because live installs are
migrated in place with no coordination.

feat/launch-wrappers - introduces ``config_version`` (absent from every
prior config.json; treated as ``0``) and migrates a pre-wrappers config to
the wrapper shape described in ``src/core/agent_wrappers.py``, WITHOUT ever
touching or removing the legacy ``agents.claude_command`` /
``codex_command`` / ``hermes_command`` / ``openclaw_command`` keys - those
remain permanently supported by ``Settings.get_agent_command`` as the
fallback path for anyone who never adopts wrappers.

Design constraints (all load-bearing - see CLAUDE.md's doc-protocol and the
task brief this module was written against):

1. NEVER destructive. ``migrate_config_file`` backs up the pre-write bytes
   to ``config.json.bak`` (reusing the exact tmp-file + fsync + os.replace
   convention ``Settings.update_settings_config`` already uses - no second
   atomic-write convention introduced) before writing anything.
2. Idempotent. A config already at ``config_version >= CURRENT_CONFIG_VERSION``
   is returned completely unchanged (``changed=False``); running the
   migration twice in a row is a no-op the second time.
3. Environment-aware. ``probe_shell_function`` shells out to ``zsh -ic
   'type <name>'`` to check whether ``cld`` / ``cldor`` actually resolve in
   THIS user's interactive shell - mirrors exactly the resolution tmux's
   spawned pane needs (see ``Settings.get_agent_command``'s docstring on
   why ``zsh -c 'source ~/.zshrc; ...'`` is required at all). Only seeds a
   wrapper for a function that was actually detected; never guesses.
4. Fail-safe. ``migrate_config_dict`` is a pure function: any input it
   can't confidently interpret (malformed ``agents`` block, unexpected
   type) makes it return the ORIGINAL data completely unchanged with
   ``changed=False`` and, critically, ``config_version`` NOT stamped - so
   a future run (e.g. after the user fixes their config.json by hand) can
   still retry the migration instead of being permanently skipped by a
   half-written version marker.

Rollback: restore ``config.json`` from ``config.json.bak`` (see
``migrate_config_file``'s docstring for the exact command). The code-level
rollback point is git tag ``baseline/adoom-2026-08-14`` (commit 6392124) -
`git checkout baseline/adoom-2026-08-14 -- src/` restores the pre-wrappers
resolver entirely if a config-level rollback isn't enough. See also the
README section "rolling back the launch-wrappers migration".
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import structlog

from src.core.config_migration_steps import (
    _step_v0_to_v1,
    _step_v1_to_v2,
    _step_v2_to_v3,
    _step_v3_to_v4,
    build_seed_wrappers,
    probe_shell_function,
)

logger = structlog.get_logger()

# Bump when the migration shape changes. 0 = no config_version key present
# at all (every config.json that predates the wrappers feature).
#
# Version history:
#   0 -> 1  feat/launch-wrappers: seed ``agents.wrappers`` from the shell
#           functions this machine actually has (see ``_step_v0_to_v1``).
#   1 -> 2  feat/settings-tabs-and-commands: ADDITIVE ONLY. Adds
#           ``accepts_model`` to each existing wrapper and seeds the
#           top-level ``terminal_commands`` list (see ``_step_v1_to_v2``).
#           Renames nothing: ``agents.wrappers`` keeps its key and every
#           wrapper keeps its ``id``, because ``Session.agent_type`` stores
#           that id - renaming one would orphan every existing session.
#   2 -> 3  feat/universal-wrappers: ADDITIVE ONLY. Adds ``family`` to each
#           existing wrapper, always ``"claude"`` (see ``_step_v2_to_v3``),
#           because every wrapper that could exist at v2 was claude-only by
#           construction - wrappers had no family concept and
#           ``get_agent_command`` only ever consulted them for the claude
#           path. Adds no key to ``agents`` and removes nothing: the
#           ``*_command`` strings all stay, and every wrapper keeps its
#           ``id`` for the same ``Session.agent_type`` reason as 1 -> 2.
#   3 -> 4  ADDITIVE ONLY. Appends ``/login`` to an existing
#           ``common_slash_commands`` list (see ``_step_v3_to_v4``). The
#           built-in default list only reaches a config that declares no
#           list of its own, so without this step an existing install
#           would never gain the command. Touches no wrapper and no
#           ``id``.
CURRENT_CONFIG_VERSION = 4

# Thin, migration-seeded wrapper scripts. Deliberately NOT the real
# multi-line cld/cldor function bodies (those live only in the user's own
# ~/.zshrc, which we don't read or copy) - each one-liner simply forwards
# to the already-defined shell function, exactly matching what the old
# hardcoded ``zsh -ic 'cld'`` / ``zsh -ic 'cldor <model>'`` fallback did.
# `entry` is left unset: the line IS the runnable command (see
# agent_wrappers.render_wrapper_invocation's source-only mode).
def migrate_config_dict(
    data: Dict, has_cld: bool, has_cldor: bool
) -> Tuple[Dict, bool]:
    """Pure migration: run every version step the config still needs.

    Description: fail-safe by construction - a step that can't confidently
      proceed makes this return ``(data, False)`` with the input
      completely untouched (no partial mutation, no config_version
      stamped), never raises. Steps run in order and only for versions the
      config hasn't reached, so a v0 config runs 0->1, 1->2, 2->3 then
      3->4, a v3 config runs only 3->4, and a v4 config does nothing at
      all.
    Inputs:
      data (dict) - the parsed config.json.
      has_cld (bool) / has_cldor (bool) - environment probe results (see
        ``probe_shell_function``); pass explicitly so this function stays
        pure/deterministic and trivially testable without shelling out.
    Output: (dict, bool) - (possibly-new dict, whether anything changed).
      The returned dict is always a NEW object when changed=True (the
      input ``data`` is never mutated in place), so a caller holding a
      reference to the original can still inspect the pre-migration
      state.
    Raises: never - logs a warning and returns (data, False) instead.
    Example: migrate_config_dict({"agents": {}}, True, False) ->
      ({..., "agents": {..., "wrappers": [claude, cld]},
        "terminal_commands": [...], "config_version": 4}, True)
    """
    try:
        existing_version = data.get("config_version", 0)
        if not isinstance(existing_version, int):
            logger.warning(
                "config_migration_skipped_bad_version_type",
                raw=existing_version,
            )
            return data, False

        if existing_version >= CURRENT_CONFIG_VERSION:
            return data, False  # already migrated - idempotent no-op

        working = data
        if existing_version < 1:
            stepped = _step_v0_to_v1(working, has_cld, has_cldor)
            if stepped is None:
                # Fail-safe: leave the file entirely alone, unversioned,
                # so a later run can retry after the user fixes it.
                return data, False
            working = stepped
        if existing_version < 2:
            working = _step_v1_to_v2(working)
        if existing_version < 3:
            working = _step_v2_to_v3(working)
        if existing_version < 4:
            working = _step_v3_to_v4(working)

        new_data = dict(working)
        new_data["config_version"] = CURRENT_CONFIG_VERSION
        logger.info(
            "config_migration_version_advanced",
            from_version=existing_version,
            to_version=CURRENT_CONFIG_VERSION,
        )
        return new_data, True

    except Exception as e:  # pragma: no cover - defensive, fail-safe path
        logger.warning("config_migration_unexpected_error", error=str(e))
        return data, False


def migrate_config_file(config_path: Path) -> Dict:
    """Run the migration against a real config.json on disk, idempotently.

    Description: reads, probes the environment, calls
      ``migrate_config_dict``, and - only if it reports a change - backs
      up the pre-write bytes to ``config.json.bak`` (overwritten each
      call, same one-generation-of-history convention as
      ``Settings.update_settings_config``) then writes atomically via
      tmp-file + fsync + os.replace. A no-op migration (already current,
      or the fail-safe path) touches NEITHER the config file NOR the
      backup file at all.
    Inputs: config_path (Path) - path to config.json.
    Output: dict - ``{"migrated": bool, "changed": bool, "wrapper_ids":
      list[str]}``. ``migrated`` mirrors ``changed`` (kept as a separate,
      clearly-named key for callers that don't want to read the wrapper
      list to know whether anything happened).
    Raises:
      FileNotFoundError: if config_path doesn't exist.
      ValueError: invalid JSON in config_path.

    ROLLBACK: ``cp config.json.bak config.json`` restores the exact
    pre-migration bytes (delete config.json.bak afterward if you want a
    clean slate for a re-run). The code-level rollback point is git tag
    ``baseline/adoom-2026-08-14`` (commit 6392124).
    """
    if not config_path.exists():
        raise FileNotFoundError(f"config.json not found: {config_path}")

    with open(config_path) as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")

    has_cld = probe_shell_function("cld")
    has_cldor = probe_shell_function("cldor")

    new_data, changed = migrate_config_dict(data, has_cld, has_cldor)

    if not changed:
        return {"migrated": False, "changed": False, "wrapper_ids": []}

    backup_path = config_path.with_suffix(config_path.suffix + ".bak")
    try:
        backup_path.write_text(raw)
    except OSError as e:
        logger.warning("config_migration_backup_failed", error=str(e))
        # Fail-safe: don't write the migrated config if we couldn't
        # secure a rollback path first.
        return {"migrated": False, "changed": False, "wrapper_ids": []}

    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(new_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config_path)

    wrapper_ids = [
        w["id"] for w in (new_data.get("agents", {}) or {}).get("wrappers", []) or []
    ]
    logger.info("config_migration_applied", wrapper_ids=wrapper_ids)
    return {"migrated": True, "changed": True, "wrapper_ids": wrapper_ids}


def ensure_config_migrated(config_path: Path) -> None:
    """Best-effort migration entry point for app startup.

    Description: wraps ``migrate_config_file`` so a migration failure of
      any kind (missing file, bad JSON, unexpected exception) NEVER
      blocks server startup - same fail-soft posture as
      ``claude_hooks.ensure_hook_settings()`` in ``src/main.py``'s
      lifespan. Errors are logged, not raised.
    Inputs: config_path (Path).
    Output: None.
    """
    try:
        result = migrate_config_file(config_path)
        if result["changed"]:
            logger.info(
                "config_migration_startup_applied",
                wrapper_ids=result["wrapper_ids"],
            )
    except FileNotFoundError:
        pass  # setup_auth.py hasn't run yet - nothing to migrate
    except Exception as e:
        logger.warning("config_migration_startup_failed", error=str(e))
