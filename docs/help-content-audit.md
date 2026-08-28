# Launchpad help content audit (fix/help-copy)

Source: user report "help needs to be cleaned up, reads very wierd." Testing
build was `d033caa` on the Mac mini at http://10.0.1.150:8000.

## Where the help screen actually is

There is no dedicated "Help" menu item, tab, or modal anywhere in the app.
The header's "More actions" menu has exactly two entries: Logout and
Settings. The only substantive explanatory prose in the whole client is the
`?` disclosure at the top of the launchpad (`client/js/launchpad.js`,
`.adopt-disclosure`), originally scoped to "how to adopt an external tmux
session." That disclosure is what this fix rewrites and widens; nothing else
in the codebase matched "help screen."

## What was wrong with the old copy

- Two literal em dashes in the rendered text ("you don't have to launch
  through cloude, any tmux session..." and "it shows up in this list
  tagged EXTERNAL, click it to adopt..."), against the project's own
  no-em-dash rule.
- The README link (`#launching-claude-with-a-custom-alias`) pointed at a
  GitHub anchor that has never existed in `README.md`. The closest real
  section for the `cld` function it was trying to point at is `### Before
  you start - three things that will bite you`; the closest section for
  the *other* concept the old text never mentioned (settings-configured
  wrappers) is `### Launch wrappers`.
- It called a required shell **function** (`cld`) a "custom launcher
  alias." `README.md`'s own install instructions are explicit: "Make it a
  function, not an alias. Aliases are resolved at parse time and will not
  be available to the command string the server runs." Calling it an
  alias in the one place a confused user goes for help teaches the wrong
  fix.
- It covered exactly one topic (adopting a tmux session you started by
  hand) and nothing else, even though it is the only help surface in the
  app. It never answered the two questions the user raised separately
  about the settings screen ("what's the difference between agent and
  launch wrappers, aren't they the same") or said anything about slash
  commands.

## What the settings screen actually does, verified against code

`client/js/settings-tabs.js` renders four tabs: wrappers, terminal,
notifications, general. The "agents" tab is gone; its removal is documented
in that file's own comments and confirmed live in the settings modal.

`client/js/agent-wrappers-view.js` (the "wrappers" tab's content) titles its
one section `<h3>launch wrappers</h3>`. So "wrapper" (tab label, and the
`AgentWrapper` class in `src/core/agent_wrappers.py`) and "launch wrapper"
(section heading, and the phrase used throughout `src/core/agent_wrappers.py`,
`src/api/routes.py`, `README.md`) are **the same object**, named two
different ways on the same screen. There is no second, distinct "agent
wrapper" concept anywhere in the code. `src/core/agent_families.py` is the
registry of the five families a wrapper can target (claude, codex, hermes,
openclaw, shell); a family with no wrappers falls back to its static
`agents.<family>_command`, exposed in the UI as a collapsed
"advanced: legacy `<family>` command" row (`agent-wrappers-view.js`
`renderLegacyCommand`).

## EXTERNAL tag: verified as derived, not stored

`src/core/session_manager.py::list_attachable_sessions` builds the
EXTERNAL/owned split on every call by probing live tmux state
(`probe.list_attachable_sessions(owned_names=set(self.owned_tmux_sessions))`)
rather than reading a persisted per-session field. The rewritten help text
says this plainly instead of implying the tag is a fixed, permanent
property of the session.

## Slash command favorites, verified against code

`client/js/slash-favorites.js`: starring writes to the same
`common_slash_commands` config key that used to be hand-edited JSON. Three
states, not two - no stars yet shows built-in defaults (and says so); star
then unstar everything shows an empty row, not the defaults again. The
rewritten help text describes this without overpromising persistence
details the user does not need.

## What changed in the rewrite

- Fixed both em dashes.
- Fixed the dead README link; split it into two correct anchors (one per
  topic it actually needed to point at).
- Fixed "alias" to "function" for `cld`.
- Added two new subsections inside the same disclosure: wrappers/launch
  wrappers (answers the user's exact settings-screen question), and slash
  commands (favorites).
- Left the markup, CSS classes, SVG marker, and disclosure/summary
  structure untouched - `tests/test_home_screen_polish.node.mjs` asserts on
  all of that and still passes unmodified.
