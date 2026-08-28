"""UTF-8 locale resolution for spawned tmux panes.

WHY THIS EXISTS
---------------

The server is normally started by a macOS LaunchAgent. A LaunchAgent
process inherits an almost empty environment: no ``LANG``, no ``LC_ALL``,
no ``LC_CTYPE``. ``TmuxBackend.start`` builds the new pane's environment
from ``os.environ.copy()``, so the pane inherits that same emptiness and
its shell falls back to the POSIX "C" locale, whose character set is
7-bit.

zsh reacts to that by refusing every multibyte character it is asked to
handle, one error per source line of the offending function::

    _enhanced_path:2: character not in range
    _enhanced_path:3: character not in range

which is printed on every single session start, before the user has typed
anything. Any prompt helper, powerline glyph or box-drawing character in
the user's dotfiles produces it.

WHAT WE DO NOT DO
-----------------

We do not hardcode ``en_US.UTF-8``. This app is not single-user, and a
machine's owner may well work in another language. The value we want is
the one the user's own interactive shell uses; we only synthesise a value
when the user has expressed no preference we can find.

Resolution order (first hit wins):

1. ``LC_ALL`` / ``LANG`` / ``LC_CTYPE`` already in the server's
   environment, IF the value already names a UTF-8 codeset. Nothing to
   do; the operator set it deliberately.
2. The user's login shell, asked directly:
   ``$SHELL -lc 'printf %s "${LC_ALL:-${LANG:-$LC_CTYPE}}"'``. This is
   the user's real, personal value (their ``.zshenv`` / ``.zprofile``).
3. A non-UTF-8 value from step 1 or 2 that still names a real locale
   (e.g. ``en_US`` or ``de_DE.ISO8859-1``): keep the LANGUAGE, swap the
   codeset for UTF-8. Still the user's preference, just a wider charset.
4. The operating system's own regional preference. On macOS that is
   ``defaults read -g AppleLocale``.
5. ``C.UTF-8``, then ``en_US.UTF-8``, whichever the C library actually
   has. This is the only step that invents a value, and it is reached
   only when the user, their shell and the OS all said nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

#: Locale variables in the order the C library itself resolves them.
_LOCALE_VARS = ("LC_ALL", "LANG", "LC_CTYPE")

#: Codeset suffixes that mean UTF-8. Case- and separator-insensitive
#: because both ``en_US.UTF-8`` and ``en_US.utf8`` occur in the wild.
_UTF8_CODESETS = ("utf8", "utf-8")

#: ``language[_TERRITORY][.codeset][@modifier]`` split.
_LOCALE_RE = re.compile(r"^(?P<base>[A-Za-z0-9_\-]+)(?:\.(?P<codeset>[^@]+))?(?P<mod>@.*)?$")

#: Seconds to wait on any helper subprocess. A login shell that has not
#: printed its locale in this long is misconfigured; we fall through
#: rather than block a session create.
_PROBE_TIMEOUT_S = 3.0

#: Probe results, cached for the process lifetime. Session creation is on
#: the user-visible latency path and one of these probes spawns a login
#: shell, so we pay for each of them exactly once. The RESOLUTION is not
#: cached, because it depends on the environment handed to it.
_probe_cache: Dict[str, object] = {}


def is_utf8_locale(value: str) -> bool:
    """Report whether a locale string names a UTF-8 character set.

    Args:
        value: A locale string such as ``en_US.UTF-8`` or ``C``.

    Returns:
        True when the codeset component is UTF-8.

    Example:
        >>> is_utf8_locale("en_US.utf8")
        True
        >>> is_utf8_locale("en_US")
        False
    """
    match = _LOCALE_RE.match((value or "").strip())
    if not match:
        return False
    codeset = (match.group("codeset") or "").replace("_", "-").lower()
    return codeset in _UTF8_CODESETS


def to_utf8_locale(value: str) -> str:
    """Rewrite a locale string to use the UTF-8 codeset.

    Args:
        value: A locale string, with or without a codeset.

    Returns:
        The same language/territory with a ``.UTF-8`` codeset, or ``""``
        when the input cannot be parsed as a locale.

    Example:
        >>> to_utf8_locale("de_DE.ISO8859-1")
        'de_DE.UTF-8'
    """
    match = _LOCALE_RE.match((value or "").strip())
    if not match:
        return ""
    return f"{match.group('base')}.UTF-8{match.group('mod') or ''}"


def _run(argv: List[str]) -> str:
    """Run a helper command and return its stripped stdout, or ``""``.

    Args:
        argv: Full argument vector. Never shell-interpreted.

    Returns:
        Stdout with surrounding whitespace removed. Any failure, timeout
        or missing binary yields ``""`` - every caller has a fallback.
    """
    cached = _probe_cache.get(repr(argv))
    if isinstance(cached, str):
        return cached
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _probe_cache[repr(argv)] = ""
        return ""
    if proc.returncode != 0:
        _probe_cache[repr(argv)] = ""
        return ""
    result = proc.stdout.decode("utf-8", errors="replace").strip()
    _probe_cache[repr(argv)] = result
    return result


def _available_locales() -> List[str]:
    """List locale names the C library actually supports.

    Returns:
        Names from ``locale -a``, or ``[]`` when that is unavailable.
        An empty list means "cannot verify", never "nothing supported".
    """
    out = _run(["locale", "-a"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _first_supported(candidates: List[str], available: List[str]) -> str:
    """Pick the first candidate the system supports.

    Args:
        candidates: Locale names in preference order.
        available: Output of :func:`_available_locales`.

    Returns:
        The first supported candidate. When ``available`` is empty we
        cannot verify anything, so the first candidate is returned as-is.
    """
    if not candidates:
        return ""
    if not available:
        return candidates[0]
    normalized = {name.replace("-", "").lower(): name for name in available}
    for candidate in candidates:
        hit = normalized.get(candidate.replace("-", "").lower())
        if hit:
            return candidate
    return ""


def _user_shell_locale() -> str:
    """Ask the user's own login shell what locale it runs in.

    Returns:
        The shell's ``LC_ALL``/``LANG``/``LC_CTYPE``, or ``""``.
    """
    shell = os.environ.get("SHELL") or "/bin/sh"
    return _run([shell, "-lc", 'printf %s "${LC_ALL:-${LANG:-$LC_CTYPE}}"'])


def _os_preference_locale() -> str:
    """Read the operating system's regional preference.

    Returns:
        A locale base such as ``en_US`` on macOS, or ``""`` elsewhere.
        The ``@calendar=`` style modifier macOS sometimes appends is
        stripped, because it is not a C-library locale modifier.
    """
    value = _run(["defaults", "read", "-g", "AppleLocale"])
    return value.split("@", 1)[0].strip()


def resolve_pane_locale(environ: Optional[Dict[str, str]] = None) -> str:
    """Resolve the UTF-8 locale a spawned pane should run in.

    Args:
        environ: Environment to inspect. Defaults to ``os.environ``.

    Returns:
        A locale name to export as ``LANG``, or ``""`` when the supplied
        environment already carries a UTF-8 locale and needs no help.

    Example:
        >>> resolve_pane_locale({"LANG": "en_US.UTF-8"})
        ''
    """
    env = os.environ if environ is None else environ

    for var in _LOCALE_VARS:
        if is_utf8_locale(env.get(var, "")):
            return ""

    available = _available_locales()

    # The user's own preference, then whatever non-UTF-8 value they or
    # the operator already set, upgraded to a UTF-8 codeset.
    candidates: List[str] = []
    for source in (_user_shell_locale(), *(env.get(v, "") for v in _LOCALE_VARS)):
        if not source:
            continue
        if is_utf8_locale(source):
            candidates.append(source.strip())
        else:
            upgraded = to_utf8_locale(source)
            if upgraded:
                candidates.append(upgraded)

    os_pref = _os_preference_locale()
    if os_pref:
        upgraded = to_utf8_locale(os_pref)
        if upgraded:
            candidates.append(upgraded)

    candidates.extend(("C.UTF-8", "en_US.UTF-8"))

    chosen = _first_supported(candidates, available)
    if not chosen:
        # Nothing we proposed is installed. Exporting an unsupported name
        # would leave the pane in "C" anyway, so say so and give up.
        logger.warning("pane_locale_unresolved", candidates=candidates)
        return ""
    return chosen


def apply_pane_locale(env: Dict[str, str]) -> Dict[str, str]:
    """Set ``LANG`` on a spawn environment when it lacks a UTF-8 locale.

    ``LANG`` rather than ``LC_ALL``: ``LC_ALL`` is an override that would
    stop the user's own dotfiles from setting, say, ``LC_TIME``. ``LANG``
    is the default-of-last-resort the C library consults, so anything the
    user sets later still wins.

    Args:
        env: Spawn environment, mutated in place.

    Returns:
        The same dict, for chaining.
    """
    chosen = resolve_pane_locale(env)
    if chosen:
        env["LANG"] = chosen
        logger.info("pane_locale_applied", locale=chosen)
    return env
