"""
Runtime discovery of a USER's own slash commands and skills.

Distinct from ``scripts/scrape-slash-commands.py`` (release-time, scrapes
Anthropic's official docs for the built-in/bundled command list) - this
module runs PER REQUEST, server-side, and answers a different question:
"what commands does *this* installation actually have, beyond the
built-ins?"

Per the official docs, custom commands have been MERGED INTO SKILLS: a
``.claude/commands/deploy.md`` and a ``.claude/skills/deploy/SKILL.md``
both create ``/deploy``. Both are scanned, at every scope Claude Code
itself reads from:

    - user scope    ``~/.claude/commands/**/*.md``, ``~/.claude/skills/**/SKILL.md``
    - plugin scope   ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/{commands,skills}``
    - project scope  ``<project>/.claude/commands/``, ``<project>/.claude/skills/``

Namespacing: a subdirectory under a commands/skills root becomes a
``:``-joined prefix, matching Claude Code's own behavior - e.g.
``~/.claude/commands/my/cb.md`` is exposed as ``/my:cb``. Verified against
this machine's real layout (7 files under ``~/.claude/commands/my/``).

``build_command_groups()`` is the single entry point the API layer calls;
it merges this runtime discovery with the release-time scraped JSON
(``src/data/slash-commands.json``) into the grouped shape the frontend
palette renders directly - see ``docs`` there for the group contract.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog
import yaml

logger = structlog.get_logger()

CLAUDE_HOME = Path.home() / ".claude"
SCRAPED_COMMANDS_PATH = Path(__file__).resolve().parent.parent / "data" / "slash-commands.json"

# First-line fallback patterns, tried in order when a file has no YAML
# frontmatter (or the frontmatter has no `description`). Generic markdown
# idioms, not tied to any specific command's wording.
_DESCRIPTION_LINE_RX = re.compile(r"^\*\*Description\*\*:\s*(.+)$", re.IGNORECASE)
_HEADING_LINE_RX = re.compile(r"^#{1,6}\s+(.+)$")
_FRONTMATTER_RX = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)

MAX_DESCRIPTION_LENGTH = 240


@dataclass
class DiscoveredCommand:
    """
    One discovered command or skill, in the same field shape as a scraped
    built-in record (``command``/``args``/``description``/``type``) plus
    ``alias_of`` for shape-compatibility, always None here - discovered
    commands don't carry alias metadata.

    Inputs (constructor): see field annotations below.
    """
    command: str  # e.g. "/my:cb" - always slash-prefixed.
    description: str
    type: str  # "user-command" | "user-skill" | "plugin-command" | "plugin-skill" | "project-command" | "project-skill"
    args: str = ""
    alias_of: Optional[str] = None
    source_path: str = ""


@dataclass
class CommandGroup:
    """One renderable group in the palette. ``id`` is stable and used by
    the frontend as a DOM/data key; ``label`` is the lowercase display
    string (matches the app's UI voice)."""
    id: str
    label: str
    commands: list = field(default_factory=list)


def _read_frontmatter_description(text: str) -> Optional[str]:
    """
    Description: extract the ``description`` field from a file's YAML
        frontmatter, if present.
    Inputs: text (str) - full file contents.
    Output: str|None - the description value, or None if there's no
        frontmatter block or no `description` key (or it fails to parse,
        which is tolerated rather than raised - a malformed frontmatter
        block in a user's own file shouldn't break the whole palette).
    Example: _read_frontmatter_description("---\\nname: x\\ndescription: y\\n---\\nbody")
        -> "y"
    """
    match = _FRONTMATTER_RX.match(text)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("slash_command_frontmatter_parse_failed", error=str(exc))
        return None
    if isinstance(data, dict):
        desc = data.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
    return None


def _first_meaningful_line(text: str, skip_frontmatter: bool) -> Optional[str]:
    """
    Description: fallback description extraction when there's no usable
        frontmatter - tries, in order, a ``**Description**: ...`` line
        (a convention used by this user's own command files), then the
        first markdown heading, then the first non-blank line verbatim.
    Inputs:
        text (str) - full file contents.
        skip_frontmatter (bool) - True to skip over a leading `---` block
            before scanning body lines.
    Output: str|None - trimmed to MAX_DESCRIPTION_LENGTH, or None if the
        file has no non-blank lines at all.
    """
    body = text
    if skip_frontmatter:
        match = _FRONTMATTER_RX.match(text)
        if match:
            body = text[match.end():]

    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if not lines:
        return None

    for line in lines[:15]:
        m = _DESCRIPTION_LINE_RX.match(line)
        if m:
            return m.group(1).strip()[:MAX_DESCRIPTION_LENGTH]

    for line in lines[:5]:
        m = _HEADING_LINE_RX.match(line)
        if m:
            return m.group(1).strip()[:MAX_DESCRIPTION_LENGTH]

    return lines[0][:MAX_DESCRIPTION_LENGTH]


def _describe_file(path: Path) -> str:
    """
    Description: resolve the best available description for a command or
        skill file - frontmatter first, then the first-meaningful-line
        fallbacks, then the filename itself as a last resort.
    Inputs: path (Path) - the ``.md``/``SKILL.md`` file.
    Output: str - never empty.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("slash_command_file_unreadable", path=str(path), error=str(exc))
        return path.stem

    frontmatter_desc = _read_frontmatter_description(text)
    if frontmatter_desc:
        return frontmatter_desc

    fallback = _first_meaningful_line(text, skip_frontmatter=True)
    return fallback or path.stem


def _namespaced_name(rel_parts: tuple) -> str:
    """
    Description: join relative path parts into a Claude Code-style
        namespaced command name.
    Inputs: rel_parts (tuple[str, ...]) - path components relative to a
        commands/skills root, extension already stripped.
    Output: str - ``:``-joined, e.g. ("my", "cb") -> "my:cb".
    Example: _namespaced_name(("cb",)) -> "cb"
    """
    return ":".join(rel_parts)


def _scan_command_files(root: Path, cmd_type: str) -> list[DiscoveredCommand]:
    """
    Description: scan a ``commands/`` directory tree for ``*.md`` files,
        one command per file, namespaced by subdirectory.
    Inputs:
        root (Path) - the commands/ directory (e.g. ``~/.claude/commands``).
        cmd_type (str) - the `type` tag to stamp on every result (e.g.
            "user-command").
    Output: list[DiscoveredCommand] - empty if root doesn't exist.
    """
    if not root.is_dir():
        return []
    results = []
    for md_path in sorted(root.rglob("*.md")):
        if md_path.name.startswith("."):
            continue
        rel = md_path.relative_to(root).with_suffix("")
        name = _namespaced_name(rel.parts)
        results.append(DiscoveredCommand(
            command=f"/{name}",
            description=_describe_file(md_path),
            type=cmd_type,
            source_path=str(md_path),
        ))
    return results


def _scan_skill_dirs(root: Path, cmd_type: str) -> list[DiscoveredCommand]:
    """
    Description: scan a ``skills/`` directory tree for ``SKILL.md`` files,
        one command per skill directory, namespaced by subdirectory.
    Inputs:
        root (Path) - the skills/ directory (e.g. ``~/.claude/skills``).
        cmd_type (str) - the `type` tag to stamp on every result (e.g.
            "user-skill").
    Output: list[DiscoveredCommand] - empty if root doesn't exist.
    """
    if not root.is_dir():
        return []
    results = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        rel_dir = skill_md.parent.relative_to(root)
        name = _namespaced_name(rel_dir.parts)
        results.append(DiscoveredCommand(
            command=f"/{name}",
            description=_describe_file(skill_md),
            type=cmd_type,
            source_path=str(skill_md),
        ))
    return results


def discover_user_scope() -> list[DiscoveredCommand]:
    """
    Description: scan ``~/.claude/commands`` and ``~/.claude/skills`` -
        the current user's own commands/skills, independent of any
        specific project.
    Inputs: none.
    Output: list[DiscoveredCommand].
    """
    return (
        _scan_command_files(CLAUDE_HOME / "commands", "user-command")
        + _scan_skill_dirs(CLAUDE_HOME / "skills", "user-skill")
    )


def discover_project_scope(project_path: str) -> list[DiscoveredCommand]:
    """
    Description: scan ``<project_path>/.claude/commands`` and
        ``<project_path>/.claude/skills``.
    Inputs: project_path (str) - absolute path to a project's working
        directory. Empty/None/nonexistent path yields an empty list
        rather than raising - a missing project-scope directory is the
        normal case, not an error.
    Output: list[DiscoveredCommand].
    """
    if not project_path:
        return []
    root = Path(project_path)
    return (
        _scan_command_files(root / ".claude" / "commands", "project-command")
        + _scan_skill_dirs(root / ".claude" / "skills", "project-skill")
    )


def _plugin_display_name(plugin_root: Path) -> str:
    """
    Description: derive a human-readable plugin name from its cache path.
    Inputs: plugin_root (Path) - a ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>``
        directory.
    Output: str - the ``<plugin>`` path segment (version stripped), e.g.
        "claude-mem" for ".../thedotmack/claude-mem/12.1.5".
    """
    # parts: .../cache/<marketplace>/<plugin>/<version>
    return plugin_root.parent.name


def discover_plugin_scope() -> dict[str, list[DiscoveredCommand]]:
    """
    Description: scan every installed plugin's ``commands/`` and
        ``skills/`` directories under ``~/.claude/plugins/cache/``.
        Plugins are installed at
        ``cache/<marketplace>/<plugin>/<version>/``; each version
        directory is scanned independently (there is normally exactly
        one installed version per plugin).
    Inputs: none.
    Output: dict[str, list[DiscoveredCommand]] - keyed by plugin display
        name, e.g. {"claude-mem": [...]}. Plugins with no commands or
        skills (hook-only plugins, e.g. this machine's "kintsugi" and
        "maestro") are simply absent from the result - an empty group
        would add UI noise for nothing.
    """
    cache_root = CLAUDE_HOME / "plugins" / "cache"
    if not cache_root.is_dir():
        return {}

    by_plugin: dict[str, list[DiscoveredCommand]] = {}
    # marketplace / plugin / version
    for marketplace_dir in sorted(p for p in cache_root.iterdir() if p.is_dir()):
        for plugin_dir in sorted(p for p in marketplace_dir.iterdir() if p.is_dir()):
            for version_dir in sorted(p for p in plugin_dir.iterdir() if p.is_dir()):
                plugin_name = _plugin_display_name(version_dir)
                found = (
                    _scan_command_files(version_dir / "commands", "plugin-command")
                    + _scan_skill_dirs(version_dir / "skills", "plugin-skill")
                )
                if found:
                    by_plugin.setdefault(plugin_name, []).extend(found)
    return by_plugin


def _load_scraped_commands() -> list[dict]:
    """
    Description: load the release-time scraped built-in/skill/workflow
        command list.
    Inputs: none.
    Output: list[dict] - the `commands` array from
        ``src/data/slash-commands.json``; empty list (logged) if the file
        is missing or unparseable rather than raising - a stale or absent
        scrape shouldn't take down the whole palette, just shrink it to
        the runtime-discovered commands.
    """
    try:
        data = json.loads(SCRAPED_COMMANDS_PATH.read_text(encoding="utf-8"))
        return data.get("commands", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("scraped_slash_commands_load_failed", path=str(SCRAPED_COMMANDS_PATH), error=str(exc))
        return []


def _group_user_scope(discovered: list[DiscoveredCommand]) -> list[CommandGroup]:
    """
    Description: split user-scope discoveries into "your commands" (no
        namespace) plus one sub-group per namespace, per Task 3's
        "sub-grouped by namespace, e.g. 'my'" requirement.
    Inputs: discovered (list[DiscoveredCommand]) - result of
        `discover_user_scope()`.
    Output: list[CommandGroup] - ["user"] first (if any un-namespaced
        commands exist), then one "user:<namespace>" group per namespace
        in sorted order.
    """
    root_commands = []
    by_namespace: dict[str, list] = {}
    for cmd in discovered:
        # command looks like "/my:cb" or "/skills-x" (skills are rarely
        # namespaced on this machine, but the split is general).
        body = cmd.command.lstrip("/")
        if ":" in body:
            namespace = body.split(":", 1)[0]
            by_namespace.setdefault(namespace, []).append(cmd)
        else:
            root_commands.append(cmd)

    groups = []
    if root_commands:
        groups.append(CommandGroup(id="user", label="your commands", commands=root_commands))
    for namespace in sorted(by_namespace):
        groups.append(CommandGroup(
            id=f"user:{namespace}",
            label=f"your commands ({namespace})",
            commands=by_namespace[namespace],
        ))
    return groups


def build_command_groups(project_path: Optional[str] = None) -> list[CommandGroup]:
    """
    Description: the single entry point the API layer calls. Merges the
        release-time scraped built-in/skill/workflow list with runtime
        discovery of this user's own commands, installed plugins, and
        (if given) the current project's commands, into the DERIVED
        groups Task 3 asks for - never hand-curated, so they can't rot as
        commands are added or removed on disk.
    Inputs: project_path (str|None) - absolute path to the active
        project's working directory; project-scope discovery is skipped
        entirely when omitted.
    Output: list[CommandGroup] in a fixed, stable order: built-in,
        bundled skills, bundled workflows, your commands (+ per-namespace
        sub-groups), one group per plugin, project commands.
    """
    groups: list[CommandGroup] = []

    scraped = _load_scraped_commands()
    builtin = [c for c in scraped if c.get("type") == "builtin"]
    skill = [c for c in scraped if c.get("type") == "skill"]
    workflow = [c for c in scraped if c.get("type") == "workflow"]
    if builtin:
        groups.append(CommandGroup(id="builtin", label="built-in commands", commands=builtin))
    if skill:
        groups.append(CommandGroup(id="skill", label="bundled skills", commands=skill))
    if workflow:
        groups.append(CommandGroup(id="workflow", label="bundled workflows", commands=workflow))

    groups.extend(_group_user_scope(discover_user_scope()))

    for plugin_name, commands in sorted(discover_plugin_scope().items()):
        groups.append(CommandGroup(
            id=f"plugin:{plugin_name}",
            label=f"{plugin_name} (plugin)",
            commands=commands,
        ))

    if project_path:
        project_commands = discover_project_scope(project_path)
        if project_commands:
            groups.append(CommandGroup(id="project", label="project commands", commands=project_commands))

    return groups


def command_groups_to_dict(groups: list[CommandGroup]) -> list[dict]:
    """
    Description: serialize CommandGroup/DiscoveredCommand dataclasses (and
        the scraped dicts mixed into `builtin`/`skill`/`workflow` groups)
        into a single plain-dict shape safe for FastAPI's JSON response.
    Inputs: groups (list[CommandGroup]).
    Output: list[dict] - each group as
        {"id": str, "label": str, "commands": [{"command", "args",
        "description", "type", "alias_of"}, ...]}.
    """
    def _cmd_to_dict(c) -> dict:
        if isinstance(c, DiscoveredCommand):
            return {
                "command": c.command,
                "args": c.args,
                "description": c.description,
                "type": c.type,
                "alias_of": c.alias_of,
            }
        # Already a plain dict from the scraped JSON.
        return {
            "command": c.get("command", ""),
            "args": c.get("args", ""),
            "description": c.get("description", ""),
            "type": c.get("type", "builtin"),
            "alias_of": c.get("alias_of"),
        }

    return [
        {"id": g.id, "label": g.label, "commands": [_cmd_to_dict(c) for c in g.commands]}
        for g in groups
    ]
