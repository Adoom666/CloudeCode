"""Static guards for the file editor's UI contract.

Two regressions this pins:

1. THE RENAME IS LABELS ONLY. The feature is called "file editor" in every
   string a user can read, while every id, class, filename and config key
   stays ``config-editor*`` because those are referenced across the
   codebase and its tests. A future rename that "tidies up" the ids would
   break wiring silently; a future edit that reintroduces "claude config"
   in a label would undo the rename silently. Both are checked here.

2. THE BARE-BUTTON SQUARE. ``client/css/styles.css`` used to carry a bare
   ``button { width: 36px; height: 36px }`` reset reaching every <button>
   in the app, and ``@media (max-width: 480px)`` raised it to 40x40. A
   class only beat that rule for the properties it actually DECLARED, so
   any labelled button whose rule omitted width/height silently became a
   40px square on a phone with its label rendering over the border.
   Measured before the fix at 375px: the "preview" tab's box was 40x40
   with a 10px content box against 44.1px of text - a 34.1px spill. Every
   labelled button rule in the file-editor stylesheets must therefore
   declare both.

   The button-selector scoping pass retired the bare element rule; it is
   ``.btn-icon`` now, applied only to ``#configEditorBtn`` and
   ``.header-menu-toggle``. None of the labelled buttons below ever
   carried that class, so the square they had to escape is smaller in
   scope but the guard is unchanged in spirit: declare your own box or
   inherit one meant for someone else.

Run with:
    python3 -m pytest tests/test_file_editor_ui.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "client" / "index.html"
CSS_DIR = ROOT / "client" / "css"

# Rules for buttons that carry a TEXT LABEL rather than an icon, and so
# cannot survive being forced into a fixed square.
LABELLED_BUTTON_RULES = [
    (CSS_DIR / "config-editor-modal.css", ".config-editor-mode-toggle button"),
    (CSS_DIR / "config-editor-modal.css", ".config-editor-modal-back"),
    (CSS_DIR / "config-editor.css", ".config-editor-new"),
    (CSS_DIR / "config-editor.css", ".config-editor-toggle,\n.config-editor-file"),
]


def _rule_body(css: str, selector: str) -> str:
    """
    Description: return the declaration block for one exact selector.
    Inputs: css (str) - stylesheet text; selector (str) - the selector
      text as written in the file.
    Output: str - the text between the braces.
    Raises: AssertionError - the selector is not present.
    """
    marker = selector + " {"
    assert marker in css, f"selector {selector!r} is no longer in the stylesheet"
    start = css.index(marker) + len(marker)
    return css[start:css.index("}", start)]


# ---- 1. the rename is labels only --------------------------------------

def test_header_button_is_labelled_file_editor():
    html = INDEX_HTML.read_text(encoding="utf-8")
    line = next(ln for ln in html.splitlines() if 'id="configEditorBtn"' in ln)
    assert 'aria-label="File editor"' in line
    assert 'title="file editor"' in line
    assert 'data-tooltip="file editor"' in line


def test_picker_title_and_close_say_file_editor():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<span class="config-editor-title" id="config-editor-title">file editor</span>' in html
    assert 'aria-label="Close file editor"' in html


@pytest.mark.parametrize("attribute", ["aria-label", "title", "data-tooltip"])
def test_no_user_facing_attribute_still_says_claude_config(attribute):
    """The old name must not survive in anything a user reads or hears."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    offenders = [
        value for value in re.findall(rf'{attribute}="([^"]*)"', html)
        if "claude config" in value.lower()
    ]
    assert offenders == [], f"stale {attribute} values: {offenders}"


