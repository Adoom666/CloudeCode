# Launchpad consolidation + meaningful session naming

**Status:** Design approved, ready for implementation plan
**Project:** Cloude Code (this repo)
**Branch target:** `weekend-mvp-v3.1`

## Problem

The launchpad currently has three distinct sections that overlap conceptually:

1. **Active session banner** (top): shows the currently-attached session with Return + End buttons
2. **Adopt an external session** (middle): lists tmux sessions on our socket that we didn't create
3. **Existing projects** (bottom): lists projects from `config.json`

Problems:
- Redundancy — a cloude-owned session appears in the banner but its matching project ALSO appears in "existing projects"
- User can't see at a glance which projects have live sessions
- Session names shown to the user are internal hex IDs (`cloude_ses_7c2419e4`) instead of meaningful project names
- Banner vs row interaction is inconsistent (banner has explicit "Return" button; Adopt rows have "attach" button; existing-project rows are click-the-row)
- Kill action only available via terminal-screen destroy button; not possible from launchpad

## Goals

- Single mental model: sessions live where projects live
- One click = return-to-terminal for any running session
- Explicit kill icon inline on each running row
- Session names reflect the project name, not internal IDs
- Never kill a session except via an explicit kill action (invariant, preserved)

## Non-goals

- Multi-session support (still single-active-backend on the server)
- Cross-machine session sharing
- Renaming of existing `cloude_ses_<hex>` sessions (legacy sessions stay alive with their old names)

## User-visible design

### Two launchpad sections (replaces three)

```
► running sessions                                       (hidden when empty)
  ┌─────────────────────────────────────────────────┐
  │ ● Cloude Code Dev                         ✕    │   green border (owned)
  │     [RUNNING] [TMUX]  started 12m ago           │
  └─────────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────────┐
  │ ● mywork                                  ✕    │   yellow border (external)
  │     [RUNNING] [EXTERNAL]  5m ago                │
  └─────────────────────────────────────────────────┘

► new project
  [ create new project ]
  [ 📁 open project from folder ]

► existing projects
  » AGI-SaaS Disruptor
    ~/projects/agi-saas
    Self-healing kubernetes-native AGI mesh...

  » NeuralMesh3000
  ...
```

The `session running: <name>` banner is removed entirely.

### Row anatomy (running session)

- **Pulse dot** — 8×8, colored (green = owned, yellow = external), `box-shadow` glow, `@keyframes pulse-glow` at 1.5s interval
- **Left border** — 3px solid matching the dot color
- **Row background** — subtle accent tint (`rgba(215, 119, 87, 0.08)`)
- **Name** — bold, primary text color
- **Badges** (under name, indented to align with name):
  - `RUNNING` — green chip on every running row
  - `TMUX` — blue chip when `created_by_cloude=true` (cloude-managed)
  - `EXTERNAL` — yellow chip when `created_by_cloude=false`
- **Age** — small gray text next to badges (`started 12m ago` / `5m ago`)
- **Kill icon** — SVG X, 18×18, `stroke="#e88"`, hover → brighter + subtle scale. Hitbox is slightly larger than the icon (min 32×32 for touch).

### Interaction model

