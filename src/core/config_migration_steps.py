"""The individual config.json version steps, one function per hop.

Extracted from ``src/core/config_migration.py`` (feat/universal-wrappers),
which crossed the repo's 500-line budget when the 2 -> 3 step was added.
That module keeps the orchestration (``migrate_config_dict``), the file
I/O (``migrate_config_file``) and ``CURRENT_CONFIG_VERSION``; this one
holds the pure per-version transforms plus the environment probe and seed
builder they use.

Every step here is ADDITIVE and never mutates its input: it returns a NEW
dict, so a caller holding the original can still inspect the
pre-migration state. See ``CURRENT_CONFIG_VERSION``'s comment in
config_migration.py for the version history and the reasoning behind each
step.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Dict, List, Optional

import structlog

from src.core.agent_families import DEFAULT_FAMILY
from src.core.agent_wrappers import derive_accepts_model
from src.core.slash_command_labels import (
    MIGRATION_APPENDED_COMMANDS,
    append_missing_commands,
)
from src.core.terminal_commands import (
    TERMINAL_COMMANDS_KEY,
    default_terminal_commands,
)

logger = structlog.get_logger()

# Top-level config.json key holding the quick-command chips. Named here
# rather than inline so the step and its tests agree on one spelling.
COMMON_SLASH_COMMANDS_KEY = "common_slash_commands"

_SEED_SCRIPT_CLD = 'cld "$@"'
_SEED_SCRIPT_CLDOR = 'cldor "$@"'


def probe_shell_function(
    name: str, runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None
) -> bool:
    """Check whether a zsh function/command named ``name`` resolves.

    Description: runs ``zsh -ic 'type <name>'`` (interactive shell, so
      ``~/.zshrc`` is sourced - same requirement tmux's own spawned pane
      has, see module docstring point 3) and treats exit code 0 as
      "resolves". Never raises - a missing ``zsh`` binary, a timeout, or
      any subprocess error is treated as "not found" (fail toward NOT
      seeding rather than guessing).
    Inputs:
      name (str) - function/command name to probe, e.g. "cld".
      runner (callable | None) - injected for tests; defaults to
        ``subprocess.run``. Signature: ``(argv: list[str]) ->
        subprocess.CompletedProcess``.
    Output: bool - True if the shell reports it resolves.
    Example: probe_shell_function("cld") -> True on a machine whose
      ~/.zshrc defines a `cld` function; False on one that doesn't.
    """
    run = runner or (
        lambda argv: subprocess.run(argv, capture_output=True, timeout=10)
    )
    try:
        result = run(["zsh", "-ic", f"type {name}"])
        return result.returncode == 0
    except Exception as e:
        logger.warning("probe_shell_function_failed", name=name, error=str(e))
        return False


def build_seed_wrappers(has_cld: bool, has_cldor: bool) -> List[Dict]:
    """Build the list of wrapper dicts to seed from environment detection.

    Description: a plain "claude" wrapper (the safe, universal default -
      marked ``default: true`` so a freshly migrated config with NEITHER
      cld nor cldor detected still launches Claude directly, never a
      function that doesn't exist) plus one entry per detected function.
      When cld is detected, IT becomes default instead (matches today's
      actual fallback behavior: the author's untouched config launches
      via cld, not plain claude).
    Inputs: has_cld (bool); has_cldor (bool) - probe results.
    Output: list[dict] - AgentWrapper-shaped dicts, ready for
      ``AgentWrapper(**d)`` or direct JSON serialization.
      ``accepts_model`` is set explicitly here (not derived) because this
      function AUTHORS these wrappers and therefore knows the answer -
      ``derive_accepts_model``'s heuristic exists only for wrappers that
      predate the field or were hand-written by the user.
    Example: build_seed_wrappers(True, True) -> [claude(default=False),
      cld(default=True), cldor(default=False, accepts_model=True)]
    """
    wrappers: List[Dict] = [
        {
            "id": "claude",
            "family": DEFAULT_FAMILY,
            "label": "claude",
            # ``command`` IS LOAD-BEARING, not tidiness.
            #
            # Every wrapper runs inside ``zsh -c 'source ~/.zshrc; ...'``,
            # because sourcing the rc is how the pane gets the user's PATH
            # and their own shell FUNCTIONS (cld, cldor). Sourcing it also
            # brings in their ALIASES, and a bare ``claude`` then resolves
            # to whichever alias they happen to have.
            #
            # Measured on a real install: a user's rc carried
            #   alias claude="security unlock-keychain ... && claude"
            # so every session this wrapper launched blocked forever on an
            # interactive keychain password prompt, in a pane with nobody
            # to type it. Claude never started, so its SessionStart hook
            # never fired, so claude_session_uuid was never bound, so
            # resume and fork could not work - from one alias.
            #
            # ``command`` bypasses aliases AND functions, which is exactly
            # right HERE (this row means "the plain CLI, no wrapper") and
            # exactly wrong for a wrapper whose whole point is to call a
            # function the rc defines. Do not add it to those.
            "script": "command claude --dangerously-skip-permissions",
            "entry": None,
            "description": "plain Claude Code CLI, no wrapper",
            "default": not has_cld,
            # Plain claude takes a prompt, not a model id. See
            # AgentWrapper.accepts_model.
            "accepts_model": False,
        }
    ]
    if has_cld:
        wrappers.append(
            {
                "id": "cld",
                "family": DEFAULT_FAMILY,
                "label": "cld (subscription)",
                "script": _SEED_SCRIPT_CLD,
                "entry": None,
                "description": (
                    "forwards to the cld shell function already defined in "
                    "~/.zshrc (subscription OAuth via macOS Keychain)"
                ),
                "default": True,
                # cld forwards "$@" straight to claude, so a model id
                # would land as a PROMPT argument. Never offer it one.
                "accepts_model": False,
            }
        )
    if has_cldor:
        wrappers.append(
            {
                "id": "cldor",
                "family": DEFAULT_FAMILY,
                "label": "cldor (openrouter)",
                "script": _SEED_SCRIPT_CLDOR,
                "entry": None,
                "description": (
                    "forwards to the cldor shell function already defined in "
                    "~/.zshrc (OpenRouter-routed, model as first argument)"
                ),
                "default": False,
                # cldor's whole purpose: it consumes $1 as the model id.
                "accepts_model": True,
            }
        )
    return wrappers


def _step_v0_to_v1(data: Dict, has_cld: bool, has_cldor: bool) -> Optional[Dict]:
    """Version step 0 -> 1: seed ``agents.wrappers`` from the environment.

    Description: the original feat/launch-wrappers migration, unchanged in
      behavior - only the version stamping moved out to the caller so
      steps can be chained. Legacy ``*_command`` keys are NEVER modified
      or removed, only read (for the opt-out check) and left in place.
    Inputs:
      data (dict) - the parsed config.json (never mutated).
      has_cld (bool) / has_cldor (bool) - environment probe results.
    Output: dict | None - the new config dict, or None when the input
      can't be confidently interpreted (caller must then abandon the whole
      migration and leave config.json completely untouched).
    """
    agents_block = data.get("agents")
    if agents_block is None:
        agents_block = {}
    if not isinstance(agents_block, dict):
        logger.warning(
            "config_migration_skipped_bad_agents_block",
            raw_type=type(agents_block).__name__,
        )
        return None

    # An explicit non-empty claude_command means the user already opted
    # out of the cld/cldor fallback - don't second-guess that by also
    # seeding cld/cldor wrappers (they'd sit unused, and once ANY wrapper
    # list is present it becomes the FIRST-checked source for
    # agent_type=="claude", which would silently stop honoring their
    # custom command). Skip wrapper-seeding entirely in this case.
    existing_claude_command = agents_block.get("claude_command", "")
    if existing_claude_command and str(existing_claude_command).strip():
        new_data = dict(data)
        new_data["agents"] = dict(agents_block)
        logger.info(
            "config_migration_version_stamped_no_wrappers",
            reason="explicit_claude_command_present",
        )
        return new_data

    if agents_block.get("wrappers"):
        # Already has wrappers (hand-authored or a partial prior
        # migration) - don't touch the list.
        new_data = dict(data)
        new_data["agents"] = dict(agents_block)
        return new_data

    seed = build_seed_wrappers(has_cld, has_cldor)
    new_agents_block = dict(agents_block)
    new_agents_block["wrappers"] = seed

    new_data = dict(data)
    new_data["agents"] = new_agents_block
    logger.info(
        "config_migration_seeded_wrappers",
        wrapper_ids=[w["id"] for w in seed],
        has_cld=has_cld,
        has_cldor=has_cldor,
    )
    return new_data


def _step_v1_to_v2(data: Dict) -> Dict:
    """Version step 1 -> 2: ADDITIVE ONLY, never renames or removes.

    Description: two additions, both no-ops when already present.
      1. ``accepts_model`` on each entry of ``agents.wrappers``, derived
         once from the wrapper's own text (``derive_accepts_model``) so
         an existing OpenRouter wrapper keeps being offered models while
         every other wrapper defaults to not accepting one - which is
         exactly today's behavior for them, minus the regression where a
         model was forwarded to a wrapper that ignores it.
      2. The top-level ``terminal_commands`` seed list.
      Wrapper ``id`` values and the ``agents.wrappers`` key itself are
      untouched: ``Session.agent_type`` stores a wrapper id, so renaming
      one would orphan every session already recorded against it.
      A config with NO wrappers (or no ``agents`` block at all) is fine -
      there is simply nothing to annotate.
    Inputs: data (dict) - config dict at version 1 (never mutated).
    Output: dict - new config dict; equal in content to the input when
      both additions were already present.
    """
    new_data = dict(data)

    agents_block = new_data.get("agents")
    if isinstance(agents_block, dict):
        wrappers = agents_block.get("wrappers")
        if isinstance(wrappers, list):
            annotated: List[Dict] = []
            for wrapper in wrappers:
                if not isinstance(wrapper, dict):
                    annotated.append(wrapper)
                    continue
                new_wrapper = dict(wrapper)
                if "accepts_model" not in new_wrapper:
                    new_wrapper["accepts_model"] = derive_accepts_model(new_wrapper)
                annotated.append(new_wrapper)
            new_agents_block = dict(agents_block)
            new_agents_block["wrappers"] = annotated
            new_data["agents"] = new_agents_block

    if TERMINAL_COMMANDS_KEY not in new_data:
        new_data[TERMINAL_COMMANDS_KEY] = default_terminal_commands()

    return new_data


def _step_v2_to_v3(data: Dict) -> Dict:
    """Version step 2 -> 3: stamp ``family`` on every existing wrapper.

    Description: one addition, a no-op when already present. Every wrapper
      reachable in a v2 config is a CLAUDE wrapper by construction - the
      field did not exist, and ``Settings.get_agent_command`` only ever
      consulted ``agents.wrappers`` on the claude path, so any wrapper the
      user configured was, in effect, a claude wrapper whatever they
      intended it for. Writing ``"claude"`` therefore records what was
      already true rather than making a choice on the user's behalf.

      A wrapper that ALREADY has a ``family`` key is left completely
      alone, including a value this code does not recognise: a
      user-edited value is never clobbered, and validation of unknown
      names belongs to ``AgentWrapper``, not to a migration whose only
      job is to be additive.

      Wrapper ``id`` values are untouched for the same reason as every
      prior step: ``Session.agent_type`` stores them.
      A config with no wrappers, no ``agents`` block, or a malformed one
      is fine - there is simply nothing to annotate.
    Inputs: data (dict) - config dict at version 2 (never mutated).
    Output: dict - new config dict; equal in content to the input when
      every wrapper already declared a family.
    Example: _step_v2_to_v3({"agents": {"wrappers": [{"id": "cld"}]}}) ->
      {"agents": {"wrappers": [{"id": "cld", "family": "claude"}]}}
    """
    new_data = dict(data)

    agents_block = new_data.get("agents")
    if not isinstance(agents_block, dict):
        return new_data

    wrappers = agents_block.get("wrappers")
    if not isinstance(wrappers, list):
        return new_data

    annotated: List[Dict] = []
    for wrapper in wrappers:
        if not isinstance(wrapper, dict) or "family" in wrapper:
            annotated.append(wrapper)
            continue
        new_wrapper = dict(wrapper)
        new_wrapper["family"] = DEFAULT_FAMILY
        annotated.append(new_wrapper)

    new_agents_block = dict(agents_block)
    new_agents_block["wrappers"] = annotated
    new_data["agents"] = new_agents_block
    return new_data


def _step_v3_to_v4(data: Dict) -> Dict:
    """Version step 3 -> 4: append ``/login`` to ``common_slash_commands``.

    Description: ADDITIVE ONLY. ``DEFAULT_COMMON_COMMANDS`` is consulted
      by ``/config/common-commands`` only when the config declares NO
      list of its own, so adding a command to that default reaches a
      fresh install and nobody else. A user whose config.json already
      carries the list would never see the new command without this step.
      Existing entries are passed through in their original form, string
      or ``{"command", "description"}`` object alike, so user wording and
      ordering survive untouched; the new commands are appended at the
      END, and one already present is skipped, which makes the step a
      no-op on second run.

      A config with NO ``common_slash_commands`` key is left WITHOUT one
      on purpose: the API already falls back to
      ``DEFAULT_COMMON_COMMANDS``, which now includes ``/login``, so
      materializing the key would freeze that user's list against every
      future default for no visible gain. A non-list value (a
      hand-mangled config) is likewise left alone rather than replaced.
    Inputs: data (dict) - config dict at version 3 (never mutated).
    Output: dict - new config dict; equal in content to the input when
      every appended command was already present or the key is absent.
    Example: _step_v3_to_v4({"common_slash_commands": ["/clear"]}) ->
      {"common_slash_commands": ["/clear", "/login"]}
    """
    new_data = dict(data)

    raw = new_data.get(COMMON_SLASH_COMMANDS_KEY)
    if not isinstance(raw, list):
        return new_data

    new_data[COMMON_SLASH_COMMANDS_KEY] = append_missing_commands(
        raw, MIGRATION_APPENDED_COMMANDS
    )
    return new_data
