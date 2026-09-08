# TODO

## Completed

- [x] **Lovecraft theme** (2026-04-24) — abyssal-dark cosmic-horror palette
  - `Dev/cloudecode/client/css/themes/lovecraft/theme.json` (70 cssVars, 19-key xterm)
  - `Dev/cloudecode/client/css/themes/lovecraft/theme.css` (sub-1Hz cursor pulse, prefers-reduced-motion gated)
  - Verified: JSON parses, braces balanced (6/6), backend auto-discovers via `_bundled_themes_root()`

- [x] **Black Market theme** (2026-04-24) — VIP basement door, jet black + amethyst (NYX-9)
  - `Dev/cloudecode/client/css/themes/black_market/theme.json` (70 cssVars, 19-key xterm; bg `#000000`, fg `#F2EFF7`, accent `#9D4EDD`)
  - `Dev/cloudecode/client/css/themes/black_market/theme.css` (250ms ease-out focus-visible amethyst shimmer, no infinite animation)
  - No effects.js per spec
  - Verified: JSON parses, spec values match, CSS braces balanced (1/1), no @keyframes/animation, backend auto-discovers via `_bundled_themes_root()`, ThemeManifest schema accepts shape

[THEME-ALIEN] [2026-04-24]: Alien shipped

[THEME-GREEN_CRT] [2026-04-24]: Green CRT shipped — Dev/cloudecode/client/css/themes/green_crt/{theme.json,theme.css}. P1 phosphor #33FF33 on #020a04, P3 amber #FFAA00 warnings, scanlines (repeating-linear-gradient 0/2/3px), 4s 60Hz pulse @keyframes (gated on prefers-reduced-motion), phosphor bloom text-shadow. No effects.js (pure CSS). Verified: JSON parses via Pydantic ThemeManifest, CSS braces 8/8, _scan_themes_root returns 23 themes incl. green_crt.

[NEW-PROJECT-FAB] [2026-04-24]: top-right + FAB with 3-action fan-out animation
[FAB-RELOCATE] [2026-04-25]: + button moved inline with project heading, ghost-styled

[OPENCLAW-HERMES-FAB] [2026-04-27]: Added OpenClaw + Hermes FAB buttons. agent_type plumbed end-to-end. Inline SVG icons, modal title reflects agent. Default new-project preserves server fallback (no agent_type sent). Dev: launchpad.js +79/-14, styles.css +6.
[FROZEN-WS-FIX] [2026-04-27]: Dead-pane health probe in tmux_backend.start() — 250ms after new-session, checks pane_dead, captures stderr, kills session, raises RuntimeError("agent failed to launch: ..."). session_manager re-raises verbatim (was wrapped as ValueError → 400). routes.py maps RuntimeError → HTTPException(502). User now sees "failed to create session: agent failed to launch: ..." instead of frozen WS welcome. Dev: tmux_backend.py +90, session_manager.py +26, routes.py +12.
[VALIDATED] [2026-04-27]: Validator-agent PASS all 4 phases. P3 confirmed Hermes TUI streaming live ASCII art (bug fix proven — agent launches, output streams).
[COMMITTED] [2026-04-27]: Dev hash 8b3af22 on weekend-mvp-v3.1 (not pushed). Prod working tree still has dangling edits made against older baseline (pre-c6fb93a). Decision needed: revert Prod or stage for separate commit. Server runs from Dev so Prod state is cosmetic until next promotion.

## Image paste from browser → Claude Code (shipped 2026-04-28)

Plan: `/Users/Adam/.claude/plans/velvety-jingling-eagle.md`

- [x] Backend foundations (uploads helper, models, config, endpoint, config.example.json)
- [x] Sweeper module + lifespan wire-up + session_manager cleanup
- [x] Frontend (api.js uploadImage, terminal.js paste handler + iOS button, index.html, styles.css)
- [x] Pytest coverage (tests/test_upload_image.py + tests/test_upload_sweeper.py) + full suite green
- [x] README — Features bullet + Recent changes entry
- [x] Validator-agent UI verification on http://192.168.1.250:8000/

[VALIDATED] [2026-04-28]: Image paste shipped. Backend POST /sessions/upload-image + Pillow validation + per-session .cloude_uploads/ + 3-layer cleanup (destroy/startup-sweep/periodic). Frontend paste handler + iOS attach button + status pill. 16/16 new pytest pass, no regressions. Validator-agent PASS desktop+mobile. Commit 5b22cd2.

## claude-history retired, capability to be rebuilt as an MCP here (2026-09-02)

[CLAUDE-HISTORY-RETIRED] [2026-09-02]: The separate `claude-history` project is
RETIRED and deleted from the workstation. It was a standalone indexer of the
owner's Claude conversation history: a `Stop` / `SubagentStop` / `SessionEnd`
hook chain in `~/.claude/settings.json` fired
`claude-history/scripts/ingest_hook.py` detached after every turn, ingesting
`~/.claude` transcript jsonl into a SQLite corpus at
`claude-history/data.nosync/claude_history.db` (22.16 GB and still growing at
deletion), plus a four-tool stdio MCP server (`history_search`,
`history_status` and two others) built in the linked worktree
`claude-history-mcp` on branch `feat/mcp-history-search` @ `f29e31b`. Removed
this session: the three hooks, the `mcpServers.claude-history` entry in
`~/.claude.json` (it had already been failing `CONNECTION_CLOSED`), and both
project directories. Superseded by the CloudeCode message archive, whose
corpus lives on the Mac mini at `/Users/jsugamele/ClaudeArchive/`.

DATA: archived on archive-nas as item `09` of the 2026-09-02 cloud export,
`/mnt/ARCHIVE/vault/85_cloud-exports/claude/claude-archive-20260902/09-claude-history-db-20260902.sqlite.zst`
(4.60 GB zstd, decompresses to 21,988,401,152 bytes, `PRAGMA integrity_check`
verified). It is a VACUUMed point-in-time snapshot taken at 19:45 on
2026-09-02, NOT a byte-equal twin of the live file - anything ingested between
that snapshot and deletion (~174 MB of growth) is not in it and is gone.

CODE: both branches are pushed to Gogs `jsugamele/claude-history.git`
(`feat/subagent-join-and-indexes` @ `d049b41`, `feat/mcp-history-search` @
`f29e31b`), so nothing was lost by deleting the working copies.

NEXT: rebuild the search capability as an MCP server INSIDE CloudeCode, over
the CloudeCode message-archive corpus rather than over a second private
database. Note this REVERSES the earlier decision recorded at
`Infrastructure/projects/remote-claude-mini/notes/cloudecode-history-feature.md`
line 160, which scoped the MCP as "not part of CloudeCode" - that note is now
superseded. Carry forward from the retired work: FTS5 is sufficient (no vector
search), the server must be structurally read-only, no arbitrary SQL over MCP
or HTTP, and project search measured at 32ms. BLOCKER, unchanged: a plaintext
`MESH_PASS` is still unswept in the archived corpus (Infrastructure TODO item
50) - do not make the corpus searchable before that credential is rotated.
