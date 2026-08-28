#!/usr/bin/env python3
"""Show, and optionally apply, an upgrade merge of ``config.json``.

WHAT THIS IS FOR

``config.json`` is authoritative for agent wrappers and slash commands. On
upgrade, new defaults should arrive without overwriting anything the user has
customised. Copying the new example over the top loses his edits; leaving the
file alone withholds every new default. This does the three-way merge instead,
and refuses to resolve a genuine conflict on his behalf.

DRY RUN IS THE DEFAULT. Nothing is written without ``--apply``.

THE BASE, AND WHY THE FIRST RUN IS NOISY

A three-way merge needs to know what the defaults USED to be. That record is
kept at ``<state dir>/config-base.json`` and is rewritten on every apply. The
first time this runs there is no base, so a field that differs from the new
default is genuinely ambiguous: it could be a customisation, or the default
could have moved. Those are reported as CANNOT DETERMINE rather than guessed,
his value is kept in every case, and the ambiguity disappears from then on.

BACKUPS

``--apply`` writes a timestamped copy of the current ``config.json`` into
``<state dir>/config-backups/`` before touching anything, together with a
manifest line recording the outcome. The manifest vocabulary matches
``scripts/upgrade_lib/upgrade_rollback_common.sh``: BACKED_UP, NOT_PRESENT,
MISSING, where MISSING is fatal. A backup that silently omits declared state
is worse than no backup.

Inputs:
    --config PATH      the live config.json (default: repo config.json)
    --defaults PATH    the new defaults (default: repo config.example.json)
    --state-dir PATH   override the state directory
    --apply            write the merge (default is a dry run)
    --import PATH      additionally append new upstream items to this list
                       field, for example --import common_slash_commands
    --json             emit the plan as JSON instead of text

Outputs:
    Exit 0 - merge computed (and applied when asked) with nothing needing
             attention.
    Exit 1 - a real error: unreadable config, unwritable backup.
    Exit 2 - the merge needs a human: conflicts, upstream removals, or
             undeterminable fields. Deliberately NOT exit 0, so an upgrade
             script cannot sail past it.

Example:
    ./venv/bin/python3 scripts/config_upgrade.py
    ./venv/bin/python3 scripts/config_upgrade.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config_defaults import (  # noqa: E402
    effective_shipped_defaults,
    supported_keys as model_supported_keys,
)
from src.core.config_merge import (  # noqa: E402
    CANNOT_DETERMINE,
    CONFLICT,
    REMOVED_UPSTREAM,
    apply_import,
    load_json,
    merge_config,
)

#: Filename of the recorded previous defaults, inside the state directory.
BASE_FILENAME = "config-base.json"

#: Directory holding timestamped pre-merge copies.
BACKUP_DIRNAME = "config-backups"

#: Human labels for each outcome, shown in the plan.
OUTCOME_LABELS = {
    "unchanged": "unchanged",
    "updated_default": "NEW DEFAULT   ",
    "kept_custom": "kept yours    ",
    "added": "ADDED         ",
    CONFLICT: "CONFLICT      ",
    REMOVED_UPSTREAM: "REMOVED UPSTREAM",
    CANNOT_DETERMINE: "CANNOT DETERMINE",
}


def resolve_state_dir(override: str | None) -> Path:
    """Resolve the durable state directory.

    Mirrors ``Settings.get_state_dir()`` in src/config.py: an explicit
    override wins, otherwise ``CLOUDE_STATE_DIR``, otherwise the macOS-native
    per-user location.

    Args:
        override: Explicit path from the command line, or None.

    Returns:
        The resolved directory. Not created here; the caller creates what it
        needs so a dry run never touches the filesystem.
    """
    if override:
        return Path(override).expanduser()

    env_value = os.environ.get("CLOUDE_STATE_DIR", "").strip()
    if env_value:
        return Path(env_value).expanduser()

    return Path.home() / "Library" / "Application Support" / "CloudeCode"


def back_up_config(config_path: Path, state_dir: Path) -> tuple[Path, str]:
    """Copy the live config aside before it is rewritten.

    Args:
        config_path: The file about to be modified.
        state_dir: Durable state directory.

    Returns:
        A tuple of (backup path or the intended path, manifest outcome). The
        outcome is BACKED_UP when bytes were copied and verified, or
        NOT_PRESENT when there was no config to copy.

    Raises:
        RuntimeError: The copy was attempted and the bytes did not land.
            Treated as fatal for the same reason the shell helper treats
            MISSING as fatal.
    """
    backup_dir = state_dir / BACKUP_DIRNAME
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"config.{stamp}.json"

    if not config_path.exists():
        return target, "NOT_PRESENT"

    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, target)

    if not target.exists() or target.stat().st_size != config_path.stat().st_size:
        raise RuntimeError(
            f"backup of {config_path} did not land at {target}; refusing to "
            "modify the config without a verified copy"
        )

    manifest = backup_dir / ".manifest"
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(f"BACKED_UP\tconfig\t{target.name}\n")

    return target, "BACKED_UP"


def render_plan(result, importable_selected: dict) -> str:
    """Format the merge plan for a terminal.

    Args:
        result: The MergeResult to describe.
        importable_selected: List imports the user asked to apply.

    Returns:
        A multi-line report.
    """
    lines: list[str] = []

    if not result.had_base:
        lines.append(
            "No record of the previous defaults was found, so any field that "
            "differs from the new default is reported as CANNOT DETERMINE "
            "rather than guessed. Your value was kept in every case. Applying "
            "records a base, and this ambiguity will not recur."
        )
        lines.append("")

    changes = result.changes()
    lines.append(f"Adopting {len(changes)} new or updated default(s):")
    for decision in changes:
        lines.append(
            f"  {OUTCOME_LABELS[decision.outcome]}  {decision.path} = "
            f"{json.dumps(decision.chosen)}"
        )
    if not changes:
        lines.append("  (none)")

    attention = result.needing_attention()
    lines.append("")
    lines.append(f"Needing your attention: {len(attention)}")
    for decision in attention:
        lines.append(f"  {OUTCOME_LABELS[decision.outcome]}  {decision.path}")
        lines.append(f"      yours   : {json.dumps(decision.mine)}")
        if decision.outcome != REMOVED_UPSTREAM:
            lines.append(f"      default : {json.dumps(decision.theirs)}")
        if decision.base is not None:
            lines.append(f"      was     : {json.dumps(decision.base)}")
        lines.append(f"      {decision.note}")
    if not attention:
        lines.append("  (none)")

    if result.importable:
        lines.append("")
        lines.append(
            "New items available in list settings. These are NOT applied "
            "automatically, because appending to a list you curated is still "
            "a change you did not ask for. Use --import <path> to take them:"
        )
        for path, items in result.importable.items():
            marker = "will import" if path in importable_selected else "available"
            lines.append(f"  [{marker}] {path}: {len(items)} new item(s)")
            for item in items:
                lines.append(f"      + {json.dumps(item)}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command line arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit code: 0 clean, 1 error, 2 needs a human.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(REPO_ROOT / "config.json"))
    parser.add_argument("--defaults", default=str(REPO_ROOT / "config.example.json"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--import", dest="imports", action="append", default=[])
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    defaults_path = Path(args.defaults).expanduser()
    state_dir = resolve_state_dir(args.state_dir)
    base_path = state_dir / BASE_FILENAME

    try:
        theirs = load_json(defaults_path)
    except json.JSONDecodeError as error:
        print(f"config_upgrade: {defaults_path} is not valid JSON: {error}", file=sys.stderr)
        return 1
    if theirs is None:
        print(f"config_upgrade: no defaults file at {defaults_path}", file=sys.stderr)
        return 1

    try:
        mine = load_json(config_path)
    except json.JSONDecodeError as error:
        print(f"config_upgrade: {config_path} is not valid JSON: {error}", file=sys.stderr)
        print("Refusing to merge; fix or move the file first.", file=sys.stderr)
        return 1

    if mine is None:
        # Nothing to preserve, so this is an install rather than a merge.
        mine = {}

    try:
        base = load_json(base_path)
    except json.JSONDecodeError:
        # A corrupt base is not a usable base, and pretending otherwise would
        # produce confidently wrong classifications.
        base = None

    # config.example.json is a hand-maintained SAMPLE of the shipped defaults,
    # not the defaults themselves. Two live settings (terminal_commands,
    # config_version) had already fallen out of it, and merging straight
    # against it reported both as REMOVED UPSTREAM - a confident verdict about
    # something never measured. The real defaults live in the Python model, so
    # fill the example's gaps from there and tell the merge that absence from
    # the example proves nothing on its own.
    supported = model_supported_keys()
    effective_theirs = effective_shipped_defaults(theirs, mine)
    stale_roots = frozenset(k for k in effective_theirs if k not in theirs)

    result = merge_config(
        mine,
        effective_theirs,
        base,
        supported_keys=supported,
        stale_roots=stale_roots,
    )

    selected = {p: result.importable[p] for p in args.imports if p in result.importable}
    unknown_imports = [p for p in args.imports if p not in result.importable]
    for path in unknown_imports:
        print(
            f"config_upgrade: nothing to import for '{path}' "
            "(no new upstream items, or not a list field)",
            file=sys.stderr,
        )

    merged = result.merged
    for path, items in selected.items():
        merged = apply_import(merged, path, items)

    if args.as_json:
        print(
            json.dumps(
                {
                    "had_base": result.had_base,
                    "changes": [d.__dict__ for d in result.changes()],
                    "attention": [d.__dict__ for d in result.needing_attention()],
                    "importable": result.importable,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(render_plan(result, selected))

    attention = result.needing_attention()

    if not args.apply:
        print("")
        print("Dry run. Nothing was written. Re-run with --apply to write it.")
        return 2 if attention else 0

    try:
        backup_target, outcome = back_up_config(config_path, state_dir)
    except (OSError, RuntimeError) as error:
        print(f"config_upgrade: backup failed: {error}", file=sys.stderr)
        return 1

    state_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    # Record the defaults this merge was resolved against, so the NEXT upgrade
    # can classify instead of reporting CANNOT DETERMINE.
    # Record the EFFECTIVE defaults, not the raw example. Recording the
    # example would mean the next run has a base that is missing exactly the
    # keys this one had to repair, so the same fields would report
    # CANNOT DETERMINE forever instead of resolving once.
    base_path.write_text(
        json.dumps(effective_theirs, indent=2) + "\n", encoding="utf-8"
    )

    print("")
    print(f"Wrote {config_path}")
    print(f"Backup: {outcome} {backup_target}")
    print(f"Recorded base: {base_path}")
    if attention:
        print("")
        print(
            f"{len(attention)} field(s) still need your attention. Your values "
            "were kept in every one of them; nothing was auto-merged."
        )
    return 2 if attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
