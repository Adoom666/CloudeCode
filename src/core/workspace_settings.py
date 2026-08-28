"""Global workspace settings: what a NEW terminal is born with.

WHAT THIS IS

Four global preferences the user asked for, and the validation that stops
a bad one from reaching disk:

    development_root   base directory for projects. Delivered to terminals
                       as ``CLOUDE_DEV_ROOT``.
    default_shell      which shell a new terminal runs. Delivered as
                       ``SHELL``, which is what ``agents.shell_command``'s
                       default ``$SHELL -i`` already expands.
    env                arbitrary NAME=value pairs injected on spawn.
    default_editor     the command that opens a file the app hands you.

WHERE THEY LIVE, AND WHY

``config.json``, as two ADDITIVE top-level blocks (``workspace`` and
``server_prefs``). Not the database, and the reason is the user's own:
"we can leave the config file for a user to revert backwards."

That property is real and measured, not hoped for. ``Settings`` is
declared ``extra="ignore"`` (src/config.py), so an OLDER build reading a
NEWER config.json ignores these keys instead of refusing to start; and
every config write path in this codebase is a raw-dict round trip, so
that older build PRESERVES the keys it does not understand rather than
dropping them on its next write. Additive scalar keys are therefore
downgrade-safe. The projects table moved to SQLite because it needed row
identity and reconciliation; a handful of global scalars needs neither.

No ``CURRENT_CONFIG_VERSION`` bump accompanies this. A migration step
exists to SEED a value an existing install could not otherwise reach, and
there is nothing to seed here: an absent ``workspace`` block deserializes
to the model's own defaults, which are all "unset". Materializing the key
would freeze that user's block against every future default for no gain -
the same reasoning ``_step_v3_to_v4`` gives for deliberately leaving an
absent ``common_slash_commands`` absent.

ENVIRONMENT VARIABLE NAMES ARE A SECURITY SURFACE, AND THE ANSWER IS NOT
A BLANKET BLOCK

Someone can type ``PATH``, ``LD_PRELOAD``, ``DYLD_INSERT_LIBRARIES`` or
``ANTHROPIC_API_KEY`` into this screen. The instinct is to block them.
That instinct is wrong here, and it is worth writing down why.

This product's entire function is spawning interactive shells that run
whatever the user types, as the user, on the user's own machine. An
environment variable confers no capability that the terminal one click
away does not already confer. A block on ``LD_PRELOAD`` stops nobody who
could simply type the command; what it reliably does stop is the app's
OWN documented wrapper mechanism, which legitimately sets ``ANTHROPIC_*``
(see src/core/agent_wrappers.py). Blocking that would break a real
workflow to prevent nothing.

So the policy has three tiers, and only one of them refuses:

  BLOCKED   ``CLOUDECODE_*``. Reserved for this app's own control
            channel - the per-session hook id, token and URL minted in
            SessionManager.get_env_for_spawn. The danger there is not
            privilege, it is SILENCE: shadowing one of those does not
            fail, it makes hook events route to the wrong session or
            nowhere, with no error anywhere. Refused at save time.
  WARNED    ``PATH``, ``HOME``, ``SHELL``, ``LD_*``, ``DYLD_*`` and any
            credential-shaped name. Saved, and named back to the user in
            the response so the UI can say what it just accepted.
  ACCEPTED  everything else.

Regardless of tier, a VALUE is never logged. Several of these will be
secrets and a structlog line is the last place they should land.

THE APP ALWAYS WINS THE MERGE

``build_spawn_env`` layers the user's map FIRST and the app's own trio
LAST. Precedence is not left to the user's ordering or to a validator
someone might later loosen - the control channel is written after
everything else, so it cannot be shadowed even if the block list above
were removed tomorrow.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not decide a bind address. ``server_prefs.bind_host`` is a
PREFERENCE that travels to the Python process as ``HOST``; the address
actually bound is decided exactly once, by
``src/core/setup_state.resolve_exposure``, downstream of anything written
here. See ``validate_bind_host``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Top-level config.json key holding the workspace block.
WORKSPACE_KEY = "workspace"

#: Top-level config.json key holding the remembered server preferences.
SERVER_PREFS_KEY = "server_prefs"

#: Env var carrying ``development_root`` into a spawned terminal.
DEV_ROOT_ENV = "CLOUDE_DEV_ROOT"

#: Env var carrying ``default_shell``. Chosen because ``agents``'
#: ``shell_command`` default is already ``$SHELL -i``, so setting this
#: makes the configured shell take effect through the mechanism that
#: exists rather than adding a second one.
SHELL_ENV = "SHELL"

#: POSIX environment variable name. Deliberately stricter than what the
#: kernel accepts (which is "anything without ``=`` or NUL"): a name
#: outside this shape cannot be referenced as ``$NAME`` from a shell, so
#: accepting one would produce a variable the user can see in the
#: settings screen and never use.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Reserved prefix. See the module docstring - the objection is silent
#: misrouting of the hook channel, not privilege.
RESERVED_ENV_PREFIX = "CLOUDECODE_"

#: Names saved but named back to the user. Exact matches.
WARNED_ENV_NAMES = frozenset({"PATH", "HOME", "SHELL", "USER", "LOGNAME", "TMPDIR"})

#: Prefixes saved but named back to the user.
WARNED_ENV_PREFIXES = ("LD_", "DYLD_", "ANTHROPIC_", "AWS_", "OPENAI_")

#: Substrings that make a name credential-shaped.
_CREDENTIAL_HINTS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "APIKEY", "API_KEY")

#: The two addresses that are always bindable regardless of interfaces.
UNIVERSAL_BIND_HOSTS = ("127.0.0.1", "0.0.0.0")


class WorkspaceValidationError(ValueError):
    """A workspace setting was rejected, with a message naming the problem.

    Its own exception type rather than bare ``ValueError`` so the route
    handler can distinguish "the user typed something wrong" (400, show
    the message) from "config.json is broken" (500).
    """


def _blank(value: Optional[str]) -> bool:
    """Whether a value means "not configured".

    Args:
        value: The raw string from the client, or None.

    Returns:
        True for None, empty, or whitespace-only.
    """
    return value is None or not str(value).strip()


def validate_development_root(raw: Optional[str]) -> str:
    """Check a development root and return it normalized.

    Args:
        raw: The path the user typed. Empty/None means "unset".

    Returns:
        The expanded absolute path, or "" when unset.

    Raises:
        WorkspaceValidationError: The path does not exist, or exists and
            is not a directory. Both messages name the path.

    Example:
        >>> validate_development_root("")
        ''
    """
    if _blank(raw):
        return ""
    path = Path(str(raw).strip()).expanduser()
    if not path.exists():
        raise WorkspaceValidationError(
            f"development root does not exist: {path}. Create it first, or "
            "clear the field to leave it unset."
        )
    if not path.is_dir():
        raise WorkspaceValidationError(
            f"development root is not a directory: {path}."
        )
    return str(path)


def validate_shell(raw: Optional[str]) -> str:
    """Check a default shell and return it normalized.

    Args:
        raw: An absolute path, or a bare name resolved against PATH.
            Empty/None means "unset" (terminals keep inheriting the
            server process's own SHELL).

    Returns:
        The absolute path to the shell, or "" when unset.

    Raises:
        WorkspaceValidationError: The shell could not be found, or is not
            executable. A shell that is present but not executable is
            called out specifically, because it is the case a bare
            existence check would pass and tmux would then fail on.
    """
    if _blank(raw):
        return ""
    text = str(raw).strip()
    candidate = Path(text).expanduser()
    if candidate.is_absolute() or text.startswith("~") or os.sep in text:
        resolved: Optional[str] = str(candidate) if candidate.exists() else None
    else:
        resolved = shutil.which(text)
    if resolved is None:
        raise WorkspaceValidationError(
            f"shell not found: {text}. Give an absolute path such as "
            "/bin/zsh, or a name that resolves on PATH."
        )
    if not os.access(resolved, os.X_OK):
        raise WorkspaceValidationError(
            f"shell is not executable: {resolved}."
        )
    if Path(resolved).is_dir():
        raise WorkspaceValidationError(f"shell is a directory: {resolved}.")
    return resolved


def validate_editor(raw: Optional[str]) -> str:
    """Check a default editor command and return it normalized.

    The value is a COMMAND LINE, not a bare path, because the useful
    editors need a flag: ``code -w``, ``subl -w``, ``vim``. Only the
    first token is resolved; the rest are the user's own arguments and
    are passed through untouched.

    Args:
        raw: The command line. Empty/None means "unset" - callers then
            fall back to whatever they did before this setting existed.

    Returns:
        The command line with its first token resolved to an absolute
        path, or "" when unset.

    Raises:
        WorkspaceValidationError: unparseable quoting, or a first token
            that resolves to nothing executable.
    """
    if _blank(raw):
        return ""
    text = str(raw).strip()
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise WorkspaceValidationError(
            f"editor command could not be parsed ({exc}). Check the quoting."
        ) from exc
    if not parts:
        raise WorkspaceValidationError("editor command is empty.")

    head = parts[0]
    candidate = Path(head).expanduser()
    if candidate.is_absolute() or head.startswith("~") or os.sep in head:
        resolved = str(candidate) if candidate.exists() else None
    else:
        resolved = shutil.which(head)
    if resolved is None:
        raise WorkspaceValidationError(
            f"editor not found: {head}. Install it, or give an absolute path."
        )
    if not os.access(resolved, os.X_OK) or Path(resolved).is_dir():
        raise WorkspaceValidationError(f"editor is not executable: {resolved}.")

    return " ".join([shlex.quote(resolved)] + [shlex.quote(p) for p in parts[1:]])


def classify_env_name(name: str) -> str:
    """Which of the three policy tiers a variable name falls into.

    Args:
        name: The variable name, already known to be well-formed.

    Returns:
        "blocked", "warned", or "accepted". See the module docstring for
        why the blocked tier is as narrow as it is.

    Example:
        >>> classify_env_name("CLOUDECODE_HOOK_TOKEN")
        'blocked'
        >>> classify_env_name("ANTHROPIC_API_KEY")
        'warned'
        >>> classify_env_name("EDITOR")
        'accepted'
    """
    upper = name.upper()
    if upper.startswith(RESERVED_ENV_PREFIX):
        return "blocked"
    if upper in WARNED_ENV_NAMES:
        return "warned"
    if upper.startswith(WARNED_ENV_PREFIXES):
        return "warned"
    if any(hint in upper for hint in _CREDENTIAL_HINTS):
        return "warned"
    return "accepted"


def validate_env_map(raw: Optional[Dict[str, str]]) -> Tuple[Dict[str, str], List[str]]:
    """Check a whole env map and return it plus the names to warn about.

    Args:
        raw: NAME -> value. None or {} means "no global env vars".

    Returns:
        (env, warnings). ``env`` is the accepted map with values coerced
        to str. ``warnings`` is one human sentence per warned name; it is
        NEVER empty-as-a-proxy-for-fine, callers must render it.

    Raises:
        WorkspaceValidationError: A malformed name, a reserved name, or a
            value containing NUL or a newline. Every message names the
            specific variable, because "invalid environment" tells the
            user nothing about which row to fix.
    """
    if not raw:
        return {}, []
    if not isinstance(raw, dict):
        raise WorkspaceValidationError(
            "environment variables must be a set of name/value pairs."
        )

    env: Dict[str, str] = {}
    warnings: List[str] = []
    for name, value in raw.items():
        key = str(name).strip()
        if not key:
            raise WorkspaceValidationError(
                "an environment variable has an empty name."
            )
        if not ENV_NAME_RE.match(key):
            raise WorkspaceValidationError(
                f"{key!r} is not a valid environment variable name. Use "
                "letters, digits and underscore, starting with a letter or "
                "underscore."
            )

        tier = classify_env_name(key)
        if tier == "blocked":
            raise WorkspaceValidationError(
                f"{key} is reserved. Names beginning {RESERVED_ENV_PREFIX} "
                "carry this app's own per-session hook channel; setting one "
                "here would misroute hook events silently rather than fail."
            )

        text = "" if value is None else str(value)
        if "\x00" in text:
            raise WorkspaceValidationError(
                f"the value for {key} contains a NUL byte, which no "
                "environment variable can hold."
            )
        if "\n" in text or "\r" in text:
            raise WorkspaceValidationError(
                f"the value for {key} contains a line break. Environment "
                "values are single-line."
            )

        if tier == "warned":
            warnings.append(
                f"{key} overrides an environment variable the shell or an "
                "agent may already rely on. It was saved; check it if a "
                "terminal behaves oddly."
            )
        env[key] = text

    return env, warnings


def _machine_ipv4_addresses() -> List[str]:
    """Every IPv4 address this machine currently holds.

    Returns:
        A list of dotted-quad strings, possibly empty. Empty is a
        could-not-enumerate outcome, and ``validate_bind_host`` treats it
        as such rather than as "no address is valid".
    """
    found: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found:
                found.append(addr)
    except OSError:
        return []
    return found


def validate_bind_host(raw: Optional[str], known: Optional[List[str]] = None) -> str:
    """Check a remembered bind address.

    This validates a PREFERENCE. It does not, and cannot, decide what the
    server binds: the Python process resolves that exactly once through
    ``src/core/setup_state.resolve_exposure``, which pins loopback until
    setup is complete no matter what is stored here. A value accepted by
    this function is a value the user would LIKE once setup is finished.

    Args:
        raw: The address. Empty/None means "unset" (the app keeps its own
            default).
        known: Injected list of this machine's addresses, for tests.
            Defaults to enumerating them live.

    Returns:
        The address, or "" when unset.

    Raises:
        WorkspaceValidationError: Not a dotted-quad IPv4 address, or an
            address this machine does not hold. A hostname is refused on
            purpose: a bind address is an address, not a lookup.
    """
    if _blank(raw):
        return ""
    text = str(raw).strip()
    try:
        socket.inet_aton(text)
    except OSError as exc:
        raise WorkspaceValidationError(
            f"{text!r} is not an IPv4 address. A bind address must be an "
            "address, not a hostname."
        ) from exc
    if text.count(".") != 3:
        raise WorkspaceValidationError(
            f"{text!r} is not a full IPv4 address."
        )
    if text in UNIVERSAL_BIND_HOSTS:
        return text

    addresses = _machine_ipv4_addresses() if known is None else known
    if not addresses:
        raise WorkspaceValidationError(
            f"this machine's addresses could not be enumerated, so whether "
            f"{text} can be bound cannot be determined. Choose 127.0.0.1 or "
            "0.0.0.0, which are always bindable."
        )
    if text not in addresses:
        raise WorkspaceValidationError(
            f"{text} is not an address this machine holds "
            f"({', '.join(addresses)}), so the server could not bind it."
        )
    return text


def build_spawn_env(
    workspace: Dict[str, object], app_env: Dict[str, str]
) -> Dict[str, str]:
    """Layer the user's global environment under the app's own.

    Args:
        workspace: The validated workspace block. ``env``,
            ``development_root`` and ``default_shell`` are read; anything
            else is ignored.
        app_env: The app's per-session control vars
            (``CLOUDECODE_SESSION_ID`` and friends).

    Returns:
        One merged map. The app's vars are written LAST and therefore win
        unconditionally - see the module docstring on why precedence is
        expressed as write order rather than trusted to validation.

    Example:
        >>> build_spawn_env({"env": {"FOO": "1"}}, {"CLOUDECODE_X": "y"})
        {'FOO': '1', 'CLOUDECODE_X': 'y'}
    """
    merged: Dict[str, str] = {}

    user_env = workspace.get("env") or {}
    if isinstance(user_env, dict):
        for name, value in user_env.items():
            merged[str(name)] = "" if value is None else str(value)

    root = workspace.get("development_root")
    if root and str(root).strip():
        merged[DEV_ROOT_ENV] = str(root).strip()

    shell = workspace.get("default_shell")
    if shell and str(shell).strip():
        merged[SHELL_ENV] = str(shell).strip()

    merged.update(app_env)
    return merged
