"""The setup wizard's auth gate and the bind lockdown that makes it safe.

WHY THIS FILE IS THE ONE THAT MATTERS

A bug in the merge report is a bad afternoon. A bug here is a stranger on the
LAN reaching an unauthenticated page that sets the TOTP secret, which is a
remote takeover of the machine this software runs terminals on.

So the tests are written from the attacker's side. The easy direction - "an
unconfigured instance lets the wizard load" - is asserted, but it is the weak
half: a gate that is broken OPEN passes it too. The half that carries the
weight is the other one, that a CONFIGURED instance genuinely refuses an
unauthenticated caller, and the invariant test below, which asserts across
every reachable state that an open wizard and an off-host bind never coexist.

``scripts/ci/mutate-setup-auth-gate.sh`` inverts the gate in five distinct
ways and requires this file to fail on each, so "these tests pass" is backed by
evidence that they can fail.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_setupw_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_setupw_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

from src.core.setup_state import (  # noqa: E402
    LOOPBACK_HOST,
    SETUP_COMPLETE,
    SETUP_INCOMPLETE,
    SETUP_UNDETERMINED,
    Exposure,
    evaluate_setup_state,
    mark_setup_complete,
    resolve_exposure,
)


def _state(tmp_path: Path, *, config=True, totp=True, jwt=True, paired=True):
    """Build a setup state from an explicit combination of facts.

    Args:
        tmp_path: Directory to place config.json and the sentinel in.
        config: Write a valid config.json.
        totp: Supply a TOTP secret.
        jwt: Supply a JWT secret.
        paired: Write the .totp_paired sentinel.

    Returns:
        The evaluated SetupState.
    """
    config_path = tmp_path / "config.json"
    if config:
        config_path.write_text(json.dumps({"agents": {}}))
    if paired:
        (tmp_path / ".totp_paired").write_text("")
    return evaluate_setup_state(
        config_path,
        "JBSWY3DPEHPK3PXP" if totp else None,
        "a-jwt-secret" if jwt else None,
    )


class TestSetupStateIsMeasuredNotDeclared:
    """Setup completeness comes from evidence, not from a flag."""

    def test_everything_present_is_complete(self, tmp_path):
        assert _state(tmp_path).status == SETUP_COMPLETE

    @pytest.mark.parametrize("missing", ["config", "totp", "jwt", "paired"])
    def test_any_missing_fact_is_incomplete(self, tmp_path, missing):
        """Each fact is load-bearing on its own."""
        state = _state(tmp_path, **{missing: False})
        assert state.status == SETUP_INCOMPLETE
        assert not state.is_complete

    def test_paired_sentinel_is_required_even_with_a_secret(self, tmp_path):
        """A secret nobody has scanned does not give the instance an owner.

        This is the distinction a config boolean could not have made, and it
        is the reason the sentinel is the fourth check rather than three.
        """
        state = _state(tmp_path, paired=False)
        assert not state.is_complete
        outstanding = {c.key for c in state.outstanding()}
        assert outstanding == {"totp_paired"}

    def test_unparseable_config_is_undetermined_not_complete(self, tmp_path):
        """THREE-OUTCOME RULE. 'I could not read it' is never 'it is fine'."""
        config_path = tmp_path / "config.json"
        config_path.write_text("{not json")
        (tmp_path / ".totp_paired").write_text("")
        state = evaluate_setup_state(config_path, "secret", "jwt")
        assert state.status == SETUP_UNDETERMINED
        assert not state.is_complete
        check = next(c for c in state.checks if c.key == "config_file")
        assert check.passed is None
        assert "cannot be determined" in check.detail

    def test_a_config_flag_cannot_declare_setup_complete(self, tmp_path):
        """Editing configuration must not be able to lift the lockdown.

        Every plausible spelling of "I am set up" is written into the config
        and the verdict must not move. If a future edit starts reading a flag
        out of the file, this fails.
        """
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "setup_complete": True,
                    "setup": {"complete": True},
                    "first_run": False,
                    "configured": True,
                    "totp_paired": True,
                }
            )
        )
        state = evaluate_setup_state(config_path, "secret", "jwt")
        assert not state.is_complete


class TestTheInvariant:
    """An unauthenticated wizard is only ever served on loopback."""

    ALL_COMBINATIONS = [
        (config, totp, jwt, paired)
        for config in (True, False)
        for totp in (True, False)
        for jwt in (True, False)
        for paired in (True, False)
    ]

    @pytest.mark.parametrize("combo", ALL_COMBINATIONS)
    @pytest.mark.parametrize(
        "configured_host", ["0.0.0.0", "127.0.0.1", "192.168.1.40", "100.64.0.1", "::"]
    )
    def test_open_wizard_implies_loopback(self, tmp_path, combo, configured_host):
        """The whole security property, checked over every reachable state.

        80 combinations of setup facts crossed with bind addresses. Not one of
        them may produce a wizard that answers without a credential on an
        address something else can reach.
        """
        config, totp, jwt, paired = combo
        state = _state(tmp_path, config=config, totp=totp, jwt=jwt, paired=paired)
        exposure = resolve_exposure(configured_host, state)

        if not exposure.wizard_requires_auth:
            assert exposure.bind_host == LOOPBACK_HOST, (
                f"{combo} on {configured_host} would serve an unauthenticated "
                f"wizard on {exposure.bind_host}"
            )

    def test_incomplete_pins_loopback_over_any_configured_address(self, tmp_path):
        exposure = resolve_exposure("0.0.0.0", _state(tmp_path, paired=False))
        assert exposure.bind_host == LOOPBACK_HOST
        assert exposure.locked_down is True
        assert exposure.configured_bind_host == "0.0.0.0"

    def test_undetermined_fails_closed(self, tmp_path):
        """A state we cannot read must not be treated as a set-up one."""
        config_path = tmp_path / "config.json"
        config_path.write_text("{broken")
        (tmp_path / ".totp_paired").write_text("")
        state = evaluate_setup_state(config_path, "secret", "jwt")
        exposure = resolve_exposure("0.0.0.0", state)
        assert exposure.bind_host == LOOPBACK_HOST
        assert exposure.wizard_requires_auth is False

    def test_complete_restores_the_configured_address(self, tmp_path):
        exposure = resolve_exposure("0.0.0.0", _state(tmp_path))
        assert exposure.bind_host == "0.0.0.0"
        assert exposure.locked_down is False
        assert exposure.wizard_requires_auth is True

    def test_the_guard_refuses_to_return_the_dangerous_pair(self):
        """A future edit that breaks the coupling must crash, not serve.

        Constructing the forbidden combination by hand and pushing it through
        the same guard proves the guard is real rather than decorative.
        """
        import src.core.setup_state as module

        class FakeComplete:
            is_complete = True

        original = module.Exposure

        class Sabotaged(original):
            pass

        # Force the branch that would produce an open wizard on a wide bind by
        # patching the constructor the "complete" arm uses.
        def broken(*args, **kwargs):
            kwargs["wizard_requires_auth"] = False
            return original(*args, **kwargs)

        module.Exposure = broken
        try:
            with pytest.raises(RuntimeError, match="remote takeover"):
                module.resolve_exposure("0.0.0.0", FakeComplete())
        finally:
            module.Exposure = original


class TestRestartHonesty:
    """Finishing setup does not move a socket that is already bound."""

    def test_lockdown_reports_that_a_restart_is_required(self, tmp_path):
        exposure = resolve_exposure("0.0.0.0", _state(tmp_path, paired=False))
        assert exposure.restart_required_to_apply is True

    def test_no_restart_claimed_when_nothing_would_change(self, tmp_path):
        """Someone who configured loopback anyway is not told to restart."""
        exposure = resolve_exposure(LOOPBACK_HOST, _state(tmp_path, paired=False))
        assert exposure.restart_required_to_apply is False
        assert exposure.locked_down is False


class TestMarkSetupComplete:
    """The transition itself."""

    def test_writing_the_sentinel_flips_the_state(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({}))
        before = evaluate_setup_state(config_path, "s", "j")
        assert not before.is_complete

        mark_setup_complete(config_path)

        after = evaluate_setup_state(config_path, "s", "j")
        assert after.is_complete
        assert resolve_exposure("0.0.0.0", after).bind_host == "0.0.0.0"

    def test_marking_twice_is_not_an_error(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        mark_setup_complete(config_path)
        mark_setup_complete(config_path)
        assert (tmp_path / ".totp_paired").exists()


class TestSavedBindPreferenceCannotDefeatTheLockdown:
    """feat/settings-gui adds a place to SAVE a bind address. Prove it cannot
    widen exposure.

    The settings screen writes ``server_prefs.bind_host`` into config.json.
    That value reaches the Python process as ``HOST`` and lands as
    ``settings.host``, which is the ``configured_bind_host`` argument to
    ``resolve_exposure`` - upstream of the single decision point, never
    beside it. These cases cross every value the new validator will ACCEPT
    with every setup state, which is a superset of what the class above
    checks: it uses a fixed list of addresses, this one uses the ones a user
    can actually get stored.

    The complementary half of the argument lives in
    tests/test_workspace_settings.py (the validator refuses an address this
    machine does not hold). Neither test alone is the proof; together they
    say a stored preference is both plausible and powerless.
    """

    #: Everything ``validate_bind_host`` returns non-empty for, on a machine
    #: holding 192.168.1.40. Deliberately includes the widest one.
    ACCEPTABLE_PREFERENCES = ["127.0.0.1", "0.0.0.0", "192.168.1.40"]

    @pytest.mark.parametrize("preference", ACCEPTABLE_PREFERENCES)
    @pytest.mark.parametrize("combo", TestTheInvariant.ALL_COMBINATIONS)
    def test_a_stored_preference_never_lifts_the_lockdown(
        self, tmp_path, preference, combo
    ):
        config, totp, jwt, paired = combo
        state = _state(tmp_path, config=config, totp=totp, jwt=jwt, paired=paired)
        exposure = resolve_exposure(preference, state)
        if not state.is_complete:
            assert exposure.bind_host == LOOPBACK_HOST, (
                f"stored preference {preference} escaped the lockdown in "
                f"state {combo}"
            )
        if not exposure.wizard_requires_auth:
            assert exposure.bind_host == LOOPBACK_HOST

    def test_a_stored_preference_is_reported_as_pending_not_as_in_force(
        self, tmp_path
    ):
        """The UI must be able to say the socket has not moved.

        uvicorn binds once. A settings screen that showed the saved value as
        the current one would be showing an aspiration as a fact - the exact
        defect the bind lockdown makes easy.
        """
        exposure = resolve_exposure("0.0.0.0", _state(tmp_path, paired=False))
        assert exposure.restart_required_to_apply is True
        assert exposure.configured_bind_host == "0.0.0.0"
        assert exposure.bind_host == LOOPBACK_HOST

    def test_validator_and_resolver_agree_on_the_universal_addresses(self):
        """The two ends of the pipe must not disagree about what is storable.

        A value the validator accepts and the resolver cannot take would be a
        setting that saves and then strands the server.
        """
        from src.core.workspace_settings import (
            UNIVERSAL_BIND_HOSTS,
            validate_bind_host,
        )

        for host in UNIVERSAL_BIND_HOSTS:
            assert validate_bind_host(host) == host
