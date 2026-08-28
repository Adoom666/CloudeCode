"""Is this instance set up yet, and what may it expose while it is not.

THE PROBLEM THIS SOLVES

A fresh install binds ``0.0.0.0`` (``Settings.host`` in src/config.py, and
``DEFAULT_BIND_HOST`` in macOS/server-manager.js). So from the first second it
runs it is reachable from every machine on the LAN and on the tailnet, before
anybody has paired an authenticator with it. The setup wizard has to be usable
without logging in - there is no credential to log in WITH yet - which means
that during exactly the window in which the TOTP secret gets decided, an
unauthenticated page that decides it is reachable by strangers. Whoever reaches
it first owns the instance. That is a remote takeover, not a papercut.

The user was offered a one-time bootstrap token for this and turned it down:
"no totp is fine, just want to make it loads if its not setup yet." So the
wizard stays open during setup, exactly as asked, and the exposure is closed
from the other end instead.

THE INVARIANT

    The wizard is only reachable without authentication while the server is
    bound to loopback.

Both halves of that sentence are decided HERE, by one function, from one piece
of state, and ``resolve_exposure`` raises rather than return a pair that
violates it. They are not two settings that must be kept in agreement; there is
no way to express the dangerous combination.

WHY THE STATE IS READ FROM THE FILESYSTEM AND NOT FROM CONFIG

"Setup is done" is not a flag someone can set. It is four facts, each of which
is the physical residue of setup actually having happened:

    1. the config file exists and parses
    2. a TOTP secret is configured
    3. a JWT secret is configured
    4. an authenticator has actually been paired - the ``.totp_paired``
       sentinel that src/api/auth.py already writes and already trusts

Number 4 is the load-bearing one, and it is why a boolean in config.json would
have been the wrong design. A TOTP secret sitting in .env that nobody has ever
scanned does not mean the instance has an owner. Only a completed pairing does.

THREE OUTCOMES, AND WHICH WAY THE THIRD ONE FAILS

Setup state is complete, incomplete, or UNDETERMINED - the last when a file
exists but cannot be read or parsed, so no honest verdict is available. For the
exposure decision, undetermined is treated exactly like incomplete: bind
loopback, leave the wizard open. That is failing closed on the dimension that
matters, because the invariant above still holds - an undetermined instance is
not reachable from the network at all, so an open wizard on it can only be
reached by something already running as this user.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Setup demonstrably finished: config parses, both secrets present, and an
#: authenticator has been paired.
SETUP_COMPLETE = "complete"

#: At least one setup step demonstrably has not happened.
SETUP_INCOMPLETE = "incomplete"

#: A setup fact could not be read, so neither verdict is supported. Treated as
#: incomplete for exposure purposes; never reported as complete.
SETUP_UNDETERMINED = "undetermined"

#: The address the server is pinned to until setup is finished. IPv4 loopback
#: specifically: "localhost" can resolve to either family depending on the
#: host's resolver, and a bind address must be an address, not a lookup.
LOOPBACK_HOST = "127.0.0.1"

#: Filename of the pairing sentinel, kept in step with src/api/auth.py's
#: ``_totp_paired_sentinel_path``. Both read the same file; this constant
#: exists so the name is written down once.
PAIRED_SENTINEL_NAME = ".totp_paired"


@dataclass(frozen=True)
class SetupCheck:
    """One setup fact, and whether it holds.

    Attributes:
        key: Stable machine identifier, used by the wizard's markup and tests.
        title: Short human name for the fact.
        passed: True, False, or None for "could not evaluate". None is a real
            third value and is never coerced to either boolean.
        detail: One sentence a human can act on, including when passed is None.
    """

    key: str
    title: str
    passed: Optional[bool]
    detail: str


@dataclass(frozen=True)
class SetupState:
    """Whether this instance has been set up, and the evidence for the verdict.

    Attributes:
        status: One of SETUP_COMPLETE, SETUP_INCOMPLETE, SETUP_UNDETERMINED.
        checks: Every fact examined, in the order the wizard should show them.
    """

    status: str
    checks: tuple[SetupCheck, ...]

    @property
    def is_complete(self) -> bool:
        """Whether setup is finished.

        Returns:
            True only for SETUP_COMPLETE. An undetermined state is not
            complete, because "I could not check" is not "yes".
        """
        return self.status == SETUP_COMPLETE

    def outstanding(self) -> tuple[SetupCheck, ...]:
        """The checks that are not passing.

        Returns:
            Every check that failed or could not be evaluated, which is what
            the wizard lists as remaining work.
        """
        return tuple(c for c in self.checks if c.passed is not True)


@dataclass(frozen=True)
class Exposure:
    """What the server may listen on, and whether the wizard needs a login.

    Never construct this directly - use ``resolve_exposure``, which is where
    the invariant coupling the two fields is enforced.

    Attributes:
        bind_host: The address the server must actually bind.
        configured_bind_host: The address the user asked for, which may differ
            while setup is incomplete. Kept so the UI can show both and say
            which is in force, instead of showing an aspiration as a fact.
        locked_down: Whether bind_host was overridden by this module.
        wizard_requires_auth: Whether the setup wizard demands a valid session.
        reason: One sentence explaining the decision, for logs and for the UI.
    """

    bind_host: str
    configured_bind_host: str
    locked_down: bool
    wizard_requires_auth: bool
    reason: str

    @property
    def restart_required_to_apply(self) -> bool:
        """Whether finishing setup needs a restart before the bind changes.

        uvicorn binds its socket once, at startup, and has no in-place rebind.
        So a lockdown that lifts the moment setup completes lifts only in this
        object, not in the kernel. The wizard must say so out loud rather than
        leaving the user believing he is reachable on an address he is not.

        Returns:
            True whenever the address in force differs from the configured one.
        """
        return self.bind_host != self.configured_bind_host


def _sentinel_path(config_path: Path) -> Path:
    """Where the TOTP pairing sentinel lives for a given config file.

    Args:
        config_path: Path to config.json.

    Returns:
        The sentinel path, alongside the config, matching src/api/auth.py.
    """
    return config_path.parent / PAIRED_SENTINEL_NAME


def evaluate_setup_state(
    config_path: Path,
    totp_secret: Optional[str],
    jwt_secret: Optional[str],
) -> SetupState:
    """Decide whether this instance has been set up, from observable facts.

    Args:
        config_path: Path to config.json.
        totp_secret: The configured TOTP secret, or None/empty when unset.
        jwt_secret: The configured JWT signing secret, or None/empty.

    Returns:
        The SetupState, including one SetupCheck per fact examined.

    Example:
        >>> state = evaluate_setup_state(Path("/nonexistent.json"), None, None)
        >>> state.status
        'incomplete'
    """
    checks: list[SetupCheck] = []

    config_ok: Optional[bool]
    if not config_path.exists():
        config_ok = False
        config_detail = (
            f"No configuration file at {config_path}. The wizard will write "
            "one when you finish."
        )
    else:
        try:
            json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            config_ok = None
            config_detail = (
                f"{config_path} exists but is not valid JSON ({exc}), so "
                "whether it is set up correctly cannot be determined. Fix or "
                "move the file; nothing here will overwrite it."
            )
        except OSError as exc:
            config_ok = None
            config_detail = (
                f"{config_path} could not be read ({exc}), so its state "
                "cannot be determined."
            )
        else:
            config_ok = True
            config_detail = f"Configuration file present and readable at {config_path}."

    checks.append(
        SetupCheck(
            key="config_file",
            title="Configuration file",
            passed=config_ok,
            detail=config_detail,
        )
    )

    checks.append(
        SetupCheck(
            key="totp_secret",
            title="Two-factor secret",
            passed=bool(totp_secret and totp_secret.strip()),
            detail=(
                "A TOTP secret is configured."
                if totp_secret and totp_secret.strip()
                else "No TOTP secret is configured, so nothing can log in yet."
            ),
        )
    )

    checks.append(
        SetupCheck(
            key="jwt_secret",
            title="Session signing key",
            passed=bool(jwt_secret and jwt_secret.strip()),
            detail=(
                "A JWT signing secret is configured."
                if jwt_secret and jwt_secret.strip()
                else "No JWT signing secret is configured, so no session can "
                "be issued."
            ),
        )
    )

    paired_ok: Optional[bool]
    try:
        paired_ok = _sentinel_path(config_path).exists()
    except OSError as exc:
        paired_ok = None
        paired_detail = (
            f"Whether an authenticator has been paired could not be "
            f"determined ({exc})."
        )
    else:
        paired_detail = (
            "An authenticator has been paired with this instance."
            if paired_ok
            else "No authenticator has been paired yet. Until one is, this "
            "instance has no owner, so it stays on loopback only."
        )

    checks.append(
        SetupCheck(
            key="totp_paired",
            title="Authenticator paired",
            passed=paired_ok,
            detail=paired_detail,
        )
    )

    if any(c.passed is None for c in checks):
        status = SETUP_UNDETERMINED
    elif all(c.passed is True for c in checks):
        status = SETUP_COMPLETE
    else:
        status = SETUP_INCOMPLETE

    return SetupState(status=status, checks=tuple(checks))


def resolve_exposure(configured_bind_host: str, state: SetupState) -> Exposure:
    """Decide the bind address and the wizard's auth requirement together.

    This is the single place either question is answered, on purpose. Two
    separate decisions reading the same state would be two places to get it
    wrong; here the dangerous combination (an open wizard on a reachable
    socket) has no representation, and the guard below refuses to return one
    even if a future edit tries.

    Args:
        configured_bind_host: The address the user configured.
        state: The evaluated setup state.

    Returns:
        The Exposure in force.

    Raises:
        RuntimeError: The computed pair would leave an unauthenticated wizard
            reachable off-host. Raised rather than logged, because continuing
            past this means serving the takeover path.

    Example:
        >>> s = evaluate_setup_state(Path("/nonexistent.json"), None, None)
        >>> resolve_exposure("0.0.0.0", s).bind_host
        '127.0.0.1'
    """
    if state.is_complete:
        exposure = Exposure(
            bind_host=configured_bind_host,
            configured_bind_host=configured_bind_host,
            locked_down=False,
            wizard_requires_auth=True,
            reason=(
                "Setup is complete, so the configured bind address is in "
                "force and the wizard requires a login."
            ),
        )
    else:
        exposure = Exposure(
            bind_host=LOOPBACK_HOST,
            configured_bind_host=configured_bind_host,
            locked_down=configured_bind_host != LOOPBACK_HOST,
            wizard_requires_auth=False,
            reason=(
                "Setup is not complete, so the setup wizard is open without "
                "a login and the server is pinned to loopback until it is. "
                f"The configured address {configured_bind_host} takes effect "
                "after setup finishes and the server restarts."
            ),
        )

    if not exposure.wizard_requires_auth and exposure.bind_host != LOOPBACK_HOST:
        raise RuntimeError(
            "refusing to start: the setup wizard would be reachable without "
            f"authentication on {exposure.bind_host}. This combination is a "
            "remote takeover path and is never valid."
        )

    return exposure


#: Environment variable carrying the address uvicorn was ACTUALLY started on.
#:
#: Written by src/main.py immediately before ``uvicorn.run``, read by the
#: endpoints that report the bind. The environment is the channel rather than
#: a module global because ``python -m src.main`` runs the chooser as
#: ``__main__`` while uvicorn imports the app as ``src.main`` - two distinct
#: module objects in one process, so a global set by the first is invisible to
#: the second. os.environ is shared by both.
BOUND_HOST_ENV = "CLOUDE_BOUND_HOST"


def record_bound_host(host: str) -> None:
    """Record the address being handed to uvicorn, at the moment it is handed.

    Args:
        host: The address about to be bound.

    Returns:
        None.
    """
    os.environ[BOUND_HOST_ENV] = host


def recorded_bound_host() -> Optional[str]:
    """The address this process bound, if it recorded one.

    Returns:
        The recorded host, or None when there is no record. None means
        UNKNOWN and nothing else. It is never resolved to the configured
        address: uvicorn binds its socket once at startup and never rebinds,
        so configuration is what somebody ASKED for, which is a different
        claim from what is listening.
    """
    value = os.environ.get(BOUND_HOST_ENV, "").strip()
    return value or None


def bind_report(configured_host: str) -> dict:
    """What to tell a client about this server's exposure. Three outcomes.

    The defect this exists to prevent, measured on a real first install:
    ``current_exposure()`` re-derives the exposure from the filesystem on
    every call, so once a slow bootstrap finished writing its files the
    server began reporting ``effective_host: "0.0.0.0"`` while its only
    socket was on loopback. The menu bar displayed the address it was told.
    A re-derivation is an aspiration wearing a measurement's name.

    So the effective host here comes only from the startup record. When there
    is no record, every field derived from it is None rather than a guess -
    ``restart_required: false`` would be a verdict nobody measured, and it
    would tell the UI there is nothing to do.

    Args:
        configured_host: The address configuration currently asks for.

    Returns:
        A dict with ``effective_host`` (str or None), ``effective_host_known``
        (bool), ``configured_host`` (str), ``locked_down`` (bool or None),
        ``restart_required`` (bool or None) and ``reason`` (str).

    Example:
        >>> import os
        >>> os.environ["CLOUDE_BOUND_HOST"] = "127.0.0.1"
        >>> bind_report("0.0.0.0")["restart_required"]
        True
    """
    effective = recorded_bound_host()

    if effective is None:
        return {
            "effective_host": None,
            "effective_host_known": False,
            "configured_host": configured_host,
            "locked_down": None,
            "restart_required": None,
            "reason": (
                "The address in force was not measured, so it is unknown. "
                f"Configuration asks for {configured_host}, which is what "
                "was requested and not evidence of what is listening."
            ),
        }

    differs = effective != configured_host
    if differs:
        reason = (
            f"Listening on {effective}. Configuration asks for "
            f"{configured_host}, which needs a server restart to apply - "
            "uvicorn binds its socket once at startup and cannot rebind."
        )
    else:
        reason = f"Listening on {effective}, which is what configuration asks for."

    return {
        "effective_host": effective,
        "effective_host_known": True,
        "configured_host": configured_host,
        "locked_down": differs,
        "restart_required": differs,
        "reason": reason,
    }


def current_bind_report() -> dict:
    """``bind_report`` for the running server's own configured host.

    Returns:
        The bind report, as described by ``bind_report``.
    """
    from src.config import settings  # noqa: PLC0415 - see current_setup_state

    return bind_report(settings.host)


def current_setup_state() -> SetupState:
    """Evaluate setup state from the running server's own settings.

    Imports ``settings`` lazily so this module stays importable by tooling
    that has no ``.env`` - src/config.py exits the process on import when the
    environment is incomplete.

    Returns:
        The SetupState for this process.
    """
    from src.config import settings  # noqa: PLC0415 - deliberate, see above

    return evaluate_setup_state(
        Path(settings.auth_config_file).expanduser(),
        settings.totp_secret,
        settings.jwt_secret,
    )


def current_exposure() -> Exposure:
    """The Exposure in force for this process.

    Returns:
        The resolved Exposure, using the configured bind host from settings.
    """
    from src.config import settings  # noqa: PLC0415

    return resolve_exposure(settings.host, current_setup_state())


def mark_setup_complete(config_path: Path) -> None:
    """Write the pairing sentinel, completing setup.

    Idempotent. Written with ``O_EXCL`` where possible so two concurrent
    finishes cannot race, and an already-present sentinel is success rather
    than an error - the caller's goal is the file existing, not creating it.

    Args:
        config_path: Path to config.json; the sentinel is written alongside it.

    Raises:
        OSError: The sentinel could not be created. Propagated deliberately -
            a silent failure here would leave the instance permanently pinned
            to loopback with no explanation.
    """
    path = _sentinel_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
