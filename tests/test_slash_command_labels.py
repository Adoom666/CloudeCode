"""Tests for the common-slash-command short descriptions.

The load-bearing property is BACKWARD COMPATIBILITY: a config.json that
predates this feature holds a plain list of strings, and it must keep
working with no migration. Half of these tests exist to pin that.
"""

import json

import pytest

from src.core import slash_command_labels as labels


class TestDescribe:
    def test_known_command_has_description(self):
        assert labels.describe("/clear") == "wipe conversation"

    def test_leading_slash_is_optional(self):
        assert labels.describe("clear") == labels.describe("/clear")

    def test_unknown_command_is_empty_not_an_error(self):
        # Users add their own commands; an unknown one must render as a
        # bare chip, never blow up the whole row.
        assert labels.describe("/totally-made-up") == ""

    def test_non_string_is_empty(self):
        assert labels.describe(None) == ""
        assert labels.describe(42) == ""

    def test_blank_is_empty(self):
        assert labels.describe("   ") == ""


class TestNormalizeBackwardCompatibility:
    """A bare-string config must keep working. This is the contract."""

    def test_plain_string_list_still_works(self):
        out = labels.normalize(["/clear", "/compact"])
        assert out == [
            {"command": "/clear", "description": "wipe conversation"},
            {"command": "/compact", "description": "summarize history"},
        ]

    def test_plain_string_list_preserves_order(self):
        given = ["/usage", "/clear", "/mcp"]
        assert labels.commands_only(labels.normalize(given)) == given

    def test_unknown_command_in_string_list_survives(self):
        out = labels.normalize(["/my-custom"])
        assert out == [{"command": "/my-custom", "description": ""}]

    def test_the_documented_config_values_all_resolve(self):
        # The exact list seen in a live config.json. Every one of these
        # must get a description from the built-in table, or a user who
        # never edits their config sees a row of blank second lines.
        given = [
            "/agents", "/clear", "/compact", "/context",
            "/hooks", "/mcp", "/resume", "/rewind", "/usage",
        ]
        out = labels.normalize(given)
        assert all(entry["description"] for entry in out)

    def test_empty_and_none_normalize_to_empty_list(self):
        assert labels.normalize([]) == []
        assert labels.normalize(None) == []


class TestNormalizeObjectForm:
    def test_object_description_wins_over_table(self):
        out = labels.normalize([{"command": "/clear", "description": "nuke it"}])
        assert out == [{"command": "/clear", "description": "nuke it"}]

    def test_object_without_description_falls_back_to_table(self):
        out = labels.normalize([{"command": "/clear"}])
        assert out == [{"command": "/clear", "description": "wipe conversation"}]

    def test_mixed_forms_in_one_list(self):
        out = labels.normalize([
            "/clear",
            {"command": "/deploy", "description": "ship it"},
        ])
        assert out == [
            {"command": "/clear", "description": "wipe conversation"},
            {"command": "/deploy", "description": "ship it"},
        ]

    def test_entry_without_a_command_is_dropped(self):
        # An empty chip is worse than a missing one.
        assert labels.normalize([{"description": "orphan"}]) == []
        assert labels.normalize([""]) == []

    def test_junk_entry_types_are_dropped(self):
        assert labels.normalize([None, 7, ["/clear"]]) == []


class TestDescriptionsStayShort:
    """The whole point of the change is that these fit on a phone."""

    def test_every_builtin_description_is_within_the_cap(self):
        too_long = {
            cmd: desc
            for cmd, desc in labels.SHORT_DESCRIPTIONS.items()
            if len(desc) > labels.MAX_DESCRIPTION_LENGTH
        }
        assert too_long == {}, f"over {labels.MAX_DESCRIPTION_LENGTH} chars: {too_long}"

    def test_every_builtin_description_is_a_single_line(self):
        assert not [d for d in labels.SHORT_DESCRIPTIONS.values() if "\n" in d]

    def test_every_builtin_description_is_lowercase_first(self):
        # House style is lowercase UI copy. "/init" is the one legitimate
        # exception because CLAUDE.md is a literal filename.
        offenders = [
            (c, d) for c, d in labels.SHORT_DESCRIPTIONS.items()
            if d and d[0].isupper()
        ]
        assert offenders == []

    def test_no_emdash_or_endash_in_descriptions(self):
        offenders = [
            (c, d) for c, d in labels.SHORT_DESCRIPTIONS.items()
            if "—" in d or "–" in d
        ]
        assert offenders == []

    def test_defaults_are_all_described(self):
        for cmd in labels.DEFAULT_COMMON_COMMANDS:
            assert labels.describe(cmd), f"{cmd} has no short description"


class TestExampleConfigStillParses:
    def test_config_example_common_commands_normalize(self, tmp_path):
        from pathlib import Path
        example = Path(__file__).parent.parent / "config.example.json"
        data = json.loads(example.read_text())
        out = labels.normalize(data["common_slash_commands"])
        # Every entry survived, in order, with a description.
        assert len(out) == len(data["common_slash_commands"])
        assert all(e["command"].startswith("/") for e in out)
        assert all(e["description"] for e in out)


class TestCommandsOnly:
    def test_projects_back_to_bare_strings(self):
        details = labels.normalize(["/clear", {"command": "/x", "description": "y"}])
        assert labels.commands_only(details) == ["/clear", "/x"]


class TestEntryCommand:
    """Reads the command out of a RAW entry, without normalizing the list.
    A config migration needs this: it must know what an entry names while
    still writing the user's own entry back untouched."""

    def test_reads_a_bare_string(self):
        assert labels.entry_command("/login") == "/login"

    def test_reads_an_object(self):
        assert labels.entry_command({"command": "/login", "description": "x"}) == "/login"

    def test_adds_the_missing_slash(self):
        assert labels.entry_command("login") == "/login"

    def test_unusable_entries_are_empty_not_an_error(self):
        assert labels.entry_command(None) == ""
        assert labels.entry_command("  ") == ""
        assert labels.entry_command({}) == ""


class TestAppendMissingCommands:
    def test_appends_at_the_end(self):
        assert labels.append_missing_commands(["/clear"], ["/login"]) == [
            "/clear", "/login",
        ]

    def test_skips_one_already_present_as_a_string(self):
        assert labels.append_missing_commands(["/login"], ["/login"]) == ["/login"]

    def test_skips_one_already_present_as_an_object(self):
        entries = [{"command": "/login", "description": "mine"}]
        assert labels.append_missing_commands(entries, ["/login"]) == entries

    def test_existing_entries_keep_their_original_form(self):
        entries = ["/clear", {"command": "/diff", "description": "review changes"}]
        out = labels.append_missing_commands(entries, ["/login"])
        assert out[:2] == entries

    def test_a_non_list_is_treated_as_empty(self):
        assert labels.append_missing_commands(None, ["/login"]) == ["/login"]

    def test_is_idempotent(self):
        once = labels.append_missing_commands(["/clear"], ["/login"])
        assert labels.append_missing_commands(once, ["/login"]) == once

    def test_does_not_mutate_the_input(self):
        entries = ["/clear"]
        labels.append_missing_commands(entries, ["/login"])
        assert entries == ["/clear"]

    def test_migration_list_matches_the_tail_of_the_defaults(self):
        """The two must agree or a fresh install and a migrated one end up
        with different chip rows."""
        n = len(labels.MIGRATION_APPENDED_COMMANDS)
        assert labels.DEFAULT_COMMON_COMMANDS[-n:] == labels.MIGRATION_APPENDED_COMMANDS
