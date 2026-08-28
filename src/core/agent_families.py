"""The agent-family registry: one table, no if/elif chains.

feat/universal-wrappers - wrappers used to be a claude-only concept. A
wrapper now declares which COMMAND FAMILY it wraps (``AgentWrapper.family``),
and one settings screen administers every family. Adding a sixth family must
be a DATA addition to ``AGENT_FAMILIES`` below and nothing else: no new
branch anywhere, no new key in a dict literal that someone forgets to
update. Everything downstream (resolution, the settings summary, the UI
grouping, wrapper-id validation) iterates this tuple.

The old shape this replaces was a dict literal inside
``Settings.get_agent_command``::

    {"codex": agents.codex_command, "hermes": agents.hermes_command, ...}[normalized]

which had to be edited in lockstep with ``RESERVED_AGENT_TYPES``, with
``AgentsConfig``'s fields, and with the settings summary. Three places, no
compiler to catch a miss.

FALLBACK RENDERING, and why it is a per-family column rather than a branch
-------------------------------------------------------------------------
A family with no wrappers falls back to its static ``agents.<name>_command``
string, exactly as ``claude_command`` did before this change. But the two
fallbacks are NOT rendered the same way, and that difference predates this
module:

* ``claude`` wraps its command in ``zsh -c 'source ~/.zshrc ...; <cmd>'``
  because a claude launch commonly names a shell FUNCTION (``cld``) that
  only exists once ``~/.zshrc`` has been sourced, and tmux's spawned pane
  shell is non-interactive.
* ``codex`` / ``hermes`` / ``openclaw`` source the rc for the SAME
  reason claude does: they name a binary the user installed themselves,
  and a version manager (nvm, asdf, mise) puts its shims on PATH from an
  rc file. The tmux pane shell reads no rc, so a bare ``codex`` is
  "command not found".
* ``shell`` returns its command RAW. ``shell_command`` is ``$SHELL -i``,
  an INTERACTIVE shell, which sources the rc itself; wrapping it would
  source the rc twice and hard-code zsh as the launcher for the one
  family whose entire point is honouring ``$SHELL``.

CORRECTED 2026-08-26, and the original reasoning is worth keeping because
it is the trap. This section used to say the other four returned their
command raw "because that is what they did before this change and a
behaviour change there would be a regression, not a generalization."
Preserving prior behaviour preserved a defect. The tell was already in
the tree: ``make_tool_last_resort`` wraps its script in ``rc_prefixed``
unconditionally, so the two paths disagreed, and they disagreed in the
worse direction -

    codex_command = ""       -> last resort  -> rc-sourced   -> WORKS
    codex_command = "codex"  -> static path  -> raw          -> BROKEN

``AgentsConfig.codex_command`` defaults to ``"codex"``, so configuring
NOTHING worked and shipping the default did not. When two paths to the
same launch disagree about the environment they need, one of them is
wrong; do not assume it is the newer one.

So ``sources_zshrc`` is a column in the table, not a conditional in the
resolver.

Every family has a LAST-RESORT rendering, used when it has neither
wrappers nor a non-empty static command. That is one more column
(``last_resort``), still data. ``claude``'s is the historical
``cld``/``cldor`` hardcoded fallback; the other four are built by
``src/core/agent_last_resort.py``. The column used to be ``None`` for the
other four, which meant ``render_static_command`` returned the EMPTY
STRING and the launch silently produced a pane that exited at once. Read
that module's docstring before changing any of this: a ``None`` here is
not a neutral default, it is an empty command.

RESERVED NAMES, and the agent_type disambiguation this module encodes
--------------------------------------------------------------------
``Session.agent_type`` stores EITHER a wrapper id (``"cld"``) OR a bare
family name (``"shell"``). ``resolve_agent_type`` is the single place that
disambiguates the two, and the ORDER is load-bearing for backward
compatibility. See its docstring.

``RESERVED_FAMILY_NAMES`` is deliberately NOT every family name: it excludes
``claude``. A config seeded by the v0->v1 migration can contain a wrapper
whose id is literally ``"claude"`` (``build_seed_wrappers`` authors one), and
that wrapper must stay reachable by id. Reserving the name would have made
``agent_type="claude"`` resolve to the family default instead of to that
specific wrapper, silently changing which script runs on a machine whose
default wrapper is something else. See ``resolve_agent_type``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from src.core.agent_last_resort import (
    make_tool_last_resort,
    render_shell_last_resort,
)
from src.core.shell_init import rc_prefixed

# The default family for any wrapper that does not declare one, and for any
# agent_type that matches neither a reserved family name nor a wrapper id.
# "claude" because every wrapper that existed before this feature was, by
# construction, a claude wrapper.
DEFAULT_FAMILY = "claude"


def _render_claude_last_resort(model: Optional[str]) -> str:
    """The historical hardcoded claude fallback, preserved byte-for-byte.

    Description: reached only when the claude family has NO wrappers and an
      empty ``agents.claude_command``. Predates both wrappers and this
      module; kept verbatim so an untouched pre-wrappers install behaves
      identically after upgrading.
    Inputs: model (str | None) - OpenRouter model id; selects cldor over cld.
    Output: str - a complete ``zsh -c ...`` shell string for tmux.
    """
    if model:
        return rc_prefixed(f"cldor {shlex.quote(model)}")
    return rc_prefixed("cld")


@dataclass(frozen=True)
class AgentFamily:
    """One command family a wrapper can wrap.

    - ``name``: the family key. Stored in ``AgentWrapper.family`` and, for
      reserved families, usable directly as an ``agent_type``.
    - ``label``: lowercase UI heading for this family's group.
    - ``command_field``: the ``AgentsConfig`` attribute holding this
      family's static fallback command (e.g. ``"claude_command"``).
    - ``sources_zshrc``: whether the static fallback is wrapped in
      ``zsh -c 'source ~/.zshrc ...'``. See the module docstring.
    - ``reserved``: whether the family NAME is refused as a wrapper id and
      resolved as a bare family before any wrapper-id lookup. False only
      for ``claude``, for the backward-compatibility reason in the module
      docstring.
    - ``needs_model``: whether a launch is IMPOSSIBLE without a model id.
      True only for ``local``: ``cldl`` addresses one specific model on the
      LM Studio server and has no meaningful default, so launching it bare
      would start a pane that either errors or silently picks something the
      user did not choose. A family with this set is REFUSED rather than
      downgraded when no model is given - see
      ``Settings.get_agent_command``. It also changes how the picker
      behaves: such a family leads to a model step instead of launching
      on Enter.
    - ``pickable``: whether this family may be offered as a PINNED,
      modelless row in the launch picker when it has no wrappers of its
      own. A reserved family is launchable by name over the API
      (``agent_type: "codex"``) but, before this column, was unreachable
      from the UI unless the user first authored a wrapper for it - so
      the picker and the API disagreed about what could be launched.
      ``shell`` is the one False: it is not an agent CLI and it already
      has its own entry point, the "new console" FAB, which posts
      ``agent_type: 'shell'`` directly. Offering it twice would be two
      controls for one action. ``claude`` is True but reaches the picker
      through its wrappers rather than as a pinned row; see
      ``ProviderGroups.buildWrapperItems``.
    - ``description``: one lowercase line describing what the family runs,
      used by the settings screen's advanced legacy-command row.
    - ``last_resort``: renderer used when the family has neither wrappers
      nor a non-empty static command. A column rather than a branch so a
      future family can bring its own without touching the resolver.
      MUST NOT be ``None`` for a family that ships: ``None`` makes
      ``render_static_command`` return the empty string, which tmux turns
      into a pane that exits immediately with nothing on screen. A new
      family owes a renderer here in the same edit that adds it, and
      ``tests/test_agent_last_resort.py`` fails the whole table if one is
      missing.
    """

    name: str
    label: str
    command_field: str
    sources_zshrc: bool
    reserved: bool
    pickable: bool
    description: str
    needs_model: bool = False
    last_resort: Optional[Callable[[Optional[str]], str]] = None


# THE TABLE. Adding a family is an entry here plus the matching
# ``<name>_command`` field on ``AgentsConfig``; nothing else changes.
AGENT_FAMILIES: Tuple[AgentFamily, ...] = (
    AgentFamily(
        name="claude",
        label="claude",
        command_field="claude_command",
        sources_zshrc=True,
        reserved=False,
        pickable=True,
        description="the single command every claude session runs.",
        last_resort=_render_claude_last_resort,
    ),
    AgentFamily(
        name="codex",
        label="codex",
        command_field="codex_command",
        sources_zshrc=True,
        reserved=True,
        pickable=True,
        description="the single command every codex session runs.",
        last_resort=make_tool_last_resort("codex", "codex", "codex", "codex_command"),
    ),
    AgentFamily(
        name="hermes",
        label="hermes",
        command_field="hermes_command",
        sources_zshrc=True,
        reserved=True,
        pickable=True,
        description="the single command every hermes session runs.",
        last_resort=make_tool_last_resort("hermes", "hermes", "hermes", "hermes_command"),
    ),
    AgentFamily(
        name="openclaw",
        label="openclaw",
        command_field="openclaw_command",
        sources_zshrc=True,
        reserved=True,
        pickable=True,
        description="the single command every openclaw session runs.",
        last_resort=make_tool_last_resort(
            "openclaw", "openclaw tui", "openclaw", "openclaw_command"
        ),
    ),
    AgentFamily(
        name="local",
        label="local (lm studio)",
        command_field="local_command",
        sources_zshrc=True,
        reserved=True,
        pickable=True,
        needs_model=True,
        description="the single command every local-model session runs.",
        last_resort=make_tool_last_resort("cldl", "cldl", "local", "local_command"),
    ),
    AgentFamily(
        name="shell",
        label="shell",
        command_field="shell_command",
        # NOT rc-sourced, and this is a decision rather than an omission:
        # shell_command is "$SHELL -i", an INTERACTIVE shell, which reads
        # the user's rc itself. Wrapping it would source the rc twice and
        # hard-code zsh as the launcher for the one family whose whole
        # point is honouring whatever $SHELL the user actually has.
        sources_zshrc=False,
        reserved=True,
        pickable=False,
        description="the single command every plain console session runs.",
        last_resort=render_shell_last_resort,
    ),
)

AGENT_FAMILY_BY_NAME: Dict[str, AgentFamily] = {f.name: f for f in AGENT_FAMILIES}

AGENT_FAMILY_NAMES: Tuple[str, ...] = tuple(f.name for f in AGENT_FAMILIES)

# Family names that are NEVER looked up as a wrapper id. Historically this
# was ``RESERVED_AGENT_TYPES`` in src/config.py, with exactly this
# membership; derived from the table now so the two can never drift.
RESERVED_FAMILY_NAMES = frozenset(f.name for f in AGENT_FAMILIES if f.reserved)


def is_valid_family(name: str) -> bool:
    """Whether ``name`` is a known agent family.

    Inputs: name (str) - candidate family name.
    Output: bool.
    """
    return name in AGENT_FAMILY_BY_NAME


def get_family(name: Optional[str]) -> AgentFamily:
    """Look up a family for LAUNCHING a session. Never returns "I don't know".

    LAUNCH-TIME RESOLVER - do not use this for display. Something has to
    actually run when a session is launched, so an unresolvable name MUST
    fall back to a real, runnable family rather than raising or returning
    nothing. That fallback is correct here and wrong for a status pill: a
    pill built from this function's return value cannot be told apart from
    a pill for a session that was genuinely launched as ``claude``, which
    is exactly the bug ``resolve_family_for_display`` (below) exists to
    fix. See that function's docstring, and the two paired tests in
    ``tests/test_agent_family_display.py`` that pin this split apart.

    Description: never raises. An unknown or missing name resolves to the
      claude family, matching the pre-existing resolver behaviour where an
      unrecognised ``agent_type`` fell through to the claude path rather
      than failing the launch.
    Inputs: name (str | None).
    Output: AgentFamily.
    Example: get_family("shell").command_field -> "shell_command"
    """
    return AGENT_FAMILY_BY_NAME.get(name or "", AGENT_FAMILY_BY_NAME[DEFAULT_FAMILY])


# The DISPLAY-time counterpart, ``resolve_family_for_display``, lives in
# ``src/core/agent_family_display.py`` - a separate module so this file
# stays under the repo's 500-line-per-file rule, and so the two resolvers'
# opposite fallback contracts (always-runnable vs never-guessed) cannot be
# accidentally merged by editing them side by side in one file. Import it
# from there; nothing here re-exports it.


def wrappers_for_family(wrappers: List, family_name: str) -> List:
    """Filter a wrapper list down to one family, preserving order.

    Description: a wrapper with no ``family`` attribute at all (a raw dict
      from a config read before validation) is treated as ``DEFAULT_FAMILY``
      - the same additive-default rule ``AgentWrapper.family`` applies.
    Inputs:
      wrappers (list) - AgentWrapper objects or AgentWrapper-shaped dicts.
      family_name (str) - the family to keep.
    Output: list - the subset, in the original order.
    Example: wrappers_for_family([cld, codex_w], "claude") -> [cld]
    """
    kept = []
    for w in wrappers:
        if isinstance(w, dict):
            name = w.get("family") or DEFAULT_FAMILY
        else:
            name = getattr(w, "family", None) or DEFAULT_FAMILY
        if name == family_name:
            kept.append(w)
    return kept


def resolve_agent_type(agent_type: Optional[str], wrappers: List) -> Tuple[AgentFamily, Optional[object]]:
    """Disambiguate an ``agent_type`` into (family, explicit wrapper or None).

    LAUNCH-TIME RESOLVER - like ``get_family``, this always returns a real,
    runnable family (never "unknown") because a launch has to run
    something. Do not use it to decide what a status pill shows; use
    ``resolve_family_for_display`` for that. The two are intentionally
    separate functions for separate purposes - do not merge them.

    Description: ``Session.agent_type`` stores either a WRAPPER ID or a bare
      FAMILY NAME, and has done since before wrappers were per-family. This
      is the single place that tells them apart. The order below is chosen
      to keep every pre-existing config resolving to exactly what it
      resolved to before:

      1. A RESERVED family name (``codex``, ``hermes``, ``openclaw``,
         ``shell``) is that family, with NO explicit wrapper - never
         looked up as a wrapper id. This is why a historical session whose
         ``agent_type`` is ``"shell"`` still launches the shell family:
         reserved names were already checked first, and wrapper ids matching
         them are refused at creation time (``Settings.add_wrapper``).
         The family's own wrappers are then consulted by the caller, which
         is the new behaviour - and a no-op for every existing config,
         because no existing config has a non-claude wrapper.
      2. Otherwise a WRAPPER ID match wins, across every family. This keeps
         ``agent_type="claude"`` resolving to a wrapper whose id is
         literally ``"claude"`` (the v0->v1 migration authors one) rather
         than to the claude family's default wrapper, which could be a
         different script entirely.
      3. Otherwise the claude family with no explicit wrapper: covers
         ``"claude"``, ``None``, empty string, and any unknown string.
         Unchanged from the pre-existing resolver.

    Inputs:
      agent_type (str | None) - the stored/requested type; case-insensitive.
      wrappers (list) - every configured wrapper, all families.
    Output: (AgentFamily, AgentWrapper | None) - the family to launch, and
      the specific wrapper the caller named, if it named one.
    Example: resolve_agent_type("shell", [...]) -> (shell family, None)
    """
    normalized = (agent_type or DEFAULT_FAMILY).lower()

    if normalized in RESERVED_FAMILY_NAMES:
        return AGENT_FAMILY_BY_NAME[normalized], None

    for w in wrappers:
        wid = w.get("id") if isinstance(w, dict) else getattr(w, "id", None)
        if wid == normalized:
            family_name = (
                w.get("family") if isinstance(w, dict) else getattr(w, "family", None)
            ) or DEFAULT_FAMILY
            return get_family(family_name), w

    return AGENT_FAMILY_BY_NAME[DEFAULT_FAMILY], None


def build_family_summaries(agents, resolve_command: Callable[[str], str]) -> List[dict]:
    """Serialize the registry with each family's live state, for the UI.

    Description: one entry per ``AGENT_FAMILIES`` row, carrying what the
      settings screen needs to render a family group and its collapsed
      advanced legacy-command row: the static command as configured,
      whether that command is currently IN USE (i.e. the family has no
      wrappers, so nothing takes precedence over it), and the shell string
      this family would actually launch right now. Built server-side so
      the client never re-derives the wrapper-beats-static-command
      precedence rule - the same reasoning behind the pre-existing
      ``effective_claude_command``.
    Inputs:
      agents (AgentsConfig) - the loaded agents block; read via getattr so
        this module never imports src.config (which imports this one).
      resolve_command (Callable[[str], str]) - normally
        ``Settings.get_agent_command``; injected to keep the registry free
        of any dependency on Settings.
    Output: list[dict] - keys ``name``, ``label``, ``reserved``,
      ``pickable``, ``command_field``, ``command``, ``description``,
      ``wrapper_count``, ``in_use``, ``effective_command``.
    """
    wrappers = getattr(agents, "wrappers", []) or []
    summaries: List[dict] = []
    for family in AGENT_FAMILIES:
        count = len(wrappers_for_family(wrappers, family.name))
        summaries.append({
            "name": family.name,
            "label": family.label,
            # Both shipped so the LAUNCH PICKER can decide what to offer
            # without hardcoding a family list client-side, which is the
            # same reason the registry is shipped at all. ``reserved``
            # means the name is directly usable as an ``agent_type``;
            # ``pickable`` means it may be offered as a pinned modelless
            # row when it has no wrappers. See AgentFamily's docstring.
            "reserved": family.reserved,
            "pickable": family.pickable,
            "needs_model": family.needs_model,
            "command_field": family.command_field,
            "command": getattr(agents, family.command_field, "") or "",
            "description": family.description,
            "wrapper_count": count,
            # The legacy command runs only when this family has no
            # wrappers at all; any wrapper takes precedence.
            "in_use": count == 0,
            "effective_command": _effective_command(resolve_command, family),
        })
    return summaries


def _effective_command(resolve_command, family: AgentFamily) -> str:
    """Render a family's live command for display, or say why it cannot.

    Description: a ``needs_model`` family has no command to show until a
      model is chosen - ``get_agent_command`` REFUSES rather than render a
      bare one, which is correct for a launch and fatal for a summary that
      renders every family. So the refusal is caught and turned into a
      sentence the settings screen can print.

      It returns a NAMED third state rather than an empty string. A blank
      cell reads as "no command configured", which is a different and
      wrong claim: the command is configured, it simply cannot be
      completed without a choice the user has not made yet.
    Inputs: resolve_command (Callable[[str], str]); family (AgentFamily).
    Output: str - the rendered command, or a reason it is unavailable.
    Example: _effective_command(fn, get_family("local"))
    """
    try:
        return resolve_command(family.name)
    except ValueError as exc:
        return f"(needs a model: {exc})"
    except Exception as exc:  # noqa: BLE001 - a summary must never 500
        return f"(could not resolve: {exc})"


def render_static_command(
    family: AgentFamily,
    command: str,
    model: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> str:
    """Render a family's static fallback command into a tmux-ready string.

    Description: applies the family's ``sources_zshrc`` column, and its
      ``last_resort`` behaviour when ``command`` is empty. See the module
      docstring for why the two shapes differ.
    Inputs:
      family (AgentFamily) - the family being launched.
      command (str) - the configured static command, possibly empty.
      model (str | None) - only consulted by the claude last-resort path.
      extra_args (list[str] | None) - further arguments appended to the
        command BEFORE any rc wrapping, each shlex-quoted independently.
        Used by the FORK path for ``--resume <uuid> --fork-session``.
        Appended before quoting on purpose: doing string surgery on an
        already-quoted ``zsh -c '...'`` is how quoting bugs get made.
        Ignored on the last-resort path, which takes no arguments.
    Output: str - a single shell string for ``tmux new-session ... <cmd>``.
    Example: render_static_command(get_family("shell"), "$SHELL -i") -> "$SHELL -i"
    """
    if command and command.strip():
        rendered = command
        tail = [a for a in (extra_args or []) if a]
        if tail:
            rendered = rendered + " " + " ".join(shlex.quote(a) for a in tail)
        if family.sources_zshrc:
            return rc_prefixed(rendered)
        return rendered
    if family.last_resort is not None:
        return family.last_resort(model)
    return command
