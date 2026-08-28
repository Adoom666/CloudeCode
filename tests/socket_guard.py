"""Hard isolation of the pytest suite from the user's live tmux sockets.

The test suite used to hardcode the production socket name ("cloude") in
roughly two dozen places, so running pytest created and killed REAL tmux
sessions on the socket the user's live work runs on.

This module makes that outcome unreachable rather than merely unlikely.
It does not depend on any individual test being written carefully:

* :data:`TEST_SOCKET_NAME` is a unique per-process socket name that every
  test must use. It is the single source of truth for the suite.
* :func:`install_subprocess_guard` wraps every subprocess entry point the
  application can reach and inspects the argv of each ``tmux`` invocation
  BEFORE it is executed. Anything that is not provably aimed at this
  process's own test socket raises :class:`TmuxSocketGuardError`.

THE THREE-OUTCOME RULE
----------------------
Each inspected invocation resolves to exactly one of three verdicts:

* ``safe``          - the socket was determined and is this run's test socket.
* ``unsafe``        - the socket was determined and is NOT the test socket.
* ``undetermined``  - the socket could not be determined at all.

``undetermined`` is a FAILURE, never a pass. "I could not tell which socket
this command would hit" is precisely the state in which the original defect
hid, so it is treated as at least as dangerous as a known-bad socket.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

__all__ = [
    "TEST_SOCKET_PREFIX",
    "TEST_SOCKET_NAME",
    "FORBIDDEN_SOCKET_NAME",
    "TmuxSocketGuardError",
    "classify_tmux_argv",
    "assert_safe_socket_name",
    "install_subprocess_guard",
    "remove_subprocess_guard",
    "guard_is_installed",
    "install_default_socket_redirect",
    "remove_default_socket_redirect",
    "default_redirect_is_installed",
    "shipped_default_socket_name",
    "auth_config_redirect_state",
    "derive_test_socket",
    "kill_test_socket_server",
]


#: Every socket this suite is allowed to touch must start with this.
TEST_SOCKET_PREFIX: str = "cloude_pytest_"

#: The production socket. No test may ever execute tmux against it.
FORBIDDEN_SOCKET_NAME: str = "cloude"

#: This process's socket. Unique per pytest process, so parallel xdist
#: workers and concurrent local runs never share or clean up each other's
#: server. Computed once at import.
TEST_SOCKET_NAME: str = f"{TEST_SOCKET_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:8]}"


class TmuxSocketGuardError(RuntimeError):
    """Raised when a test tries to run tmux against a non-test socket."""


def _as_argv_list(cmd: Any) -> Optional[List[str]]:
    """Normalise a subprocess command into a list of strings.

    Inputs:
        cmd: The first positional argument handed to a subprocess API.
             May be a list/tuple of str/bytes/Path, or a single string.

    Outputs:
        A list of strings, or None when the command is a bare string
        (shell form), which cannot be parsed reliably.
    """
    if isinstance(cmd, (str, bytes, Path)):
        return None
    if isinstance(cmd, Sequence):
        out: List[str] = []
        for part in cmd:
            if isinstance(part, bytes):
                out.append(part.decode("utf-8", "replace"))
            else:
                out.append(str(part))
        return out
    return None


def _looks_like_tmux(token: str) -> bool:
    """Report whether an argv[0] token names the tmux binary.

    Inputs:
        token: The first element of an argv list.

    Outputs:
        True when the basename is exactly "tmux".
    """
    return os.path.basename(token) == "tmux"


def _mentions_tmux_command(token: str) -> bool:
    """Report whether an argv token invokes tmux anywhere inside itself.

    Catches shell-wrapped forms such as ``sh -c "tmux ls"``, where the
    whole command is a single token. Deliberately compares basenames of
    whitespace/separator-delimited components, so an unrelated path like
    ``/private/tmp/tmux-501/`` does not match.

    Inputs:
        token: One element of an argv list.

    Outputs:
        True when a component of the token names the tmux binary.
    """
    for sep in (";", "|", "&", "(", ")", "\n"):
        token = token.replace(sep, " ")
    return any(_looks_like_tmux(part) for part in token.split() if part)


def classify_tmux_argv(cmd: Any) -> Tuple[str, Optional[str], str]:
    """Decide whether a subprocess command may be executed.

    Inputs:
        cmd: The command as passed to a subprocess API (list or string).

    Outputs:
        A ``(verdict, socket_name, detail)`` tuple. ``verdict`` is one of
        "not_tmux", "safe", "unsafe" or "undetermined". ``socket_name`` is
        the resolved socket when one could be determined, else None.
        ``detail`` is a human-readable reason, always populated.

    Example:
        >>> classify_tmux_argv(["tmux", "-L", "cloude", "ls"])[0]
        'unsafe'
    """
    argv = _as_argv_list(cmd)

    if argv is None:
        text = cmd.decode("utf-8", "replace") if isinstance(cmd, bytes) else str(cmd)
        if "tmux" in text:
            return (
                "undetermined",
                None,
                "tmux invoked through an unparseable shell string; the target "
                f"socket cannot be determined: {text!r}",
            )
        return ("not_tmux", None, "not a tmux invocation")

    if not argv:
        return ("not_tmux", None, "empty argv")

    if not _looks_like_tmux(argv[0]):
        # A tmux call hidden behind a shell wrapper is still a risk we
        # cannot evaluate, so surface it rather than waving it through.
        if any(_mentions_tmux_command(tok) for tok in argv):
            return (
                "undetermined",
                None,
                "tmux appears in argv but not as argv[0], so the socket flag "
                f"cannot be attributed reliably: {argv!r}",
            )
        return ("not_tmux", None, "not a tmux invocation")

    # argv[0] is tmux. Two flags answer entirely from the binary and never
    # open, create or connect to a socket - `-V` prints the version and
    # exits, `-h` prints usage and exits. They are the ONLY tmux
    # invocations that are safe without a socket selector, and they are
    # exactly what a discovery probe needs to run (see
    # src/core/tmux_discovery.probe_tmux, which executes `tmux -V` to prove
    # the binary runs rather than trusting shutil.which). Blocking them
    # would force the probe to guess, which is the defect it exists to
    # remove. Anything else with no -L/-S still targets the shared default
    # socket and stays unsafe.
    if len(argv) == 2 and argv[1] in ("-V", "-h"):
        return (
            "safe",
            None,
            f"tmux {argv[1]} answers from the binary and opens no socket",
        )

    # Find the socket selector among the server options, which tmux
    # requires to appear before the command word.
    for i, tok in enumerate(argv[1:], start=1):
        if tok == "-L":
            if i + 1 >= len(argv):
                return (
                    "undetermined",
                    None,
                    f"tmux -L given with no socket name: {argv!r}",
                )
            return _verdict_for_name(argv[i + 1], argv)
        if tok.startswith("-L") and len(tok) > 2:
            return _verdict_for_name(tok[2:], argv)
        if tok == "-S":
            if i + 1 >= len(argv):
                return (
                    "undetermined",
                    None,
                    f"tmux -S given with no socket path: {argv!r}",
                )
            return _verdict_for_name(os.path.basename(argv[i + 1]), argv)
        if tok.startswith("-S") and len(tok) > 2:
            return _verdict_for_name(os.path.basename(tok[2:]), argv)

    # No socket selector at all. tmux would fall back to its shared
    # "default" socket, which is a real server this suite does not own.
    return (
        "unsafe",
        "default",
        "tmux invoked with no -L/-S, which targets the shared default "
        f"socket rather than the test socket: {argv!r}",
    )


def _verdict_for_name(name: str, argv: List[str]) -> Tuple[str, Optional[str], str]:
    """Judge a resolved socket name against the test-socket policy.

    Inputs:
        name: The socket name parsed out of the argv.
        argv: The full argv, used only for the error message.

    Outputs:
        A ``(verdict, socket_name, detail)`` tuple, as per
        :func:`classify_tmux_argv`.
    """
    if not name:
        return ("undetermined", None, f"empty socket name in: {argv!r}")
    if name == TEST_SOCKET_NAME or name.startswith(TEST_SOCKET_NAME + "_"):
        return ("safe", name, "targets this run's test socket")
    if name == FORBIDDEN_SOCKET_NAME:
        return (
            "unsafe",
            name,
            f"targets the PRODUCTION socket {FORBIDDEN_SOCKET_NAME!r}: {argv!r}",
        )
    return (
        "unsafe",
        name,
        f"targets socket {name!r}, which is not this run's test socket "
        f"{TEST_SOCKET_NAME!r}: {argv!r}",
    )


def assert_safe_socket_name(name: Optional[str], context: str) -> None:
    """Fail unless a socket name is provably this run's test socket.

    Inputs:
        name: The socket name to check. None means "could not determine".
        context: Short description of where the name came from, for the
                 error message.

    Outputs:
        None. Raises :class:`TmuxSocketGuardError` on unsafe or
        undetermined input.
    """
    if name is None:
        raise TmuxSocketGuardError(
            f"{context}: socket name could not be determined. Under the "
            "three-outcome rule this is a failure, not a pass."
        )
    if name != TEST_SOCKET_NAME:
        raise TmuxSocketGuardError(
            f"{context}: resolved socket {name!r} is not this run's test "
            f"socket {TEST_SOCKET_NAME!r}."
        )


#: Every socket handed out by :func:`derive_test_socket`, so teardown can
#: kill exactly the servers this run created and nothing else.
_DERIVED_SOCKETS: set = set()


def derive_test_socket(label: str) -> str:
    """Mint a unique child socket name owned by this pytest process.

    Some test modules need a socket per test rather than one per run, for
    example so a torn-down pane cannot affect the next case. Deriving the
    name from :data:`TEST_SOCKET_NAME` keeps that isolation while leaving
    ownership provable, which a scheme like ``cc-paint-<hex>`` does not:
    the guard has no way to tell such a name from a real user socket.

    Inputs:
        label: Short tag identifying the calling module, for readability
               in ``ls /tmp/tmux-*``.

    Outputs:
        A socket name of the form
        ``cloude_pytest_<pid>_<hex>_<label>_<hex>``, registered for
        teardown.

    Example:
        >>> derive_test_socket("paint").startswith(TEST_SOCKET_NAME + "_")
        True
    """
    name = f"{TEST_SOCKET_NAME}_{label}_{uuid.uuid4().hex[:8]}"
    _DERIVED_SOCKETS.add(name)
    return name


# ---- subprocess interception -------------------------------------------

_ORIGINALS: dict = {}


def _guarded(original: Callable, label: str) -> Callable:
    """Wrap a subprocess entry point with the tmux socket check.

    Inputs:
        original: The unpatched callable.
        label: Name of the entry point, used in error messages.

    Outputs:
        A callable with the same signature that inspects the command
        first and raises before any process is spawned.
    """

    def wrapper(*args, **kwargs):
        if args:
            cmd: Any = args[0]
        elif "args" in kwargs:
            cmd = kwargs["args"]
        elif "program" in kwargs:
            cmd = [kwargs["program"], *args[1:]]
        else:
            cmd = None

        if label == "asyncio.create_subprocess_exec" and args:
            cmd = list(args)

        if cmd is not None:
            verdict, name, detail = classify_tmux_argv(cmd)
            if verdict == "unsafe":
                raise TmuxSocketGuardError(
                    f"BLOCKED tmux invocation via {label}: {detail} "
                    f"Tests must use tests.socket_guard.TEST_SOCKET_NAME."
                )
            if verdict == "undetermined":
                raise TmuxSocketGuardError(
                    f"BLOCKED tmux invocation via {label}: {detail} "
                    "The target socket could not be determined, which the "
                    "three-outcome rule treats as a failure, not a pass."
                )
        return original(*args, **kwargs)

    wrapper.__name__ = getattr(original, "__name__", "guarded")
    wrapper.__doc__ = f"Socket-guarded wrapper around {label}."
    return wrapper


def install_subprocess_guard() -> None:
    """Patch every subprocess entry point tmux can be launched through.

    Inputs:
        None.

    Outputs:
        None. Idempotent: a second call is a no-op.
    """
    if _ORIGINALS:
        return

    import asyncio

    targets = [
        (subprocess, "run", "subprocess.run"),
        (subprocess, "Popen", "subprocess.Popen"),
        (subprocess, "call", "subprocess.call"),
        (subprocess, "check_call", "subprocess.check_call"),
        (subprocess, "check_output", "subprocess.check_output"),
        (asyncio, "create_subprocess_exec", "asyncio.create_subprocess_exec"),
        (asyncio, "create_subprocess_shell", "asyncio.create_subprocess_shell"),
    ]
    for module, attr, label in targets:
        original = getattr(module, attr, None)
        if original is None:
            continue
        _ORIGINALS[(module.__name__, attr)] = (module, attr, original)
        setattr(module, attr, _guarded(original, label))


def remove_subprocess_guard() -> None:
    """Restore every patched subprocess entry point.

    Inputs:
        None.

    Outputs:
        None.
    """
    for module, attr, original in list(_ORIGINALS.values()):
        setattr(module, attr, original)
    _ORIGINALS.clear()


def guard_is_installed() -> bool:
    """Report whether the subprocess guard is currently active.

    Inputs:
        None.

    Outputs:
        True when every entry point is patched, False otherwise.
    """
    return bool(_ORIGINALS)


# ---- production default redirect ---------------------------------------

_DEFAULT_PATCHES: List[Tuple[Any, str, Any]] = []
_FUNC_DEFAULT_PATCHES: List[Tuple[Any, tuple]] = []
_KWDEFAULT_PATCHES: List[Tuple[Any, dict]] = []


def install_default_socket_redirect() -> None:
    """Point every in-process default socket name at the test socket.

    The hardcoded literals were only half the defect. Any code path that
    simply omitted a socket name fell back to
    ``tmux_backend.DEFAULT_SOCKET_NAME``, which is the production socket,
    so a test that named no socket at all still reached the user's live
    server.

    This rewrites three things, because a Python default can be bound in
    three different places:

    * module-level ``DEFAULT_SOCKET_NAME`` in every already-imported
      ``src`` module, since ``from ... import DEFAULT_SOCKET_NAME``
      creates an independent binding;
    * ``__defaults__`` on functions and methods, which capture the value
      at definition time and are therefore immune to the above;
    * ``__kwdefaults__`` for keyword-only parameters.

    Inputs:
        None.

    Outputs:
        None. Idempotent.
    """
    import inspect
    import sys

    if _DEFAULT_PATCHES or _FUNC_DEFAULT_PATCHES:
        return

    import src.core.tmux_backend  # noqa: F401  (ensure it is imported)

    for mod_name, module in list(sys.modules.items()):
        if not (mod_name == "src" or mod_name.startswith("src.")):
            continue
        if module is None:
            continue

        current = getattr(module, "DEFAULT_SOCKET_NAME", None)
        if current == FORBIDDEN_SOCKET_NAME:
            _DEFAULT_PATCHES.append((module, "DEFAULT_SOCKET_NAME", current))
            setattr(module, "DEFAULT_SOCKET_NAME", TEST_SOCKET_NAME)

        holders = [module] + [
            obj for obj in vars(module).values() if isinstance(obj, type)
        ]
        for holder in holders:
            for obj in list(vars(holder).values()):
                func = obj
                if isinstance(obj, (classmethod, staticmethod)):
                    func = obj.__func__
                if not inspect.isfunction(func):
                    continue

                defaults = func.__defaults__
                if defaults and FORBIDDEN_SOCKET_NAME in defaults:
                    _FUNC_DEFAULT_PATCHES.append((func, defaults))
                    func.__defaults__ = tuple(
                        TEST_SOCKET_NAME if v == FORBIDDEN_SOCKET_NAME else v
                        for v in defaults
                    )

                kwdefaults = func.__kwdefaults__
                if kwdefaults and FORBIDDEN_SOCKET_NAME in kwdefaults.values():
                    _KWDEFAULT_PATCHES.append((func, dict(kwdefaults)))
                    func.__kwdefaults__ = {
                        k: (TEST_SOCKET_NAME if v == FORBIDDEN_SOCKET_NAME else v)
                        for k, v in kwdefaults.items()
                    }

    _install_auth_config_redirect()


#: Why the auth-config redirect did or did not install. Never silent:
#: a skipped redirect is reportable state, not an assumed success.
_AUTH_REDIRECT_STATE: dict = {"reason": "not attempted"}


def auth_config_redirect_state() -> str:
    """Report whether the auth-config socket redirect installed, and why.

    Inputs:
        None.

    Outputs:
        "installed", or a short reason it was skipped.
    """
    return _AUTH_REDIRECT_STATE["reason"]


def _install_auth_config_redirect() -> None:
    """Rewrite the socket name coming out of the on-disk auth config.

    ``config.json`` on a developer machine legitimately says
    ``tmux_socket_name: "cloude"``, and the adopt path reads it directly
    via ``settings.load_auth_config()``. That is a production-reaching
    path that contains no hardcoded literal anywhere in the test suite,
    so removing literals alone would not have closed it.

    Inputs:
        None.

    Outputs:
        None.
    """
    try:
        from src import config as config_module
    except (ImportError, SystemExit, ValueError):
        # ``src.config`` builds a pydantic Settings at import time and
        # exits when .env is absent. If it cannot be imported here, no
        # config-derived socket name can be produced either, so there is
        # nothing to redirect. The subprocess guard remains the wall.
        _AUTH_REDIRECT_STATE["reason"] = "src.config could not be imported"
        return

    original = config_module.Settings.load_auth_config

    def patched(self, *args, **kwargs):
        auth_config = original(self, *args, **kwargs)
        session = getattr(auth_config, "session", None)
        if session is not None:
            if getattr(session, "tmux_socket_name", None) == FORBIDDEN_SOCKET_NAME:
                try:
                    session.tmux_socket_name = TEST_SOCKET_NAME
                except (AttributeError, TypeError, ValueError):
                    # Frozen or validated model. The subprocess guard is
                    # the backstop, so a failure here is loud, not silent.
                    raise TmuxSocketGuardError(
                        "auth config carries the production socket name and "
                        "it could not be redirected to the test socket"
                    )
        return auth_config

    patched.__name__ = "load_auth_config"
    patched.__doc__ = "Socket-guarded wrapper around Settings.load_auth_config."
    _DEFAULT_PATCHES.append(
        (config_module.Settings, "load_auth_config", original)
    )
    config_module.Settings.load_auth_config = patched
    _AUTH_REDIRECT_STATE["reason"] = "installed"


def remove_default_socket_redirect() -> None:
    """Restore every default socket name patched by the redirect.

    Inputs:
        None.

    Outputs:
        None.
    """
    for module, attr, original in _DEFAULT_PATCHES:
        setattr(module, attr, original)
    _DEFAULT_PATCHES.clear()
    for func, defaults in _FUNC_DEFAULT_PATCHES:
        func.__defaults__ = defaults
    _FUNC_DEFAULT_PATCHES.clear()
    for func, kwdefaults in _KWDEFAULT_PATCHES:
        func.__kwdefaults__ = kwdefaults
    _KWDEFAULT_PATCHES.clear()


def default_redirect_is_installed() -> bool:
    """Report whether the production default has been redirected.

    Inputs:
        None.

    Outputs:
        True when at least one default binding was rewritten.
    """
    return bool(_DEFAULT_PATCHES or _FUNC_DEFAULT_PATCHES)


def shipped_default_socket_name() -> str:
    """Read the production default straight from the source file.

    Needed because the in-process constant is redirected during tests, so
    a test that wants to assert what the app SHIPS cannot read the live
    attribute. Parsing the source is immune to the redirect.

    Inputs:
        None.

    Outputs:
        The socket name declared by ``DEFAULT_SOCKET_NAME`` in
        ``src/core/tmux_backend.py``.

    Example:
        >>> shipped_default_socket_name()
        'cloude'
    """
    import ast

    source_path = (
        Path(__file__).resolve().parent.parent / "src" / "core" / "tmux_backend.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        else:
            continue
        if "DEFAULT_SOCKET_NAME" in targets and isinstance(value, ast.Constant):
            return str(value.value)
    raise AssertionError(
        f"DEFAULT_SOCKET_NAME not found as a module-level constant in {source_path}"
    )


def kill_test_socket_server() -> None:
    """Kill only this run's test tmux server, addressed by its own name.

    Never issues a bare tmux command and never names any other socket, so
    it cannot reach a server this process did not create.

    Inputs:
        None.

    Outputs:
        None. Failures are ignored: a socket with no server is the normal
        case and is not an error.
    """
    original = _ORIGINALS.get(("subprocess", "run"))
    run = original[2] if original else subprocess.run
    for name in [TEST_SOCKET_NAME, *sorted(_DERIVED_SOCKETS)]:
        try:
            run(
                ["tmux", "-L", name, "kill-server"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            # No server on the socket is the expected steady state.
            continue
