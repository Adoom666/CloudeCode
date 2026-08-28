"""User-defined launch wrappers for the "claude" agent family.

feat/launch-wrappers - replaces the hardcoded ``cld`` / ``cldor`` zsh
functions in ``Settings.get_agent_command()`` with a user-editable list of
named wrappers stored in ``config.json`` (``agents.wrappers``). A wrapper's
``script`` is arbitrary multi-line shell text - anything from a single
command (``"claude --dangerously-skip-permissions"``) up to a full function
definition copy-pasted verbatim, including the author's real ``cld``:

    cld() (
        local claude_token
        claude_token="$(security find-generic-password ...)" || { ... }
        export CLAUDE_CODE_OAUTH_TOKEN="$claude_token"
        ...
        command claude --dangerously-skip-permissions "$@"
    )

Representation choice: ``script`` is stored as a single JSON string with
embedded ``\\n`` (not an array of lines). Every other multi-line-shaped
value already in this codebase (``AgentsConfig.*_command``,
``ProvidersConfig.models`` entries) is a plain string, and a script written
via the settings-panel textarea round-trips through ``json.dump(..., indent=2)``
with zero extra logic. An array-of-lines shape would read slightly better
in a hand-edited config.json but forces a join/split step at every read and
write site for a benefit that only matters if someone is hand-editing
30-line shell functions directly in JSON - the UI textarea is the intended
editing surface, and it preserves whitespace/indentation exactly either way.

Execution model (see ``render_wrapper_invocation``):
  1. The script's exact bytes are written to a stable per-wrapper file
     (never embedded into a shell-quoted string - sidesteps every quoting
     hazard a pasted function might contain: single quotes, ``$(...)``,
     ``||`` blocks, nested double quotes).
  2. That file is ``source``-d from inside the same ``zsh -c`` wrapper the
     legacy ``claude_command`` / ``cld`` path already uses (so ``~/.zshrc``
     is loaded first - same reason a bare ``cld`` would otherwise be
     "command not found" under tmux's non-interactive pane shell).
  3. If the script is a bare runnable command/one-liner (``entry`` unset),
     sourcing it is enough - this is the migrated-``claude_command`` case
     and the thin ``cld "$@"`` / ``cldor "$@"`` migration-seeded wrappers
     (see ``src/core/config_migration.py``), which merely invoke the
     function the user's own ``~/.zshrc`` already defines.
  4. If the script instead *defines* a function (``entry`` set to that
     function's name, e.g. ``"cld"``), it's invoked explicitly after the
     source step: ``source <file> "$@"; <entry> "$@"``.
  ``model`` (the OpenRouter model id, when set) is forwarded as the OUTER
  zsh -c's own positional parameter ``$1`` (``zsh -c '<script>' _ <model>``),
  which both the ``source <file> "$@"`` step and the ``<entry> "$@"`` step
  re-forward unchanged - exactly mirroring how the real ``cldor`` consumes
  ``$1`` as its model argument and forwards the rest via ``"$@"``. This
  requires no placeholder syntax inside the script at all.
  The ``( ... )`` subshell form the author's real functions use is
  preserved verbatim in the sourced file, so env exports/unsets inside a
  wrapper (``CLAUDE_CODE_OAUTH_TOKEN``, ``ANTHROPIC_BASE_URL``, ...) never
  leak into the ``zsh -c`` shell that invoked it - sourcing only *defines*
  the function; the subshell isolation happens when it's *called*.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from src.core.agent_families import (
    AGENT_FAMILY_NAMES,
    DEFAULT_FAMILY,
    is_valid_family,
)
from src.core.agent_last_resort import example_wrapper
from src.core.shell_init import rc_prefixed

# Filesystem-safe, stable identifier: used both as the wrapper's dict key
# and as the on-disk script filename (``<id>.zsh``). Lowercase start avoids
# any ambiguity with shell option-like leading characters.
WRAPPER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_WRAPPER_ID_RE = re.compile(WRAPPER_ID_PATTERN)


def is_valid_wrapper_id(v: str) -> bool:
    """Single source of truth for wrapper-id validation.

    Inputs: v (str) - candidate wrapper id.
    Output: bool - True if v matches WRAPPER_ID_PATTERN exactly.
    """
    return bool(_WRAPPER_ID_RE.fullmatch(v))


# The ONE heuristic used to derive ``accepts_model`` for wrappers that
# predate the field (see ``derive_accepts_model`` and the v1->v2 step in
# src/core/config_migration.py). Deliberately a single named constant
# rather than a hardcoded list of any particular user's wrapper ids:
# library code must not know that one person's OpenRouter wrapper happens
# to be called "cldor". A wrapper is guessed to take a model id when its
# own human-authored text says so - the migration-seeded OpenRouter
# wrapper's label is "cldor (openrouter)" and its description says "model
# as first argument", while the subscription wrappers ("claude", "cld")
# mention neither.
#
# This runs EXACTLY ONCE, during the v1->v2 migration. After that the
# stored boolean is authoritative and user-editable, so a wrong guess is
# a checkbox away from fixed and never re-guessed.
MODEL_CAPABLE_TEXT_RE = re.compile(r"openrouter|\bmodel\b", re.IGNORECASE)


def derive_accepts_model(wrapper: dict) -> bool:
    """Guess whether a pre-``accepts_model`` wrapper takes a model id.

    Description: applies ``MODEL_CAPABLE_TEXT_RE`` to the wrapper's own
      human-authored text (label, description, script, entry). Used only
      to give existing configs a sensible starting value during the
      v1->v2 migration; never consulted at launch time.
    Inputs: wrapper (dict) - an AgentWrapper-shaped dict, possibly
      missing any of the optional keys.
    Output: bool - True if the wrapper looks model-capable.
    Example: derive_accepts_model({"label": "cldor (openrouter)"}) -> True
    """
    haystack = " ".join(
        str(wrapper.get(key) or "")
        for key in ("label", "description", "script", "entry")
    )
    return bool(MODEL_CAPABLE_TEXT_RE.search(haystack))


class AgentWrapper(BaseModel):
    """One user-defined launch wrapper for the "claude" agent family.

    - ``id``: stable identifier. Doubles as the ``agent_type`` value a
      caller passes to launch through this specific wrapper (see
      ``Settings.get_agent_command``) and as the on-disk script filename.
      NEVER rename one: ``Session.agent_type`` stores it, so a rename
      orphans every running and historical session launched through it.
    - ``family``: which command family this wrapper wraps - one of
      ``src/core/agent_families.AGENT_FAMILY_NAMES`` (claude, codex,
      hermes, openclaw, shell). Defaults to ``"claude"`` because every
      wrapper that existed before this field was, by construction, a
      claude wrapper; the v2->v3 migration writes that value explicitly.
      The family selects which static ``agents.<family>_command`` string
      is the fallback when the family has no wrappers at all.
    - ``label``: human-readable name shown in the UI.
    - ``script``: the full shell source - a plain command, or a complete
      function definition. Never validated for shell syntax (arbitrary
      user shell, same trust level as the pre-existing ``claude_command``
      field); written verbatim to disk, never interpolated into a quoted
      string.
    - ``entry``: function name to invoke after sourcing ``script``. Unset
      (None/empty) means ``script`` is directly runnable on its own - the
      common case for a migrated single command or a thin
      ``cld "$@"``-style forwarder to a function the user's own
      ``~/.zshrc`` defines. Set it when ``script`` is a function
      definition that needs an explicit call, e.g. ``entry="cld"`` for a
      pasted ``cld() ( ... )`` body.
    - ``description``: optional free-text note shown in the editor.
    - ``default``: at most one wrapper should carry this; the resolver
      falls back to the first wrapper in list order if none (or more than
      one, from a hand-edited config.json) is marked default.
    - ``accepts_model``: whether this wrapper consumes an OpenRouter model
      id as its first argument (the ``cldor`` shape). Model choice and
      wrapper choice are two INDEPENDENT axes; collapsing them into one
      flat picker list is what caused the regression this field fixes -
      a model chosen next to a wrapper that ignores models got forwarded
      to the DEFAULT wrapper, whose ``"$@"`` passed the model id through
      to ``claude`` as a PROMPT ARGUMENT. Defaults to ``False`` so a
      wrapper that says nothing about models is never offered one and
      behaviour for every existing wrapper is unchanged; the v1->v2
      migration seeds a derived value once (see ``derive_accepts_model``).
      ``Settings.get_agent_command`` drops the model outright when the
      resolved wrapper does not accept one, so the picker's rule is
      enforced server-side too, not just in the UI.

    SECURITY: there is deliberately no "secret" field. A wrapper is a
    shell command - secrets must never be pasted into ``script`` or
    ``description``. The correct pattern, which the author's own ``cld``
    demonstrates, is to read from the macOS Keychain at *run time* inside
    the script (``security find-generic-password ...``), so no credential
    ever passes through this app or lands in config.json.
    """

    id: str = Field(..., description="Stable id; also the on-disk script filename and the agent_type value")
    family: str = Field(
        DEFAULT_FAMILY,
        description="Which command family this wrapper wraps (see src/core/agent_families.py)",
    )
    label: str = Field(..., description="Human-readable name shown in the UI")
    script: str = Field(..., description="Full shell source: a command or a function definition")
    entry: Optional[str] = Field(
        None,
        description="Function name to call after sourcing script; unset = script is directly runnable",
    )
    description: Optional[str] = Field(None, description="Optional free-text note")
    default: bool = Field(False, description="Preferred wrapper when none is explicitly selected")
    accepts_model: bool = Field(
        False,
        description="Whether this wrapper consumes an OpenRouter model id as its first argument",
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not is_valid_wrapper_id(v):
            raise ValueError(f"wrapper id must match {WRAPPER_ID_PATTERN}")
        return v

    @field_validator("family", mode="before")
    @classmethod
    def _validate_family(cls, v: Optional[str]) -> str:
        """Coerce a missing/blank family to the default, reject an unknown one.

        Description: ``None`` and ``""`` become ``DEFAULT_FAMILY``
          ("claude"), so a wrapper dict written before this field existed
          validates unchanged even if it reaches the model without going
          through the v2->v3 migration first (a hand-edited config.json, a
          direct API POST from an older client). An unknown NON-blank name
          is a real error and is refused rather than silently coerced,
          because silently relocating a wrapper into the wrong family
          would launch the wrong binary.
        Inputs: v (str | None) - raw family value from config or request.
        Output: str - a valid family name.
        Raises: ValueError - v is a non-blank unknown family name.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return DEFAULT_FAMILY
        name = str(v).strip().lower()
        if not is_valid_family(name):
            raise ValueError(
                f"unknown agent family '{v}'; expected one of {', '.join(AGENT_FAMILY_NAMES)}"
            )
        return name

    @field_validator("script")
    @classmethod
    def _validate_script_nonblank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("wrapper script must not be blank")
        return v


