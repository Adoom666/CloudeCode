"""The upgrade merge must not report a live setting as removed upstream.

THE DEFECT THESE TESTS PIN

Run against the real config.json on 2026-08-20, the merge printed:

    REMOVED UPSTREAM  terminal_commands
    REMOVED UPSTREAM  config_version

Both keys are live: ``terminal_commands`` is imported by src/config.py and has
a real default factory, and ``config_version`` is a declared AuthConfig field
the loader reads. They were absent from ``config.example.json`` because that
file is a hand-maintained sample that had gone stale, not because upstream
removed anything. The merge derived "what upstream ships" from the example, so
a gap in the sample read as a deletion in the product.

Every test below states which half of that it pins: that the verdict is now
right, or that the tool refuses to give a verdict it cannot support.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Suite convention (see tests/test_agent_wrappers_api.py): src/config.py
# instantiates Settings() at import and exits the process when .env is absent,
# and the repo has no .env, so any test module that imports it seeds the
# environment itself first. Only one test here needs src.config at all - the
# rest deliberately exercise the import-safe path scripts/config_upgrade.py
# uses, which is the whole reason src/core/auth_defaults.py exists.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_cfgdef_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_cfgdef_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.core.config_defaults import (
    OPTIONAL_KEYS,
    effective_shipped_defaults,
    shipped_defaults,
    supported_keys,
)
from src.core.config_merge import (
    CANNOT_DETERMINE,
    REMOVED_UPSTREAM,
    merge_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two keys whose misclassification is the whole reason this module exists.
REGRESSION_KEYS = ("terminal_commands", "config_version")


def _merge_like_the_tool(mine: dict, example: dict, base: dict | None = None):
    """Run a merge the way scripts/config_upgrade.py runs it.

    Keeping this in one place means a test cannot accidentally assert against
    a call shape the real tool does not use.

    Args:
        mine: The user's configuration.
        example: Parsed config.example.json.
        base: Recorded previous defaults, or None.

    Returns:
        The MergeResult.
    """
    effective = effective_shipped_defaults(example, mine)
    stale = frozenset(k for k in effective if k not in example)
    return merge_config(
        mine,
        effective,
        base,
        supported_keys=supported_keys(),
        stale_roots=stale,
    )


class TestTheReportedDefect:
    """The exact two keys from the bug report, classified correctly."""

    @pytest.mark.parametrize("key", REGRESSION_KEYS)
    def test_live_key_absent_from_example_is_not_called_removed(self, key):
        """A key the model supports is never REMOVED UPSTREAM."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        assert key not in example, (
            f"{key} is now in config.example.json, so this test no longer "
            "exercises the stale-example path it was written for. Confirm the "
            "merge still handles a stale example before deleting it."
        )

        mine = dict(example)
        mine[key] = shipped_defaults()[key]

        result = _merge_like_the_tool(mine, example)
        outcomes = {d.path: d.outcome for d in result.decisions}
        assert outcomes[key] != REMOVED_UPSTREAM

    @pytest.mark.parametrize("key", REGRESSION_KEYS)
    def test_live_key_at_the_shipped_default_needs_no_attention(self, key):
        """Holding the shipped default is not a thing to bother a human with."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        mine = dict(example)
        mine[key] = shipped_defaults()[key]

        result = _merge_like_the_tool(mine, example)
        flagged = {d.path for d in result.needing_attention()}
        assert key not in flagged

    @pytest.mark.parametrize("key", REGRESSION_KEYS)
    def test_the_users_value_is_never_discarded(self, key):
        """Whatever the verdict, his value survives the merge."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        mine = dict(example)
        sentinel = 4 if key == "config_version" else [{"label": "x", "command": "x"}]
        mine[key] = sentinel

        result = _merge_like_the_tool(mine, example)
        assert result.merged[key] == sentinel


class TestRemovalRequiresEvidence:
    """REMOVED UPSTREAM is a measurement, not a default."""

    def test_key_the_loader_does_not_read_is_reported_removed(self):
        """Positive evidence: the running code genuinely ignores this key."""
        example = {"agents": {}}
        mine = {"agents": {}, "a_setting_no_version_ever_had": 1}

        result = _merge_like_the_tool(mine, example)
        outcomes = {d.path: d.outcome for d in result.decisions}
        assert outcomes["a_setting_no_version_ever_had"] == REMOVED_UPSTREAM

    def test_child_of_a_model_filled_subtree_is_cannot_determine(self):
        """A document that never mentioned a subtree says nothing about it.

        Guards the general case rather than only today's two keys: if a future
        model default is a mapping, the example's silence about it must not
        turn every one of its children into a phantom deletion.
        """
        example = {"agents": {}}
        mine = {"agents": {}, "session": {"a_child": 1}}

        # `session` is a supported key whose model default is {}, so the
        # example omitting it makes it a stale root.
        result = _merge_like_the_tool(mine, example)
        outcomes = {d.path: d.outcome for d in result.decisions}
        assert outcomes["session.a_child"] == CANNOT_DETERMINE

    def test_complete_defaults_still_allow_a_removal_verdict(self):
        """Callers who really do hold the full defaults are not hobbled.

        merge_config with supported_keys=None keeps the old, stronger claim,
        because in that mode the caller has asserted the defaults are complete.
        """
        result = merge_config({"gone": 1}, {}, None)
        assert result.decisions[0].outcome == REMOVED_UPSTREAM


