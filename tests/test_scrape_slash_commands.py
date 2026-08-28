"""Tests for scripts/scrape-slash-commands.py.

Runs entirely against a SAVED FIXTURE of the docs table (see
`FIXTURE_MARKDOWN` below) — never touches the network. Covers: basic row
parsing, escaped-pipe args, alias detection, type classification (skill /
workflow / builtin), and the loud-failure guards (missing heading,
malformed header, below-floor row count).

Run with:
    python3 -m pytest tests/test_scrape_slash_commands.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRAPER_PATH = ROOT / "scripts" / "scrape-slash-commands.py"


def _load_scraper_module():
    """
    Description: import scripts/scrape-slash-commands.py as a module
        despite its hyphenated filename (not a valid Python identifier
        for a normal `import` statement).
    Inputs: none.
    Output: module object with the scraper's functions/constants.
    """
    spec = importlib.util.spec_from_file_location("scrape_slash_commands", SCRAPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrape_slash_commands"] = module
    spec.loader.exec_module(module)
    return module


scraper = _load_scraper_module()

# A trimmed, representative fixture of the real table structure verified
# against the live docs page on 2026-08-14: header, separator, then rows
# covering a plain command, escaped-pipe args, a Skill marker, a Workflow
# marker, and two alias phrasings ("Alias for `/x`" and "Alias of
# [`/x`](url): ..."). Intentionally NOT fetched live — this is the
# structure-change tripwire fixture.
FIXTURE_MARKDOWN = """
# Commands

## Commands across a typical workflow

Some intro text, not a table.

## All commands

The table below lists all the commands.

| Command | Purpose |
| :--- | :--- |
| `/add-dir <path>` | Add a working directory for file access |
| `/advisor [model\\|off]` | Enable or disable the advisor tool. Accepts `fable`, `opus`, or `sonnet` |
| `/batch <instruction>` | **[Skill](/docs/en/skills#bundled-skills).** Orchestrate large-scale changes. Example: `/batch migrate src/` |
| `/deep-research <question>` | **[Workflow](/docs/en/workflows#bundled-workflows).** Fan out web searches on a question |
| `/cost` | Alias for `/usage` |
| `/review [low\\|medium\\|high]` | Alias of [`/code-review`](/docs/en/code-review): reviews the current diff |
| `/usage` | Show session cost and plan usage limits |

## MCP prompts

Not part of the table.
"""


def test_extract_table_lines_finds_data_rows():
    rows = scraper.extract_table_lines(FIXTURE_MARKDOWN)
    assert len(rows) == 7
    assert all(r.strip().startswith("|") for r in rows)


def test_extract_table_lines_missing_heading_fails_loud():
    bad_markdown = "# Commands\n\nNo such section here.\n"
    with pytest.raises(SystemExit):
        scraper.extract_table_lines(bad_markdown)


def test_extract_table_lines_bad_header_fails_loud():
    bad_markdown = FIXTURE_MARKDOWN.replace("| Command | Purpose |", "| Name | Desc |")
    with pytest.raises(SystemExit):
        scraper.extract_table_lines(bad_markdown)


def test_split_table_row_respects_escaped_pipes():
    cells = scraper.split_table_row("| `/advisor [x\\|y]` | does a thing |")
    assert cells == ["`/advisor [x|y]`", "does a thing"]


def test_parse_command_cell_splits_name_and_args():
    assert scraper.parse_command_cell("`/cd <path>`") == ("/cd", "<path>")
    assert scraper.parse_command_cell("`/help`") == ("/help", "")


def test_parse_purpose_cell_classifies_builtin():
    description, cmd_type, alias_of = scraper.parse_purpose_cell(
        "Add a working directory for file access"
    )
    assert cmd_type == "builtin"
    assert alias_of is None
    assert description == "Add a working directory for file access"


def test_parse_purpose_cell_classifies_skill_and_strips_marker():
    description, cmd_type, alias_of = scraper.parse_purpose_cell(
        "**[Skill](/docs/en/skills#bundled-skills).** Orchestrate large-scale changes. "
        "Example: `/batch migrate src/`"
    )
    assert cmd_type == "skill"
    assert alias_of is None
    assert description.startswith("Orchestrate large-scale changes.")
    assert "[Skill]" not in description


def test_parse_purpose_cell_classifies_workflow():
    description, cmd_type, alias_of = scraper.parse_purpose_cell(
        "**[Workflow](/docs/en/workflows#bundled-workflows).** Fan out web searches on a question"
    )
    assert cmd_type == "workflow"
    assert alias_of is None


def test_parse_purpose_cell_detects_simple_alias():
    description, cmd_type, alias_of = scraper.parse_purpose_cell("Alias for `/usage`")
    assert alias_of == "/usage"
    assert description == "Alias for /usage"


def test_parse_purpose_cell_detects_linked_alias():
    description, cmd_type, alias_of = scraper.parse_purpose_cell(
        "Alias of [`/code-review`](/docs/en/code-review): reviews the current diff"
    )
    assert alias_of == "/code-review"
    assert "reviews the current diff" in description


def test_parse_rows_end_to_end_on_fixture():
    table_rows = scraper.extract_table_lines(FIXTURE_MARKDOWN)
    commands = scraper.parse_rows(table_rows)
    by_name = {c.command: c for c in commands}

    assert by_name["/add-dir"].args == "<path>"
    assert by_name["/advisor"].args == "[model|off]"
    assert by_name["/batch"].type == "skill"
    assert by_name["/deep-research"].type == "workflow"
    assert by_name["/cost"].alias_of == "/usage"
    assert by_name["/review"].alias_of == "/code-review"
    assert by_name["/usage"].type == "builtin"
    assert by_name["/usage"].alias_of is None


def test_build_document_shape():
    table_rows = scraper.extract_table_lines(FIXTURE_MARKDOWN)
    commands = scraper.parse_rows(table_rows)
    doc = scraper.build_document(commands, "https://example.invalid/commands.md")

    assert doc["source_url"] == "https://example.invalid/commands.md"
    assert doc["command_count"] == len(commands)
    assert "scraped_at" in doc
    assert isinstance(doc["commands"], list)
    for record in doc["commands"]:
        assert set(record.keys()) == {"command", "args", "description", "type", "alias_of"}


def test_main_refuses_to_write_below_plausibility_floor(tmp_path, monkeypatch):
    """A page with a real table but too few rows must not overwrite the
    output file — this is the plausibility-floor guard, distinct from the
    missing-heading guard tested above."""
    sparse_markdown = (
        "## All commands\n\n"
        "| Command | Purpose |\n"
        "| :--- | :--- |\n"
        "| `/help` | Show help |\n"
    )
    out_path = tmp_path / "slash-commands.json"
    monkeypatch.setattr(scraper, "fetch_source", lambda url: sparse_markdown)
    monkeypatch.setattr(sys, "argv", ["scrape-slash-commands.py", "--out", str(out_path)])

    with pytest.raises(SystemExit):
        scraper.main()

    assert not out_path.exists()