def find_wrapper(wrappers: List[AgentWrapper], wrapper_id: str) -> Optional[AgentWrapper]:
    """Look up a wrapper by id.

    Inputs: wrappers (list[AgentWrapper]); wrapper_id (str).
    Output: AgentWrapper | None.
    """
    for w in wrappers:
        if w.id == wrapper_id:
            return w
    return None


def default_wrapper(wrappers: List[AgentWrapper]) -> Optional[AgentWrapper]:
    """Resolve the preferred wrapper when none is explicitly selected.

    Description: the wrapper with ``default=True`` wins; if none (or, from
      a hand-edited config.json, more than one) is marked default, the
      first wrapper in list order is used. Empty list -> None.
    Inputs: wrappers (list[AgentWrapper]).
    Output: AgentWrapper | None.
    Example: default_wrapper([w_claude, w_cld]) -> w_claude (if
      w_claude.default is True).
    """
    if not wrappers:
        return None
    for w in wrappers:
        if w.default:
            return w
    return wrappers[0]


def wrapper_scripts_dir(log_directory: str) -> Path:
    """Directory the resolved wrapper script files live in.

    Description: co-located under the app's own log directory (already a
      writable, per-install path - see ``Settings.get_log_dir``) rather
      than a shared /tmp, so two installs never collide and cleanup is
      implicit (small text files, never grow, never need sweeping).
    Inputs: log_directory (str) - Settings.log_directory (pre-expansion OK).
    Output: Path - directory (not guaranteed to exist yet).
    """
    return Path(log_directory).expanduser() / "agent_wrapper_scripts"