class TestDefaultsTableTracksTheLoader:
    """The table cannot silently drift back out of date."""

    def test_every_loader_key_is_declared(self):
        """Read src/config.py and demand the table covers what it reads.

        This is the guard that makes the fix durable. The original defect was
        a second list going stale; a fix that adds a THIRD list with no check
        would just reschedule it.
        """
        source = (REPO_ROOT / "src" / "config.py").read_text()
        loader_start = source.index("def load_auth_config")
        loader_end = source.index("\n    def ", loader_start + 1)
        loader = source[loader_start:loader_end]

        read_keys = set(re.findall(r'data\.get\(\s*"([a-z_]+)"', loader))
        # TERMINAL_COMMANDS_KEY is read via the constant, not a literal.
        read_keys.add("terminal_commands")

        declared = supported_keys()
        missing = sorted(read_keys - declared)
        assert not missing, (
            "src/config.py's loader reads these keys but "
            "src/core/config_defaults.py does not declare them, which is "
            f"exactly the drift that caused the original defect: {missing}"
        )

    def test_optional_keys_carry_no_invented_default(self):
        """An unset optional must not be written down as an explicit null."""
        for key in OPTIONAL_KEYS:
            assert key not in shipped_defaults()
            assert key in supported_keys()

    def test_defaults_are_not_shared_between_calls(self):
        """A caller mutating the result cannot poison the next one."""
        first = shipped_defaults()
        first["terminal_commands"].append({"label": "poison", "command": "x"})
        assert not any(
            entry.get("label") == "poison"
            for entry in shipped_defaults()["terminal_commands"]
        )

    def test_scalar_defaults_agree_with_the_pydantic_model(self):
        """One number, one place. Catches a hand-copied literal drifting.

        Imports AuthConfig inside the test because src/config.py exits the
        process when .env is absent; under pytest conftest has already made
        the environment valid.
        """
        from src.config import AuthConfig

        table = shipped_defaults()
        for name in (
            "config_version",
            "jwt_expiry_minutes",
            "access_token_ttl_seconds",
            "refresh_token_ttl_seconds",
            "refresh_grace_seconds",
        ):
            model_default = AuthConfig.model_fields[name].get_default(
                call_default_factory=True
            )
            assert table[name] == model_default, name


class TestExamplePrecedence:
    """The example still wins where it speaks."""

    def test_example_value_beats_the_model_default(self):
        """The curated value is the shipped one; the model only fills gaps."""
        example = {"jwt_expiry_minutes": 45}
        effective = effective_shipped_defaults(example, {"jwt_expiry_minutes": 1})
        assert effective["jwt_expiry_minutes"] == 45

    def test_model_only_fills_keys_the_user_actually_has(self):
        """Merging must not expand everyone's config.json with new settings."""
        effective = effective_shipped_defaults({"agents": {}}, {"agents": {}})
        assert "refresh_grace_seconds" not in effective

    def test_effective_defaults_never_alias_their_inputs(self):
        """Mutating the result cannot reach back into the caller's data."""
        example = {"agents": {"nested": [1]}}
        effective = effective_shipped_defaults(example, {"agents": {}})
        effective["agents"]["nested"].append(2)
        assert example["agents"]["nested"] == [1]


class TestTheCommandLineToolEndToEnd:
    """The shipped entry point, not just the library underneath it."""

    def test_dry_run_on_a_stale_example_reports_no_removals(self, tmp_path):
        """The user-visible output, which is what he actually complained about."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        mine = dict(example)
        for key in REGRESSION_KEYS:
            mine[key] = shipped_defaults()[key]

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(mine, indent=2))

        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "config_upgrade.py"),
                "--config",
                str(config_path),
                "--state-dir",
                str(tmp_path / "state"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        assert "REMOVED UPSTREAM" not in proc.stdout, proc.stdout
        for key in REGRESSION_KEYS:
            assert key not in proc.stdout, proc.stdout

    def test_apply_records_the_effective_base_not_the_raw_example(self, tmp_path):
        """Otherwise the repaired keys report CANNOT DETERMINE forever."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        mine = dict(example)
        mine["config_version"] = 4
        mine["terminal_commands"] = shipped_defaults()["terminal_commands"]

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(mine, indent=2))
        state_dir = tmp_path / "state"

        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "config_upgrade.py"),
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--apply",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        base = json.loads((state_dir / "config-base.json").read_text())
        assert "config_version" in base
        assert "terminal_commands" in base

        # Second run: with a base recorded, the ambiguity must be gone.
        second = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "config_upgrade.py"),
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert "CANNOT DETERMINE  config_version" not in second.stdout, second.stdout
