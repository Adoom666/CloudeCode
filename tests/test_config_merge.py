"""Tests for the upgrade-aware three-way merge of ``config.json``.

WHY THIS FILE EXISTS

``config.json`` is authoritative for agent wrappers and slash commands, so
getting an upgrade wrong either destroys the user's customisations or
silently withholds every new default.

The assertions below are organised around the distinction that matters, and
that a two-way diff cannot make at all:

    a field he never touched
    a field he changed
    a field he changed where the default ALSO changed

Only the third is a conflict, and it must never be resolved silently. A merge
that treats the first and second the same either clobbers his edits or freezes
him on old defaults forever, and both look like success from the outside.

The fourth case has its own tests: with no recorded base, "he changed it" and
"the default changed" are genuinely indistinguishable, and the merge must say
CANNOT DETERMINE rather than pick. That is the three-outcome rule applied to a
merge.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config_merge import (  # noqa: E402
    ADDED,
    CANNOT_DETERMINE,
    CONFLICT,
    KEPT_CUSTOM,
    REMOVED_UPSTREAM,
    UNCHANGED,
    UPDATED_DEFAULT,
    apply_import,
    merge_config,
)


def outcome_for(result, path: str) -> str:
    """Look up one field's outcome.

    Args:
        result: A MergeResult.
        path: Dotted field path.

    Returns:
        The outcome string.

    Raises:
        AssertionError: The path was never classified.
    """
    for decision in result.decisions:
        if decision.path == path:
            return decision.outcome
    raise AssertionError(f"no decision recorded for {path}: "
                         f"{[d.path for d in result.decisions]}")


def chosen_for(result, path: str):
    """Value the merge selected for one field.

    Args:
        result: A MergeResult.
        path: Dotted field path.

    Returns:
        The chosen value.
    """
    for decision in result.decisions:
        if decision.path == path:
            return decision.chosen
    raise AssertionError(f"no decision recorded for {path}")


class TestTheThreeCases:
    """The distinction a two-way diff cannot make."""

    def test_untouched_field_receives_the_new_default(self) -> None:
        """He had the old default, so the new one arrives."""
        result = merge_config(
            mine={"jwt_expiry_minutes": 30},
            theirs={"jwt_expiry_minutes": 45},
            base={"jwt_expiry_minutes": 30},
        )
        assert outcome_for(result, "jwt_expiry_minutes") == UPDATED_DEFAULT
        assert result.merged["jwt_expiry_minutes"] == 45

    def test_customised_field_survives_when_the_default_is_unchanged(self) -> None:
        """His edit is not reverted to the shipped default."""
        result = merge_config(
            mine={"agents": {"codex_command": "MINE"}},
            theirs={"agents": {"codex_command": "codex"}},
            base={"agents": {"codex_command": "codex"}},
        )
        assert outcome_for(result, "agents.codex_command") == KEPT_CUSTOM
        assert result.merged["agents"]["codex_command"] == "MINE"

    def test_both_changed_is_a_conflict_and_keeps_his_value(self) -> None:
        """The case that must never be auto-merged."""
        result = merge_config(
            mine={"agents": {"codex_command": "MINE"}},
            theirs={"agents": {"codex_command": "codex-v2"}},
            base={"agents": {"codex_command": "codex"}},
        )
        assert outcome_for(result, "agents.codex_command") == CONFLICT
        assert result.merged["agents"]["codex_command"] == "MINE", (
            "a conflict must never silently adopt the upstream value"
        )

    def test_a_conflict_is_reported_not_buried(self) -> None:
        """A conflict has to reach the user, with both values."""
        result = merge_config(
            mine={"a": "MINE"}, theirs={"a": "THEIRS"}, base={"a": "OLD"}
        )
        attention = result.needing_attention()
        assert len(attention) == 1
        decision = attention[0]
        assert decision.mine == "MINE"
        assert decision.theirs == "THEIRS"
        assert decision.base == "OLD"
        assert decision.note, "a conflict must explain itself"

    def test_the_three_cases_produce_three_different_outcomes(self) -> None:
        """Guards against any future collapse of the classification."""
        result = merge_config(
            mine={"untouched": 1, "customised": "MINE", "both": "MINE"},
            theirs={"untouched": 2, "customised": "d", "both": "THEIRS"},
            base={"untouched": 1, "customised": "d", "both": "OLD"},
        )
        outcomes = {
            outcome_for(result, "untouched"),
            outcome_for(result, "customised"),
            outcome_for(result, "both"),
        }
        assert len(outcomes) == 3, f"cases collapsed together: {outcomes}"


class TestNoRecordedBase:
    """Without a base, two of the cases are indistinguishable."""

    def test_a_differing_field_is_cannot_determine_not_a_guess(self) -> None:
        """The merge must not invent a classification it cannot support."""
        result = merge_config(mine={"a": "MINE"}, theirs={"a": "THEIRS"}, base=None)
        assert outcome_for(result, "a") == CANNOT_DETERMINE
        assert result.had_base is False

    def test_cannot_determine_still_keeps_his_value(self) -> None:
        """Ambiguity is never resolved by overwriting him."""
        result = merge_config(mine={"a": "MINE"}, theirs={"a": "THEIRS"}, base=None)
        assert result.merged["a"] == "MINE"

    def test_cannot_determine_reaches_the_attention_list(self) -> None:
        """An unevaluable field must not be skipped so the run looks clean."""
        result = merge_config(mine={"a": "MINE"}, theirs={"a": "THEIRS"}, base=None)
        assert [d.path for d in result.needing_attention()] == ["a"]

    def test_identical_fields_are_still_quiet_without_a_base(self) -> None:
        """Only genuinely ambiguous fields are noisy, not every field."""
        result = merge_config(mine={"a": 1, "b": 2}, theirs={"a": 1, "b": 2}, base=None)
        assert result.needing_attention() == []
        assert outcome_for(result, "a") == UNCHANGED


class TestAdditionsAndRemovals:
    """New and departed settings."""

    def test_a_brand_new_setting_is_added(self) -> None:
        """He cannot have an opinion about a field he has never seen."""
        result = merge_config(mine={}, theirs={"new": "v"}, base={})
        assert outcome_for(result, "new") == ADDED
        assert result.merged["new"] == "v"

    def test_a_setting_dropped_upstream_is_kept_and_reported(self) -> None:
        """Deleting his configuration is a loss, not a merge.

        This is not hypothetical: the user's live config carries
        ``terminal_commands`` with three commands he wrote, and that key is
        absent from the shipped example. Copying the example over the top
        would have destroyed them with no warning.
        """
        result = merge_config(
            mine={"terminal_commands": [{"id": "top", "command": "htop"}]},
            theirs={},
            base={},
        )
        assert outcome_for(result, "terminal_commands") == REMOVED_UPSTREAM
        assert result.merged["terminal_commands"] == [{"id": "top", "command": "htop"}]
        assert result.needing_attention(), "an upstream removal must be reported"

    def test_nested_maps_are_merged_key_by_key(self) -> None:
        """A nested map must not be replaced wholesale."""
        result = merge_config(
            mine={"agents": {"claude_command": "claude", "codex_command": "MINE"}},
            theirs={"agents": {"claude_command": "claude", "hermes_command": "hermes"}},
            base={"agents": {"claude_command": "claude", "codex_command": "codex"}},
        )
        agents = result.merged["agents"]
        assert agents["codex_command"] == "MINE", "his edit was dropped"
        assert agents["hermes_command"] == "hermes", "the new default never arrived"

    def test_comment_keys_always_follow_the_new_defaults(self) -> None:
        """Stale documentation must not be pinned as if it were a setting."""
        result = merge_config(
            mine={"_comment_x": "old prose"},
            theirs={"_comment_x": "new prose"},
            base={"_comment_x": "old prose"},
        )
        assert result.merged["_comment_x"] == "new prose"


class TestListsAreAtomic:
    """Lists are never merged element-wise, and new items are offered."""

    def test_a_customised_list_is_not_rewritten(self) -> None:
        """Element-wise list merging is where customisations get eaten."""
        result = merge_config(
            mine={"cmds": ["/review", "/mine"]},
            theirs={"cmds": ["/review", "/plan"]},
            base={"cmds": ["/review"]},
        )
        assert result.merged["cmds"] == ["/review", "/mine"]

    def test_new_upstream_items_are_offered_but_not_applied(self) -> None:
        """Offering is safe; appending without being asked is not."""
        result = merge_config(
            mine={"cmds": ["/review", "/mine"]},
            theirs={"cmds": ["/review", "/plan", "/test"]},
            base={"cmds": ["/review"]},
        )
        assert result.importable["cmds"] == ["/plan", "/test"]
        assert "/plan" not in result.merged["cmds"], "applied without being asked"

    def test_an_explicit_import_appends_and_preserves_his_order(self) -> None:
        """Reordering somebody's curated list is a change they did not ask for."""
        merged = apply_import({"cmds": ["/review", "/mine"]}, "cmds", ["/plan", "/test"])
        assert merged["cmds"] == ["/review", "/mine", "/plan", "/test"]

    def test_importing_twice_does_not_duplicate(self) -> None:
        """Import is idempotent."""
        once = apply_import({"cmds": ["/a"]}, "cmds", ["/b"])
        twice = apply_import(once, "cmds", ["/b"])
        assert twice["cmds"] == ["/a", "/b"]

    def test_importing_into_a_non_list_raises(self) -> None:
        """Better a loud error than a silently mangled field."""
        with pytest.raises(KeyError):
            apply_import({"a": "scalar"}, "a", ["x"])