- **Click anywhere on a running-session row (except the X)** → return to terminal (same path as old banner Return button). If the user clicks a row whose name matches the currently-active backend, this is a straight re-attach. If it matches a DIFFERENT running session, the existing swap modal fires: "switch session? attaching to <X> will detach from your current session <Y>."
- **Click the X icon** → existing destroy confirmation modal. This is the ONE kill path on the launchpad.
- **Click an "existing projects" row** (unchanged):
  - If no tmux session exists for the project's slug → create one
  - If a tmux session with the derived slug exists → adopt it (one-click return-to-work — matches user's mental model)
  - If a DIFFERENT session is currently active → swap modal as today

### Session naming

When creating a new session for a project, the tmux session name is the project name **verbatim**, with a single transformation step to satisfy tmux's only hard constraints:

```
project.name = "Cloude Code Dev"
  ↓ sanitize_for_tmux() — ONLY strips `.` and `:` (tmux pane/window separators)
sanitized = "Cloude Code Dev"
  ↓
tmux_session = "cloude_Cloude Code Dev"
```

Sanitization rules (minimal — preserve what the user sees):
- Replace `.` → `_` (tmux pane separator)
- Replace `:` → `_` (tmux window separator)
- Collapse any run of multiple consecutive whitespace chars into a single space
- Strip leading and trailing whitespace
- If the result is empty after stripping → fall back to `cloude_ses_<hex>` (the legacy naming)

Case, spaces, hyphens, emoji, apostrophes, quotes, parens — all preserved. tmux tolerates them all.

Collision behavior: **before creating a tmux session with name `cloude_<sanitized>`, check if it already exists on our socket. If yes, adopt it instead** (via the existing `adopt_external_session(name, confirm_detach=True)` path). This is the core user expectation: "open Cloude Code Dev" = "resume my Cloude Code Dev session" whether it's alive or not.

Legacy `cloude_ses_<hex>` sessions are left alone. They continue to appear in the running-sessions list with their hex names until they're ended.

## Technical design

### Backend changes

**`src/core/session_manager.py`**

Add helper:
```python
def _sanitize_tmux_name(name: str) -> str:
    """Make a project name safe as a tmux session name.

    tmux forbids '.' (pane separator) and ':' (window separator). Everything
    else — spaces, case, punctuation, emoji — is fine. Replace forbidden
    chars with '_', collapse whitespace runs, strip edges. Returns empty
    string if the result is unusable, in which case the caller falls back
    to legacy hex-based naming.
    """
```

Extend `create_session()` signature with optional `project_name: str | None = None`. Flow:
1. If `project_name` provided → `sanitized = _sanitize_tmux_name(project_name)` → `target_name = f"cloude_{sanitized}"`
2. Else → existing hex-based `ses_<hex>` flow (legacy path preserved)
3. If `sanitized` non-empty AND `target_name` appears in `tmux -L cloude list-sessions`:
   - Log `session_create_redirected_to_adopt project=<name> existing_tmux=<target_name>`
   - Call `self.adopt_external_session(target_name, confirm_detach=<same as incoming>)` and return its result
4. Else construct `TmuxBackend(session_name=target_name, ...)` and proceed with the existing create path
5. Metadata persists new session in `owned_tmux_sessions` as always

**`src/core/tmux_backend.py`**

Add `session_name: str | None = None` kwarg to `__init__`. If provided, use it verbatim instead of `f"{SESSION_PREFIX}{slug_from_id}"`. The existing `for_external` classmethod continues to work for adopt-external calls; the new kwarg is for cloude-owned sessions with meaningful names.

**`src/api/routes.py`**

`CreateSessionRequest` gains `project_name: str | None = None`. `POST /sessions` handler passes it through.

The existing `GET /api/v1/sessions/attachable` continues to return ALL running sessions (both owned and external). Rename semantics in docstring: "running sessions on this socket, cross-referenced with owned_tmux_sessions." No endpoint URL change (stability for the client).

**Retain**: `POST /sessions/detach`, `POST /sessions/adopt`, `DELETE /sessions`. No new endpoints.

**`src/models.py`**

- `CreateSessionRequest`: add `project_name: str | None = None`
- `AttachableSession`: unchanged (client-side derives display name from owned_tmux_sessions)

### Frontend changes

**`client/js/launchpad.js`**

Merge `renderActiveSessionBanner()` + `renderAttachable()` into a single `renderRunningSessions(running: list)` function that:
1. Fetches `/api/v1/sessions/attachable` (returns all running sessions)
2. Sort: owned-first, within each group newest-first by `created_at_epoch`
3. Render V3 row per session with appropriate border/badge colors
4. Hide the entire `► running sessions` section if list is empty
5. Each row:
   - Whole row is clickable (delegated handler checks target — if target is the X or its child, do kill; else do return/swap)
   - Return handler: if `session.name === activeBackend.tmux_session` → `returnToExistingTerminal`. Else → swap modal (existing copy)
   - X handler: existing destroy confirmation modal (same as banner's "end session")
6. Display name derivation (client-side):
   - If name starts with `cloude_` and the rest matches a known project's slug, show the project's original name
   - Else show the raw tmux session name (handles `cloude_ses_<hex>` legacy and `mywork`-style externals)

Delete:
- `renderActiveSessionBanner()` and all banner DOM
- `refreshActiveSessionBanner()`
- The section anchor `<div id="active-session-banner">`

Keep:
- `renderExistingProjects()` unchanged except for the "project has a running session" inline hint (OPTIONAL — skip if it complicates). Clicking an existing-projects row with `project_name` now reaches the server's new adopt-on-slug-collision path automatically.

`selectProject(project)` — pass `project_name: project.name` in the `createSession` request body.

**`client/js/api.js`**

`createSession({ working_dir, auto_start_claude, copy_templates, cols, rows, project_name })` — add `project_name` to the request body.

Existing `listAttachableSessions`, `adoptSession`, `detachSession`, `destroySession` are unchanged.

Remove the `getCurrentSession()` wrapper unless another caller still needs it (check `app.js`, `terminal.js`).

**`client/js/app.js`**

`returnToExistingTerminal(session)` continues to exist — running-session row click calls it when `session.name === activeBackend`.

Remove banner-specific DOM hooks (`showLaunchpad` no longer touches a banner element).

**`client/css/styles.css`**

Add:
```css
.running-sessions { /* section wrapper */ }
.running-session-row { display:flex; flex-direction:column; padding:10px 12px; margin-bottom:6px; background:rgba(215,119,87,0.08); border-radius:4px; cursor:pointer; }
.running-session-row.owned { border-left:3px solid #4ade80; }
.running-session-row.external { border-left:3px solid #fbbf24; }
.running-session-top { display:flex; align-items:center; gap:8px; margin-bottom:4px; }
.running-session-dot { width:8px; height:8px; border-radius:50%; display:inline-block; animation: pulse-glow 1.5s ease-in-out infinite; }
.running-session-row.owned .running-session-dot { background:#4ade80; box-shadow:0 0 8px #4ade80; }
.running-session-row.external .running-session-dot { background:#fbbf24; box-shadow:0 0 8px #fbbf24; }
.running-session-name { flex:1; font-weight:bold; }
.running-session-kill { /* SVG X wrapper */ width:32px; height:32px; display:flex; align-items:center; justify-content:center; cursor:pointer; color:#e88; }
.running-session-kill:hover { color:#ff6b6b; transform: scale(1.15); }
.running-session-badges { display:flex; gap:6px; margin-left:18px; align-items:center; }
.badge-running { background:#2a3a2a; color:#4ade80; }
.badge-tmux { background:#2a2a3a; color:#8899ff; }
.badge-external { background:#4a3a2a; color:#fbbf24; }
.badge-running, .badge-tmux, .badge-external { font-size:.7em; padding:2px 6px; border-radius:3px; }
.running-session-age { color:#888; font-size:.75em; align-self:center; }

@keyframes pulse-glow {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.55; }
}
```

Remove:
- All `.active-session-banner*` rules
- Unused `.adopt-disclosure*` rules if no longer referenced (keep the Adopt help `<details>` disclosure styles only if we keep the disclosure — see "Disclosure placement" below)

### Disclosure placement

The current `?` disclosure next to "Adopt an external session" has the `tmux -L cloude new -s ...` and `$SHELL -ic ...` help copy. Since the section is being absorbed into "running sessions," move the `<details>` next to the `► running sessions` heading and tweak the first sentence: "Sessions shown here run on the `cloude` tmux socket. Start one externally with `tmux -L cloude new -s <name>` — it'll appear here." Rest of the content (one-liner, README link) unchanged.

### Migration / backward compatibility

- No server-side migration. Existing `owned_tmux_sessions` entries with `cloude_ses_<hex>` names continue to work.
- Existing `session_metadata.json` — no schema change. `create_session` request adds optional `project_name`; missing it falls to legacy hex naming.
- Client's legacy `session.id` rendering path stays intact; the new section just uses the tmux session name from `/sessions/attachable` as the source of truth for display.

### Testing

**Unit (`tests/test_session_backend.py`):**
1. `_sanitize_tmux_name("Cloude Code Dev")` == `"Cloude Code Dev"` (verbatim preservation — case + spaces intact)
2. `_sanitize_tmux_name("Dotted.Name:Thing")` == `"Dotted_Name_Thing"` (tmux separators replaced)
3. `_sanitize_tmux_name("🔥 cool 🔥")` == `"🔥 cool 🔥"` (emoji preserved — tmux tolerates them)
4. `_sanitize_tmux_name("   many   spaces   ")` == `"many spaces"` (run-collapse + strip)
5. `_sanitize_tmux_name("")` and `_sanitize_tmux_name("   ")` and `_sanitize_tmux_name(":::...")` all return empty string
6. `create_session(project_name=X)` with no pre-existing tmux session → creates `cloude_<sanitized>` and returns; `backend.tmux_session` matches
7. `create_session(project_name=X)` with pre-existing tmux session `cloude_<sanitized>` → calls `adopt_external_session` internally, does NOT create a duplicate
8. `create_session(project_name=None)` → falls back to legacy `cloude_ses_<hex>` flow

**Integration:**
9. End-to-end open-project with renamed flow: `POST /sessions {working_dir, project_name: "Cloude Code Dev"}` → `tmux -L cloude has-session -t "cloude_Cloude Code Dev"` returns 0. Second call to same endpoint (with the session still alive) → adopts rather than errors.

**Client / manual smoke (validator-agent after code lands):**
- Launchpad with 0 running sessions → "running sessions" section hidden
- Launchpad with 1 owned running → shown with green pulse + TMUX badge
- Launchpad with 1 external running → yellow pulse + EXTERNAL badge
- Click row → return to terminal
- Click X → modal → confirm → row disappears, tmux session killed
- Open "Cloude Code Dev" from existing projects → tmux has `cloude_Cloude Code Dev` (not `cloude_ses_*`)
- Open same project second time → adopts existing instead of 409

## Files modified

### Backend
- `src/core/session_manager.py` — `_sanitize_tmux_name` helper, `create_session(project_name=…)` param, adopt-on-collision branch
- `src/core/tmux_backend.py` — `session_name` kwarg on `__init__`
- `src/api/routes.py` — pass-through of `project_name`
- `src/models.py` — `CreateSessionRequest.project_name`
- `tests/test_session_backend.py` — 7+ new tests

### Frontend
- `client/js/launchpad.js` — consolidation: delete banner + adopt-section rendering, add `renderRunningSessions`, row click delegation, display-name derivation
- `client/js/api.js` — add `project_name` to `createSession`; remove `getCurrentSession` if orphaned
- `client/js/app.js` — remove banner-specific DOM hooks from `showLaunchpad`
- `client/css/styles.css` — new `.running-session-*` rules + `@keyframes pulse-glow`; delete `.active-session-banner*`
- `client/index.html` — remove the banner container div if present

## Risks

1. **Name collision across different projects** — "Cloude Code Dev" and "Cloude Code Dev " (trailing space) both sanitize to `Cloude Code Dev`. Whitespace-run-collapse + strip handles most accidental cases. "Cloude" and "Cloude" (different unicode homoglyph) would still collide — acceptable edge case. Adopt-on-collision means the second open reuses the first's session rather than erroring.
2. **CSS shuffle breaks responsive layout** — pulsing dots + flex rows may look different on narrow mobile widths. Validator-agent smoke must verify on phone viewport.
3. **Legacy hex session names alongside meaningful names** — `cloude_ses_7c2419e4` next to `cloude_Cloude Code Dev` in the same list. Acceptable — legacy ones age out as users end them.
4. **Session name with special chars in tmux CLI quoting** — a name like `cloude_My Project (old)` needs proper quoting when the user runs `tmux -L cloude attach -t "cloude_My Project (old)"` manually. The web UI quotes correctly internally; user docs in the disclosure note this.

## Out of scope

- Project-name rename UI (still via editing `config.json` manually)
- Multi-session concurrency
- A "Started at <ISO timestamp>" tooltip on the age string (nice-to-have, skip)
- Any color theme change beyond the accent-green / accent-yellow for owned/external
