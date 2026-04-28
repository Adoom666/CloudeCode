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