def test_internal_identifiers_were_deliberately_left_alone():
    """The rename must NOT have touched the wiring. These ids and classes
    are referenced from app.js, the stylesheets and other tests."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for identifier in (
        'id="configEditorBtn"',
        'id="config-editor-overlay"',
        'id="config-editor-title"',
        'id="config-editor-close"',
        'id="config-editor-tree"',
        'class="config-editor-picker-content"',
    ):
        assert identifier in html, f"{identifier} was renamed - it must not be"


# ---- 2. the bare-button square ----------------------------------------

def test_bare_button_reset_still_exists_so_this_guard_is_not_vacuous():
    """If the (now scoped) reset is ever removed, these tests stop
    protecting anything and should be revisited rather than left as
    decoration.

    The reset is still a hard square; it is written through
    ``--control-size`` because the header's height (and therefore the
    top-right FAB's clearance below it) is derived from the same number.
    Assert the RESET and the RESOLVED VALUES, not the literal that used
    to spell them, or this guard breaks on every refactor that keeps its
    meaning intact.

    The rule itself is ``.btn-icon`` now, not a bare ``button`` element -
    see the button-selector scoping pass. It no longer reaches the
    labelled buttons this file's other test guards, which is the fix;
    this test only confirms the square those buttons had to escape from
    still exists somewhere, on the two controls that actually want it.
    """
    css = (CSS_DIR / "styles.css").read_text(encoding="utf-8")
    assert not re.search(r"\nbutton \{", css), \
        "a bare `button {` element rule reappeared - it should be scoped, e.g. to .btn-icon"
    assert re.search(r"\n\.btn-icon \{[^}]*width: var\(--control-size\)", css), \
        "the .btn-icon width reset is gone - re-evaluate the rules below"
    # The token still resolves to the same three sizes it used to state
    # inline, so every rule below still fights the same square.
    assert re.search(r"--control-size:\s*36px;", css), "base square changed"
    assert re.search(
        r"@media \(max-width: 768px\) \{\s*\n\s*:root \{[^}]*--control-size:\s*44px;",
        css,
    ), "the 768px bare-button bump is gone - re-evaluate the rules below"
    assert re.search(
        r"@media \(max-width: 480px\) \{\s*\n\s*:root \{[^}]*--control-size:\s*40px;",
        css,
    ), "the 480px bare-button bump is gone - re-evaluate the rules below"


@pytest.mark.parametrize("css_path,selector", LABELLED_BUTTON_RULES)
def test_labelled_button_rules_declare_width_and_height(css_path: Path, selector: str):
    body = _rule_body(css_path.read_text(encoding="utf-8"), selector)
    assert re.search(r"(^|\s)width:", body), \
        f"{selector} must declare width or it collapses to the bare button square"
    assert re.search(r"(^|\s)height:", body), \
        f"{selector} must declare height or it collapses to the bare button square"


def test_command_rows_break_long_tokens():
    """A single unbreakable token in a scraped description (a path, a URL)
    used to render 491.3px inside a 234.3px line box at 375px, pushing the
    slash-commands modal body to scrollWidth 520 against clientWidth 292 -
    which is what made the list slide left and right while scrolling."""
    css = (CSS_DIR / "styles.css").read_text(encoding="utf-8")
    body = _rule_body(css, ".command-name,\n.command-description")
    assert "overflow-wrap: anywhere" in body
    assert "min-width: 0" in body


def test_command_list_containers_cannot_exceed_their_parent():
    css = (CSS_DIR / "styles.css").read_text(encoding="utf-8")
    body = _rule_body(css, ".all-commands-list,\n.command-category,\n.command-item")
    assert "min-width: 0" in body
    assert "max-width: 100%" in body


# ---- the new-file control ---------------------------------------------

def _script_pos(html: str, name: str) -> int:
    """
    Description: position of one module's <script> TAG, not of the first
      prose mention of its filename in a comment.
    Inputs: html (str); name (str) - bare filename.
    Output: int - character offset of the tag.
    """
    tag = f'<script src="/static/js/{name}"></script>'
    assert tag in html, f"{name} is not loaded"
    return html.index(tag)


def test_new_file_button_and_module_are_wired():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="config-editor-new"' in html
    # Load order matters: the panel's click handler calls into the module.
    assert _script_pos(html, "config-editor-new-file.js") < _script_pos(html, "config-editor-panel.js")


def test_command_description_module_loads_before_its_consumers():
    html = INDEX_HTML.read_text(encoding="utf-8")
    pos = _script_pos(html, "command-description.js")
    assert pos < _script_pos(html, "slash-command-filter.js")
    assert pos < _script_pos(html, "slash-commands.js")