def _write_script_file(wrapper: AgentWrapper, scripts_dir: Path) -> Path:
    """Write ``wrapper.script`` verbatim to its stable on-disk path.

    Description: rewritten on every resolution call (cheap - a few KB at
      most) so an edited-but-not-yet-restarted config always sources the
      current script text. Bytes are written EXACTLY as stored - no
      escaping, no line-ending normalization - so a round trip through
      config.json -> disk is byte-for-byte faithful to what the user
      pasted, indentation included.
    Inputs: wrapper (AgentWrapper); scripts_dir (Path).
    Output: Path - the file path written.
    """
    scripts_dir.mkdir(parents=True, exist_ok=True)
    path = scripts_dir / f"{wrapper.id}.zsh"
    path.write_text(wrapper.script)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort; not a secret file, just tidy permissions
    return path


def render_wrapper_invocation(
    wrapper: AgentWrapper,
    scripts_dir: Path,
    model: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> str:
    """Build the single shell-string command for launching this wrapper.

    Description: see the module docstring's "Execution model" section for
      the full reasoning. Returns exactly one flat string - the shape
      every other branch of ``Settings.get_agent_command`` already
      returns, handed straight to the tmux backend's
      ``new-session ... <command>``.
    Inputs:
      wrapper (AgentWrapper) - the wrapper to launch.
      scripts_dir (Path) - directory to write the resolved script file
        into (see ``wrapper_scripts_dir``).
      model (str | None) - forwarded as $1 to the sourced script / entry
        call; None means no model argument at all (matches plain ``cld``
        with no args).
      extra_args (list[str] | None) - further positional arguments after
        the model, forwarded through the same ``"$@"`` chain. Used by the
        FORK path to pass ``--resume <uuid> --fork-session``, so a fork
        launches through the user's own wrapper (and therefore their own
        auth setup) rather than around it. Each element is shlex-quoted
        independently, so a value can never break out of the layer it
        was placed in. Note the ``_`` throwaway $0 is emitted whenever
        there is ANY argument, model or not - without it the first real
        argument would silently become $0 and never reach "$@".
    Output: str - e.g.
      ``zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; source '"'"'/path/cld.zsh'"'"' "$@"; cld "$@"' _ some-model``
    Example: render_wrapper_invocation(cld_wrapper, dir, None) ->
      a zsh -c string that sources the cld.zsh file and calls cld with no args.
    """
    path = _write_script_file(wrapper, scripts_dir)
    inner = f"source {shlex.quote(str(path))} \"$@\""
    if wrapper.entry and wrapper.entry.strip():
        inner += f"; {shlex.quote(wrapper.entry.strip())} \"$@\""
    outer = rc_prefixed(inner)
    # `_` is a throwaway $0; everything after it starts at $1 and is
    # forwarded through every "$@" in `inner` above (source's own
    # arg-forwarding, then the entry call) - see module docstring point 3.
    # The $0 slot is emitted whenever there is ANY argument: without it the
    # first real argument lands in $0, which "$@" does not include, and it
    # would vanish with no error.
    positional = [model] if model else []
    positional += [a for a in (extra_args or []) if a]
    if positional:
        outer += " _ " + " ".join(shlex.quote(a) for a in positional)
    return outer


# ---------------------------------------------------------------------------
# Offered example wrappers - NOT auto-installed anywhere. Served only via
# GET /api/v1/agents/wrappers/examples for a user to explicitly import into
# their own config.json (see the settings-panel "import example" action).
# EXAMPLE_WRAPPER_CLD and EXAMPLE_WRAPPER_CLDOR are both the author's REAL
# function bodies, as supplied for this feature, with one deliberate
# deviation from verbatim: EXAMPLE_WRAPPER_CLDOR originally invoked claude
# via the hardcoded absolute path "$HOME/.local/bin/claude", which only
# resolves on the machine it was authored on. It now resolves "claude" from
# PATH and fails with a named message when absent, instead of a raw shell
# "no such file or directory".
#
# That PATH resolution deliberately steps past a shell ALIAS or FUNCTION
# named "claude" before looking up the binary. A bare `command -v claude`
# reports the alias DEFINITION TEXT, not a path, whenever such an alias is
# defined - measured 2026-08-20 on the author's Mac mini, where
# `alias claude=...` makes `command -v claude` return
# "alias claude='security unlock-keychain ... && /opt/homebrew/bin/claude'".
# The unalias/unset run inside a command-substitution subshell, so the
# caller's own alias and function are left intact. Nothing else in either
# body was changed.
# ---------------------------------------------------------------------------

EXAMPLE_WRAPPER_CLD = """cld() (
    local claude_token
    claude_token="$(security find-generic-password -a "$USER" -s "claude-cld-oauth" -w 2>/dev/null)" || {
        echo "Claude OAuth token not found in macOS Keychain."
        echo "Run: claude setup-token"
        return 1
    }
    unset CLAUDE_CONFIG_DIR
    export CLAUDE_CODE_OAUTH_TOKEN="$claude_token"
    unset OPENROUTER_API_KEY
    unset ANTHROPIC_BASE_URL
    unset ANTHROPIC_AUTH_TOKEN
    unset ANTHROPIC_API_KEY
    unset CLAUDE_CODE_USE_BEDROCK
    unset CLAUDE_CODE_USE_VERTEX
    unset CLAUDE_CODE_USE_FOUNDRY
    unset ANTHROPIC_MODEL
    unset ANTHROPIC_DEFAULT_OPUS_MODEL
    unset ANTHROPIC_DEFAULT_SONNET_MODEL
    unset ANTHROPIC_DEFAULT_HAIKU_MODEL
    unset CLAUDE_CODE_SUBAGENT_MODEL
    command claude --dangerously-skip-permissions "$@"
)"""

EXAMPLE_WRAPPER_CLDOR = """cldor() (
  local openrouter_key
  local selected_model=""
  local claude_bin

  openrouter_key="$(
    security find-generic-password \\
      -a "$USER" \\
      -s "claude-cldor-openrouter" \\
      -w 2>/dev/null
  )" || {
    echo "OpenRouter API key not found in macOS Keychain."
    return 1
  }

  # Resolve the real claude binary, stepping past any shell-level alias or
  # function of the same name. A bare `command -v claude` reports the ALIAS
  # DEFINITION TEXT when an alias exists, which is not a path and is not
  # executable. The unalias/unset run inside this command substitution's own
  # subshell, so the caller's alias and function survive untouched.
  claude_bin="$(unalias claude 2>/dev/null; unset -f claude 2>/dev/null; command -v claude)"
  if [[ ! -x "$claude_bin" ]]; then
    echo "claude not found on PATH."
    return 1
  fi

  # Treat the first non-option argument as the model name
  if [[ $# -gt 0 && "$1" != -* ]]; then
    selected_model="$1"
    shift
  fi

  # Separate OpenRouter settings, sessions, plugins and history
  export CLAUDE_CONFIG_DIR="$HOME/.claude-cldor"

  # Prevent normal Claude subscription authentication from leaking in
  unset CLAUDE_CODE_OAUTH_TOKEN

  # Prevent cloud-provider routing
  unset CLAUDE_CODE_USE_BEDROCK
  unset CLAUDE_CODE_USE_VERTEX
  unset CLAUDE_CODE_USE_FOUNDRY

  # OpenRouter connection
  export OPENROUTER_API_KEY="$openrouter_key"
  export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
  export ANTHROPIC_AUTH_TOKEN="$openrouter_key"

  # OpenRouter requires this to be explicitly blank
  export ANTHROPIC_API_KEY=""

  if [[ -n "$selected_model" ]]; then
    # Use the command-line model for all Claude Code model roles
    export ANTHROPIC_DEFAULT_OPUS_MODEL="$selected_model"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="$selected_model"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$selected_model"
    export CLAUDE_CODE_SUBAGENT_MODEL="$selected_model"

    echo "OpenRouter model: $selected_model"

    "$claude_bin" \\
      --dangerously-skip-permissions \\
      --model "$selected_model" \\
      "$@"
  else
    # Defaults when no model is supplied
    export ANTHROPIC_DEFAULT_OPUS_MODEL="~anthropic/claude-opus-latest"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="~anthropic/claude-sonnet-latest"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="~anthropic/claude-haiku-latest"
    export CLAUDE_CODE_SUBAGENT_MODEL="~anthropic/claude-opus-latest"

    "$claude_bin" \\
      --dangerously-skip-permissions \\
      "$@"
  fi
)"""

EXAMPLE_WRAPPERS: List[dict] = [
    {
        "id": "cld",
        "family": "claude",
        "label": "cld (subscription, keychain-backed)",
        "script": EXAMPLE_WRAPPER_CLD,
        "entry": "cld",
        "description": (
            "example - reads a Claude subscription OAuth token from the "
            "macOS Keychain at run time (entry 'claude setup-token' first) "
            "and clears every OpenRouter/Bedrock/Vertex env var so it can't "
            "accidentally inherit routing from a prior shell."
        ),
        "default": False,
        "accepts_model": False,
    },
    {
        "id": "cldor",
        "family": "claude",
        "label": "cldor (openrouter, keychain-backed)",
        "script": EXAMPLE_WRAPPER_CLDOR,
        "entry": "cldor",
        "description": (
            "example - takes a model id as its first argument, reads an "
            "OpenRouter API key from the macOS Keychain, and runs Claude "
            "Code isolated in its own CLAUDE_CONFIG_DIR so it never shares "
            "session/auth state with a subscription-mode launch."
        ),
        "default": False,
        "accepts_model": True,
    },
    # One offered example per NON-claude family. Before these existed the
    # example list was two entries, both claude, so the wrappers screen's
    # "import example" action had nothing to offer a codex/hermes/openclaw
    # or shell user at all. Built from the same guarded text as those
    # families' last resorts (src/core/agent_last_resort.py) so importing
    # one and editing it starts from a launch that already fails legibly.
    # These are NOT keychain-backed: the two claude examples above read a
    # credential at run time because Claude Code needs one, and none of
    # these tools does.
    example_wrapper("codex", "codex", "codex", "codex_command", "codex (guarded)"),
    example_wrapper("hermes", "hermes", "hermes", "hermes_command", "hermes (guarded)"),
    example_wrapper(
        "openclaw", "openclaw", "openclaw tui", "openclaw_command", "openclaw (guarded)"
    ),
    {
        "id": "cldl",
        "family": "local",
        "label": "cldl (lm studio)",
        # LM Studio speaks the Anthropic Messages API, so pointing Claude
        # Code at it is a ROUTING change and nothing more - no protocol
        # shim, no second client.
        #
        # The address comes from CLDL_HOST, which the app injects into the
        # tmux spawn environment from providers.local_host. It is NOT
        # interpolated into this script: a value that never enters shell
        # text cannot be quoted wrongly. The ${CLDL_HOST:?...} form fails
        # LOUDLY with a sentence rather than silently falling back to a
        # guessed address and connecting somewhere the user did not choose.
        #
        # The API key is a placeholder because LM Studio does not check it;
        # the SDK simply refuses to start without one. It is not a secret
        # and there is nothing here to keep out of a config file.
        "script": (
            'cldl() (\n'
            '    local host="${CLDL_HOST:?no LM Studio address: set '
            'providers.local_host in config.json}"\n'
            '    local model="${1:?cldl needs a model id as its first argument}"\n'
            # NO /v1 SUFFIX. The Anthropic SDK appends "/v1/messages"
            # itself, so a base URL ending in /v1 produces
            # /v1/v1/messages, which LM Studio answers with a 65-byte
            # {"error":"Unexpected endpoint or method"} - a 200 carrying
            # JSON that is not a Message. Claude Code reports that as
            # "empty or malformed response ... 0 stream events received",
            # which reads like a streaming or proxy fault and sends you
            # looking in entirely the wrong place. Found end to end
            # against a real LM Studio on 2026-08-26; every layer below
            # this line was working the whole time.
            '    export ANTHROPIC_BASE_URL="http://${host}"\n'
            '    export ANTHROPIC_API_KEY="lm-studio"\n'
            '    command claude --dangerously-skip-permissions '
            '--model "$model" "${@:2}"\n'
            ')'
        ),
        "entry": "cldl",
        "description": (
            "example - runs Claude Code against a local LM Studio server "
            "instead of the hosted API. Takes a model id as its first "
            "argument and reads the server address from CLDL_HOST, which "
            "the app sets from providers.local_host; it refuses to launch "
            "rather than guess either one."
        ),
        "default": False,
        "accepts_model": True,
    },
    {
        "id": "shell-default",
        "family": "shell",
        "label": "shell (interactive)",
        # No probe: there is no third-party binary, and the
        # ${SHELL:-/bin/sh} default means this can never render empty.
        "script": 'exec "${SHELL:-/bin/sh}" -i',
        "entry": None,
        "description": (
            "example - a plain interactive login-rc shell, the same thing "
            "agents.shell_command runs by default."
        ),
        "default": False,
        "accepts_model": False,
    },
]
