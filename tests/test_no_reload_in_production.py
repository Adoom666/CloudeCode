"""Guard against uvicorn's file-watching reloader ever becoming the
production default again.

The incident this test exists for: src/main.py's __main__ block called
``uvicorn.run(..., reload=True)`` unconditionally. Production runs that
exact code path under a launchd LaunchAgent, so every deployed instance was
running with --reload live. WatchFiles then treated every write under the
repo root - including the 48 files a `git pull` touches - as a reason to
tear down and re-exec the whole server. That is how a routine deploy pull
became an unplanned mid-work restart, and evidence in
~/Library/Logs/cloude-code/launchd.log ties at least one such reload
cascade directly to an `[Errno 48] Address already in use` bind race and a
subsequent full process exit (launchd's own KeepAlive/ThrottleInterval then
relaunches the whole job, which is the documented 30-second double-start
with "last exit code = 3").

The fix is settings.dev_reload (env var CLOUDE_DEV_RELOAD), defaulting to
False, so reload is opt-in for local development and never the default in
a deployed .env. This test does not merely check today's config default -
it inspects the actual AST of the uvicorn.run(...) call in src/main.py, so
it fails loudly if anyone ever hardcodes reload=True there again, no
matter what the current default setting is.
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "src" / "main.py"


def _find_uvicorn_run_call(tree: ast.Module) -> ast.Call:
    """Locate the uvicorn.run(...) call inside the __main__ guard.

    Description: walks the module AST for a Call node whose function is
        `uvicorn.run`.
    Inputs: tree (ast.Module) - the parsed src/main.py module.
    Outputs: ast.Call - the matching call node.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "uvicorn"
        ):
            return node
    raise AssertionError(
        "src/main.py no longer contains a uvicorn.run(...) call - "
        "this test needs updating, but first confirm reload is still "
        "not hardcoded on wherever the server is started."
    )


def test_uvicorn_run_reload_is_not_hardcoded_true():
    """The reload= keyword must never be a literal True.

    It must be a NAME or ATTRIBUTE expression (e.g. settings.dev_reload) -
    something read from configuration - not a hardcoded boolean constant.
    A hardcoded True is exactly the bug this test exists to catch; a
    hardcoded False would also be wrong in a quieter way (no dev opt-in
    path), so both constant forms are rejected.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"), filename=str(MAIN_PY))
    call = _find_uvicorn_run_call(tree)

    reload_kwarg = None
    for kw in call.keywords:
        if kw.arg == "reload":
            reload_kwarg = kw
            break

    assert reload_kwarg is not None, (
        "uvicorn.run(...) in src/main.py no longer passes reload= "
        "explicitly - uvicorn defaults reload to False, which is safe, "
        "but make it explicit so this stays auditable."
    )

    value = reload_kwarg.value
    is_hardcoded_constant = isinstance(value, ast.Constant) and isinstance(
        value.value, bool
    )
    assert not is_hardcoded_constant, (
        "uvicorn.run(..., reload=...) in src/main.py is a hardcoded "
        f"boolean literal ({value.value!r}), not a config-driven value. "
        "Production must never run a file-watching reloader - point "
        "reload= at settings.dev_reload (CLOUDE_DEV_RELOAD env var, "
        "defaulting False) instead of a constant."
    )


def test_dev_reload_setting_defaults_false(monkeypatch, tmp_path):
    """settings.dev_reload must default to False when CLOUDE_DEV_RELOAD is unset.

    This is the config-side half of the guard: even if reload= is wired
    to settings.dev_reload correctly, a default of True would reintroduce
    the same production incident silently.
    """
    monkeypatch.delenv("CLOUDE_DEV_RELOAD", raising=False)
    monkeypatch.setenv("DEFAULT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("TOTP_SECRET", "testsecretnotreal")
    monkeypatch.setenv("JWT_SECRET", "testjwtnotreal")
    # Isolate from any real .env file that might set CLOUDE_DEV_RELOAD=1
    # on a developer's own machine. Construct Settings directly rather
    # than reloading src.config: other already-imported modules hold a
    # reference to the ORIGINAL settings singleton and to the original
    # Settings class object, and reloading the module would swap both out
    # from under them (e.g. isinstance checks against the stale class),
    # which is exactly the kind of test-order-dependent breakage this file
    # must not introduce.
    from src.config import Settings

    fresh_settings = Settings(_env_file=None)
    assert fresh_settings.dev_reload is False, (
        "Settings.dev_reload defaulted to True with CLOUDE_DEV_RELOAD "
        "unset - this must default False so a deployed .env that "
        "never mentions the var still gets a non-reloading server."
    )