class TestTheRealConfigFiles:
    """Exercise the merge against the files actually in the repo."""

    def test_the_shipped_example_merges_against_itself_cleanly(self) -> None:
        """A fresh install has nothing to resolve."""
        example = json.loads((REPO_ROOT / "config.example.json").read_text())
        result = merge_config(mine=example, theirs=example, base=example)
        assert result.needing_attention() == []
        assert result.changes() == []


class TestTheCommandLineTool:
    """End-to-end behaviour of scripts/config_upgrade.py."""

    def _run(self, tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess:
        """Invoke the tool with a sandboxed state directory."""
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "config_upgrade.py"), *args,
             "--state-dir", str(tmp_path / "state")],
            capture_output=True,
            text=True,
        )

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        """The default must never modify the file."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"a": "MINE"}))
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"a": "THEIRS"}))
        before = config.read_text()

        self._run(tmp_path, ["--config", str(config), "--defaults", str(defaults)])

        assert config.read_text() == before, "a dry run modified the config"

    def test_needing_attention_exits_2_not_0(self, tmp_path: Path) -> None:
        """An upgrade script must not sail past an unresolved conflict."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"a": "MINE"}))
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"a": "THEIRS"}))

        proc = self._run(tmp_path, ["--config", str(config), "--defaults", str(defaults)])
        assert proc.returncode == 2, proc.stdout + proc.stderr

    def test_apply_backs_up_before_writing(self, tmp_path: Path) -> None:
        """A verified copy exists before the file is touched."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"a": 1}))
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"a": 1, "b": 2}))

        proc = self._run(
            tmp_path, ["--config", str(config), "--defaults", str(defaults), "--apply"]
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        backups = list((tmp_path / "state" / "config-backups").glob("config.*.json"))
        assert len(backups) == 1, "no backup was taken"
        assert json.loads(backups[0].read_text()) == {"a": 1}

        manifest = (tmp_path / "state" / "config-backups" / ".manifest").read_text()
        assert "BACKED_UP" in manifest

    def test_apply_records_a_base_so_the_next_upgrade_can_classify(
        self, tmp_path: Path
    ) -> None:
        """The recorded base is what removes the CANNOT DETERMINE noise."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"a": 1}))
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"a": 1}))

        self._run(
            tmp_path, ["--config", str(config), "--defaults", str(defaults), "--apply"]
        )
        base = tmp_path / "state" / "config-base.json"
        assert base.exists(), "no base recorded; every later upgrade stays ambiguous"
        assert json.loads(base.read_text()) == {"a": 1}

    def test_corrupt_config_refuses_to_merge(self, tmp_path: Path) -> None:
        """Never treat unparseable configuration as absent."""
        config = tmp_path / "config.json"
        config.write_text("{ this is not json")
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"a": 1}))

        proc = self._run(
            tmp_path, ["--config", str(config), "--defaults", str(defaults), "--apply"]
        )
        assert proc.returncode == 1
        assert config.read_text() == "{ this is not json", "corrupt file was overwritten"
