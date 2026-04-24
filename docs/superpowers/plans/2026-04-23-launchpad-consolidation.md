# Launchpad Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the three-section launchpad (banner + Adopt + Existing projects) into two sections (Running sessions + Existing projects), add V3-style rows with pulsing dots and inline X kill icons, and name cloude-owned tmux sessions verbatim after their project.

**Architecture:** Back-end changes are additive — a new `_sanitize_tmux_name` helper and an optional `project_name` param on `create_session`. Front-end rewrites the launchpad's rendering to merge the banner and Adopt section, with the row click-vs-X event delegation handling both return-to-terminal and destroy flows.

**Tech Stack:** Python 3.12 / FastAPI / pydantic v2, vanilla JS + xterm.js frontend, tmux on a dedicated `-L cloude` socket, pytest-asyncio for backend tests.

**Spec:** `docs/superpowers/specs/2026-04-23-launchpad-consolidation-design.md`

**Branch:** `weekend-mvp-v3.1`

---

## File structure (change inventory)

### Backend
- **Modify** `src/core/session_manager.py` — add `_sanitize_tmux_name` + `project_name` kwarg on `create_session` + adopt-on-collision branch
- **Modify** `src/core/tmux_backend.py` — add `session_name: str | None` kwarg to `__init__` (verbatim override)
- **Modify** `src/models.py` — add `project_name: Optional[str]` to `CreateSessionRequest`
- **Modify** `src/api/routes.py` — pass-through of `project_name` into `create_session`
- **Modify** `tests/test_session_backend.py` — new helper + create-session tests

### Frontend
- **Modify** `client/js/launchpad.js` — delete banner rendering, merge Adopt into new `renderRunningSessions`, add row delegation + display-name derivation, pass `project_name` on createSession
- **Modify** `client/js/api.js` — `createSession` body now includes `project_name`
- **Modify** `client/js/app.js` — remove banner DOM references from `showLaunchpad`
- **Modify** `client/index.html` — remove banner container div
- **Modify** `client/css/styles.css` — add `.running-session-*` + `@keyframes pulse-glow`; remove `.active-session-banner*`

---

## Task 1: Add `_sanitize_tmux_name` helper (TDD)

**Files:**
- Modify: `src/core/session_manager.py`
- Test: `tests/test_session_backend.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_backend.py`:

```python
# ---- sanitize_tmux_name -------------------------------------------------

def test_sanitize_tmux_name_preserves_verbatim_input():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("Cloude Code Dev") == "Cloude Code Dev"


def test_sanitize_tmux_name_replaces_dot_and_colon():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("Dotted.Name:Thing") == "Dotted_Name_Thing"


def test_sanitize_tmux_name_preserves_emoji_and_unicode():
    from src.core.session_manager import _sanitize_tmux_name
    # tmux tolerates high codepoints; we pass them through
    assert _sanitize_tmux_name("🔥 cool 🔥") == "🔥 cool 🔥"


def test_sanitize_tmux_name_collapses_whitespace_runs():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("   many   spaces   ") == "many spaces"


def test_sanitize_tmux_name_returns_empty_for_unusable_input():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("") == ""
    assert _sanitize_tmux_name("   ") == ""
    assert _sanitize_tmux_name(":::...") == "______"  # dots+colons replaced, not empty


def test_sanitize_tmux_name_only_separators_yields_underscore_run():
    # design choice: after replacement, if the result is non-empty but
    # consists only of underscores, we still pass it through — tmux
    # accepts it. empty-string fallback is only for truly empty/whitespace
    # inputs. This test pins that behavior.
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("...") == "___"


def test_sanitize_tmux_name_strips_newlines_and_tabs():
    from src.core.session_manager import _sanitize_tmux_name
    assert _sanitize_tmux_name("foo\n\tbar") == "foo bar"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode"
source venv/bin/activate
python3 -m pytest tests/test_session_backend.py::test_sanitize_tmux_name_preserves_verbatim_input -v
```
Expected: `ImportError: cannot import name '_sanitize_tmux_name'` OR `AttributeError`. Either counts as FAIL.

- [ ] **Step 3: Implement `_sanitize_tmux_name`**

Add to `src/core/session_manager.py` (near the top, after imports, module-level):

```python
import re

_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:]")
_WHITESPACE_RUN = re.compile(r"\s+")


def _sanitize_tmux_name(name: str) -> str:
    """Transform a project name into a tmux-safe session name (verbatim where possible).

    tmux forbids only '.' (pane separator) and ':' (window separator) — everything else
    (spaces, case, unicode, emoji, punctuation) is legal. This helper preserves the
    original name as closely as possible so users see the same string in the web UI
    and at the `tmux -L cloude list-sessions` CLI.

    Transformation rules, applied in order:
      1. Replace any '.' or ':' with '_'.
      2. Collapse runs of whitespace (including newlines/tabs) into a single space.
      3. Strip leading and trailing whitespace.

    Returns the sanitized name, or an empty string if nothing usable remains
    (empty input, whitespace-only input). Callers use the empty-string return as
    a signal to fall back to the legacy `ses_<hex>` naming.
    """
    if not name:
        return ""
    replaced = _TMUX_FORBIDDEN_CHARS.sub("_", name)
    collapsed = _WHITESPACE_RUN.sub(" ", replaced)
    return collapsed.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k sanitize_tmux_name
```
Expected: 7 passed.

- [ ] **Step 5: Run full suite to verify no regressions**

Run:
```bash
python3 -m pytest tests/ -q
```
Expected: previously-passing total count + 7 = all green.

- [ ] **Step 6: Commit**

```bash
git add src/core/session_manager.py tests/test_session_backend.py
git commit -m "feat: _sanitize_tmux_name helper for verbatim project naming"
```

---

## Task 2: Add `session_name` kwarg to `TmuxBackend.__init__` (TDD)

**Files:**
- Modify: `src/core/tmux_backend.py`
- Test: `tests/test_session_backend.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_backend.py`:

```python
def test_tmux_backend_accepts_verbatim_session_name_override():
    """When session_name= is passed to __init__, it's used verbatim
    instead of applying the slug+prefix transformation to session_id."""
    backend = TmuxBackend(
        session_id="ses_abc123",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_t2_")),
        session_name="cloude_Cloude Code Dev",
    )
    assert backend.tmux_session == "cloude_Cloude Code Dev"
    # session_id still recorded for metadata
    assert backend.session_id == "ses_abc123"


def test_tmux_backend_without_session_name_uses_legacy_slug():
    """Backward compat: no session_name kwarg → legacy cloude_<slug> naming."""
    backend = TmuxBackend(
        session_id="ses_abc123",
        working_dir=Path(tempfile.mkdtemp(prefix="cc_t2b_")),
    )
    # Existing code derives slug from session_id; we just assert the
    # prefix is still cloude_ and the raw session_id appears somewhere.
    assert backend.tmux_session.startswith("cloude_")
    assert "abc123" in backend.tmux_session
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k tmux_backend_accepts_verbatim
```
Expected: FAIL with `TypeError: TmuxBackend.__init__() got an unexpected keyword argument 'session_name'`.

- [ ] **Step 3: Add the kwarg to `TmuxBackend.__init__`**

In `src/core/tmux_backend.py`, locate `class TmuxBackend(SessionBackend):` and find its `__init__`. Add a new optional parameter and override `self.tmux_session` when it's provided. Example patch (preserve all existing parameters exactly as they are; only ADD `session_name`):

```python
def __init__(
    self,
    session_id: str,
    working_dir: Path,
    on_output: Optional[Callable[[bytes], Any]] = None,
    socket_name: str = DEFAULT_SOCKET_NAME,
    scrollback_lines: int = 3000,
    session_name: Optional[str] = None,
) -> None:
    super().__init__(session_id, working_dir, on_output)

    self.socket_name = socket_name
    self.scrollback_lines = scrollback_lines
    self.slug = _slugify(session_id)
    # If an explicit session_name is provided (used by create_session with
    # project_name for verbatim naming, and by for_external for adoption),
    # it OVERRIDES the legacy cloude_<slug> derivation. Otherwise default
    # to the legacy hex-based name so existing call sites are unchanged.
    if session_name is not None:
        self.tmux_session = session_name
    else:
        self.tmux_session = f"{SESSION_PREFIX}{self.slug}"
    # ... (keep remaining existing __init__ body unchanged)
```

IMPORTANT: the existing `for_external` classmethod already sets `self.tmux_session` after construction; leave that path alone. Both patterns co-exist cleanly.

- [ ] **Step 4: Run the two new tests to verify they pass**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k "tmux_backend_accepts_verbatim or tmux_backend_without_session_name"
```
Expected: 2 passed.

- [ ] **Step 5: Run full suite for regression check**

Run:
```bash
python3 -m pytest tests/ -q
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/core/tmux_backend.py tests/test_session_backend.py
git commit -m "feat: TmuxBackend.__init__ accepts session_name override"
```

---

## Task 3: Add `project_name` to `CreateSessionRequest` model (TDD)

**Files:**
- Modify: `src/models.py`
- Test: `tests/test_session_backend.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_backend.py`:

```python
def test_create_session_request_accepts_project_name():
    from src.models import CreateSessionRequest
    req = CreateSessionRequest(
        working_dir="/tmp",
        auto_start_claude=False,
        copy_templates=False,
        project_name="Cloude Code Dev",
    )
    assert req.project_name == "Cloude Code Dev"


def test_create_session_request_project_name_defaults_to_none():
    from src.models import CreateSessionRequest
    req = CreateSessionRequest(
        working_dir="/tmp",
        auto_start_claude=False,
        copy_templates=False,
    )
    assert req.project_name is None
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k "create_session_request_accepts_project_name or create_session_request_project_name_defaults"
```
Expected: FAIL with pydantic validation error OR AttributeError.

- [ ] **Step 3: Add the field**

In `src/models.py`, locate the `CreateSessionRequest` class. Add a new optional field alongside the existing fields:

```python
class CreateSessionRequest(BaseModel):
    # ... existing fields ...
    project_name: Optional[str] = None
```

Match the exact style (type hints, Field(...) calls if present) used by the surrounding fields. If the file uses `str | None` syntax instead of `Optional[str]`, match that.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k "create_session_request"
```
Expected: 2 passed.

- [ ] **Step 5: Regression check**

Run:
```bash
python3 -m pytest tests/ -q
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/models.py tests/test_session_backend.py
git commit -m "feat: CreateSessionRequest.project_name optional field"
```

---

## Task 4: Thread `project_name` through routes and SessionManager (verbatim naming, no collision path yet) (TDD)

**Files:**
- Modify: `src/core/session_manager.py`
- Modify: `src/api/routes.py`
- Test: `tests/test_session_backend.py`

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_session_backend.py`:

```python
@pytest.mark.asyncio
async def test_session_manager_create_session_verbatim_name():
    """create_session with project_name=X → tmux_session == cloude_<sanitized X>."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")

    from src.core.session_manager import SessionManager
    wd = Path(tempfile.mkdtemp(prefix="cc_t4_"))
    sm = SessionManager()
    try:
        session = await sm.create_session(
            working_dir=wd,
            auto_start_claude=False,
            copy_templates=False,
            project_name="T4 Verbatim Test",
        )
        assert sm.backend is not None
        assert sm.backend.tmux_session == "cloude_T4 Verbatim Test"
    finally:
        if sm.backend is not None:
            try:
                await sm.destroy_session()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_session_manager_create_session_without_project_name_uses_legacy():
    """create_session without project_name → legacy cloude_ses_<hex> naming."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")

    from src.core.session_manager import SessionManager
    wd = Path(tempfile.mkdtemp(prefix="cc_t4b_"))
    sm = SessionManager()
    try:
        session = await sm.create_session(
            working_dir=wd,
            auto_start_claude=False,
            copy_templates=False,
        )
        assert sm.backend is not None
        assert sm.backend.tmux_session.startswith("cloude_ses_")
    finally:
        if sm.backend is not None:
            try:
                await sm.destroy_session()
            except Exception:
                pass
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k "create_session_verbatim or create_session_without_project"
```
Expected: FAIL with `TypeError: create_session() got an unexpected keyword argument 'project_name'`.

- [ ] **Step 3: Extend `create_session` signature + threading**

In `src/core/session_manager.py`, find `async def create_session(` and add the new optional parameter alongside existing ones:

```python
async def create_session(
    self,
    working_dir: Path,
    auto_start_claude: bool = True,
    copy_templates: bool = False,
    initial_cols: Optional[int] = None,
    initial_rows: Optional[int] = None,
    project_name: Optional[str] = None,
) -> Session:
    # ... existing validation / single-active check ...

    # Derive a tmux session name override from project_name if provided.
    # An empty sanitized string means "fall back to legacy hex naming."
    tmux_session_name: Optional[str] = None
    if project_name:
        sanitized = _sanitize_tmux_name(project_name)
        if sanitized:
            tmux_session_name = f"{SESSION_PREFIX}{sanitized}"

    # ... existing session_id generation ...

    backend = build_backend(
        settings,
        session_id=session_id,
        working_dir=working_dir,
        on_output=self._handle_backend_output,
        session_name=tmux_session_name,  # NEW — None falls through to legacy
    )
    # ... rest of existing create_session body unchanged ...
```

IMPORTANT: `SESSION_PREFIX` may need to be imported from `tmux_backend`. If it's already imported, reuse; if not, add `from src.core.tmux_backend import SESSION_PREFIX`.

ALSO: the `build_backend` helper (in `src/core/session_backend.py`) may need to accept and forward `session_name`. Check its current signature. If it doesn't forward kwargs, add `session_name: Optional[str] = None` to its signature and pass it into the `TmuxBackend(...)` constructor call. For `PTYBackend`, ignore the kwarg (PTY has no concept of a named session).

- [ ] **Step 4: Update `build_backend` to forward `session_name`**

In `src/core/session_backend.py`, find `def build_backend(` and add the forwarding:

```python
def build_backend(
    settings,
    session_id: str,
    working_dir: Path,
    on_output,
    session_name: Optional[str] = None,
) -> SessionBackend:
    # ... existing tmux-vs-pty selection logic ...
    if backend_kind == "tmux":
        return TmuxBackend(
            session_id=session_id,
            working_dir=working_dir,
            on_output=on_output,
            session_name=session_name,
        )
    # PTYBackend doesn't use session_name; continue as before
    return PTYBackend(session_id=session_id, working_dir=working_dir, on_output=on_output)
```

- [ ] **Step 5: Update `POST /sessions` route**

In `src/api/routes.py`, find the create-session handler and forward `project_name`:

```python
@router.post("/sessions", response_model=Session, dependencies=[Depends(require_auth)])
async def create_session(request: Request, body: CreateSessionRequest):
    sm = request.app.state.session_manager
    return await sm.create_session(
        working_dir=Path(body.working_dir),
        auto_start_claude=body.auto_start_claude,
        copy_templates=body.copy_templates,
        initial_cols=body.cols,
        initial_rows=body.rows,
        project_name=body.project_name,
    )
```

Match the exact existing handler layout — just add the one new kwarg.

- [ ] **Step 6: Run the two new tests to verify they pass**

Run:
```bash
python3 -m pytest tests/test_session_backend.py -v -k "create_session_verbatim or create_session_without_project"
```
Expected: 2 passed.

- [ ] **Step 7: Regression check**

Run:
```bash
python3 -m pytest tests/ -q
```
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/core/session_manager.py src/core/session_backend.py src/api/routes.py tests/test_session_backend.py
git commit -m "feat: create_session honors project_name for verbatim tmux naming"
```

---

## Task 5: Adopt-on-collision in `create_session` (TDD)

**Files:**
- Modify: `src/core/session_manager.py`
- Test: `tests/test_session_backend.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_backend.py`:

```python
@pytest.mark.asyncio
async def test_create_session_adopts_when_target_name_exists():
    """If a tmux session with the derived name already exists on our
    socket, create_session should adopt it rather than fail with 'already
    running' or create a duplicate."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")

    from src.core.session_manager import SessionManager

    project_name = f"adopt_collide_{secrets.token_hex(4)}"
    target_tmux = f"cloude_{project_name}"

    # Pre-create the tmux session on our socket so the collision fires.
    subprocess.run(
        ["tmux", "-L", "cloude", "new-session", "-d", "-s", target_tmux],
        check=True,
    )
    try:
        wd = Path(tempfile.mkdtemp(prefix="cc_t5_"))
        sm = SessionManager()
        try:
            # No active session; create_session with matching project_name
            # should DETECT the existing tmux session and adopt it.
            result = await sm.create_session(
                working_dir=wd,
                auto_start_claude=False,
                copy_templates=False,
                project_name=project_name,
            )
            assert sm.backend is not None
            assert sm.backend.tmux_session == target_tmux
            # The adopted session existed BEFORE create_session — verify
            # we did not wipe+recreate it by checking we can still see it.
            assert subprocess.call(
                ["tmux", "-L", "cloude", "has-session", "-t", target_tmux],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0
        finally:
            if sm.backend is not None:
                try:
                    await sm.detach_current_session()
                except Exception:
                    pass
    finally:
        subprocess.run(
            ["tmux", "-L", "cloude", "kill-session", "-t", target_tmux],
            check=False,
        )
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m pytest tests/test_session_backend.py::test_create_session_adopts_when_target_name_exists -v
```
Expected: FAIL — likely "tmux session already exists" error or "session already running" from `create_session`.

- [ ] **Step 3: Add collision-detection + adopt branch in `create_session`**

In `src/core/session_manager.py`, in `create_session`, AFTER deriving `tmux_session_name` (from Task 4) and BEFORE constructing the backend, add:

```python
# Adopt-on-collision: if project_name resolves to a tmux session name that
# is already alive on our socket, reuse it instead of creating a duplicate.
# Matches user expectation "open project X" == "resume my X session whether
# it's already running or not."
if tmux_session_name:
    from src.core.session_backend import build_backend as _build_probe
    probe = _build_probe(
        settings,
        session_id="__collision_probe__",
        working_dir=Path.home(),
        on_output=None,
    )
    try:
        existing = probe.discover_existing() or []
    except Exception as exc:  # pragma: no cover
        logger.debug("collision_probe_failed", error=str(exc))
        existing = []
    if tmux_session_name in existing:
        logger.info(
            "session_create_redirected_to_adopt",
            project=project_name,
            existing_tmux=tmux_session_name,
        )
        # Reuse adopt_external_session. If there's already an active
        # session in this SessionManager, the caller's responsibility
        # chain has already walked through a swap modal; we use
        # confirm_detach=True here to honor that.
        result = await self.adopt_external_session(
            name=tmux_session_name,
            confirm_detach=True,
        )
        # adopt_external_session returns a dict {session, initial_scrollback_b64, fifo_start_offset}
        # but create_session must return a Session. Unwrap accordingly.
        # Look at the actual return type of adopt_external_session in your
        # code and extract the Session from it. Pseudocode:
        return result["session"] if isinstance(result, dict) else result
```

IMPORTANT: the probe backend MUST NOT be assigned as `self.backend`. It's a throwaway for `discover_existing()` only. Do not call `probe.start()`.

- [ ] **Step 4: Run the collision test to verify it passes**

Run:
```bash
python3 -m pytest tests/test_session_backend.py::test_create_session_adopts_when_target_name_exists -v
```
Expected: PASS.

- [ ] **Step 5: Regression check**

Run:
```bash
python3 -m pytest tests/ -q
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/core/session_manager.py tests/test_session_backend.py
git commit -m "feat: create_session adopts pre-existing tmux session on name collision"
```

---

## Task 6: `client/js/api.js` — include `project_name` in createSession

**Files:**
- Modify: `client/js/api.js`

- [ ] **Step 1: Locate the existing `createSession` wrapper**

Open `client/js/api.js` and find `async createSession(` (or `createSession: function(` depending on style).

- [ ] **Step 2: Add `project_name` to the body**

Current body likely reads approximately:
```js
async createSession({ working_dir, auto_start_claude = true, copy_templates = false, cols, rows }) {
    return this._fetch('/api/v1/sessions', {
        method: 'POST',
        body: JSON.stringify({ working_dir, auto_start_claude, copy_templates, cols, rows }),
    });
}
```

Update to destructure + forward `project_name`:
```js
async createSession({ working_dir, auto_start_claude = true, copy_templates = false, cols, rows, project_name = null }) {
    return this._fetch('/api/v1/sessions', {
        method: 'POST',
        body: JSON.stringify({
            working_dir,
            auto_start_claude,
            copy_templates,
            cols,
            rows,
            project_name,
        }),
    });
}
```

- [ ] **Step 3: Verify syntax**

Run:
```bash
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/api.js"
```
Expected: no output (= pass).

- [ ] **Step 4: Commit**

```bash
git add client/js/api.js
git commit -m "feat: api.createSession passes project_name to server"
```

---

## Task 7: `client/js/launchpad.js` — `selectProject` passes `project_name`

**Files:**
- Modify: `client/js/launchpad.js`

- [ ] **Step 1: Locate `selectProject`**

Open `client/js/launchpad.js` and find `async selectProject(project)`.

- [ ] **Step 2: Add `project_name` to createSession call**

The existing `createSession` invocation passes working_dir + auto_start_claude + copy_templates + dims. Add `project_name: project.name`:

```js
const session = await window.API.createSession({
    working_dir: project.path,
    auto_start_claude: true,
    copy_templates: false,
    project_name: project.name,
    ..._dims
});
```

Apply the same change to any OTHER call sites in the same file that open an existing project (e.g. `openProjectFromFolder` if it ends up calling `createSession`). Do NOT add `project_name` to the "new project from empty path" flow unless that flow has a user-entered name — check by searching for `API.createSession(` in the file. If a call site does not have a human-friendly project name available, leave it alone (backend falls back to hex naming).

- [ ] **Step 3: Verify syntax**

Run:
```bash
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add client/js/launchpad.js
git commit -m "feat: selectProject passes project.name as project_name"
```

---

## Task 8: CSS — running-session row styles + pulse keyframe

**Files:**
- Modify: `client/css/styles.css`

- [ ] **Step 1: Append the new rules**

Add to the end of `client/css/styles.css`:

```css
/* ---- Running sessions section (Task 8) -------------------------- */

.running-sessions-section {
    margin-bottom: 1.5rem;
}

.running-session-row {
    display: flex;
    flex-direction: column;
    padding: 10px 12px;
    margin-bottom: 6px;
    background: rgba(215, 119, 87, 0.08);
    border-radius: 4px;
    cursor: pointer;
    transition: background 120ms ease;
}

.running-session-row:hover {
    background: rgba(215, 119, 87, 0.14);
}

.running-session-row.owned {
    border-left: 3px solid #4ade80;
}

.running-session-row.external {
    border-left: 3px solid #fbbf24;
}

.running-session-top {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
}

.running-session-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    animation: pulse-glow 1.5s ease-in-out infinite;
}

.running-session-row.owned .running-session-dot {
    background: #4ade80;
    box-shadow: 0 0 8px #4ade80;
}

.running-session-row.external .running-session-dot {
    background: #fbbf24;
    box-shadow: 0 0 8px #fbbf24;
}

.running-session-name {
    flex: 1;
    font-weight: bold;
    color: var(--accent, #d77757);
}

.running-session-kill {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: #e88;
    border-radius: 4px;
    flex-shrink: 0;
    transition: color 120ms ease, transform 120ms ease, background 120ms ease;
}

.running-session-kill:hover {
    color: #ff6b6b;
    transform: scale(1.15);
    background: rgba(255, 107, 107, 0.12);
}

.running-session-badges {
    display: flex;
    gap: 6px;
    margin-left: 18px;
    align-items: center;
    flex-wrap: wrap;
}

.badge {
    font-size: 0.7em;
    padding: 2px 6px;
    border-radius: 3px;
    letter-spacing: 0.5px;
}

.badge-running { background: #2a3a2a; color: #4ade80; }
.badge-tmux    { background: #2a2a3a; color: #8899ff; }
.badge-external { background: #4a3a2a; color: #fbbf24; }

.running-session-age {
    color: #888;
    font-size: 0.75em;
    align-self: center;
}

@keyframes pulse-glow {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.55; }
}
```

- [ ] **Step 2: Search for obsolete banner CSS to remove**

Run:
```bash
grep -n "active-session-banner\|active-session-return\|active-session-end\|active-session-title\|active-session-meta\|active-session-name\|active-session-info\|active-session-cwd\|active-session-backend\|active-session-actions" "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/css/styles.css"
```
Delete all matched rule blocks. Keep the grep output as a checklist: each class name should return 0 matches after deletion.

- [ ] **Step 3: Re-run grep to verify deletion**

Same command as Step 2. Expected: zero output.

- [ ] **Step 4: Commit**

```bash
git add client/css/styles.css
git commit -m "style: running-session row styles + pulse-glow keyframe; drop banner CSS"
```

---

## Task 9: Frontend — render `Running sessions` list and drop the banner

**Files:**
- Modify: `client/js/launchpad.js`
- Modify: `client/js/app.js`
- Modify: `client/index.html`

- [ ] **Step 1: Remove the banner markup from `index.html`**

Open `client/index.html`. Search for `active-session-banner`:

```bash
grep -n "active-session-banner" "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/index.html"
```

Delete the entire `<div class="active-session-banner" id="active-session-banner" hidden>…</div>` block (the one shipped in commit `ba5f077`). If the block spans multiple lines, delete all of them.

After deletion, re-run the grep. Expected: zero output.

- [ ] **Step 2: Remove banner DOM hooks from `app.js`**

In `client/js/app.js`, search for `active-session-banner`:

```bash
grep -n "active-session-banner\|refreshActiveSessionBanner\|renderActiveSessionBanner" "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/app.js"
```

Remove any direct DOM manipulation of `#active-session-banner` from `showLaunchpad` or related functions. `returnToExistingTerminal(session)` — KEEP that function; it's still used by the new running-sessions-row click handler. Only remove the banner-specific show/hide code.

- [ ] **Step 3: Remove banner rendering from `launchpad.js`; add `renderRunningSessions`**

In `client/js/launchpad.js`:

**(a) Remove** the existing `renderActiveSessionBanner`, `refreshActiveSessionBanner`, `_getCurrentSessionLabel` (if only used by banner), and any banner-specific click handlers (return, end-session on banner).

**(b) Remove** the standalone "Adopt an external session" section rendering (`renderAttachable` or similar), along with its disclosure. The disclosure moves to the new running-sessions section (Task 10).

**(c) Add** `renderRunningSessions` that fetches `/api/v1/sessions/attachable`, sorts, and renders V3 rows:

```js
async loadRunningSessions() {
    try {
        const list = await window.API.listAttachableSessions();
        // list: [{name, created_by_cloude, created_at_epoch, window_count}, ...]
        this.runningSessions = Array.isArray(list) ? list : [];
    } catch (err) {
        console.warn('Launchpad: listAttachableSessions failed:', err);
        this.runningSessions = [];
    }
    // Augment with the CURRENTLY ACTIVE backend, which the server filters
    // out of /sessions/attachable to prevent self-adopt. Refetch via
    // GET /sessions (returns 404 when none active) and merge.
    try {
        const current = await window.API.getCurrentSession();
        if (current && current.tmux_session) {
            const already = this.runningSessions.some(s => s.name === current.tmux_session);
            if (!already) {
                this.runningSessions.unshift({
                    name: current.tmux_session,
                    created_by_cloude: true,
                    created_at_epoch: current.created_at_epoch || 0,
                    window_count: 1,
                    is_active: true,
                });
            } else {
                // Mark the matching row active so click = direct return
                const row = this.runningSessions.find(s => s.name === current.tmux_session);
                if (row) row.is_active = true;
            }
        }
    } catch (err) {
        // No active session — fine, nothing to augment
    }
    // Sort: active first, then owned, then external; within each, newest first
    this.runningSessions.sort((a, b) => {
        if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
        if (a.created_by_cloude !== b.created_by_cloude) {
            return a.created_by_cloude ? -1 : 1;
        }
        return (b.created_at_epoch || 0) - (a.created_at_epoch || 0);
    });
    this.renderRunningSessions();
},

renderRunningSessions() {
    const container = document.getElementById('running-sessions-list');
    if (!container) return;
    const section = document.getElementById('running-sessions-section');
    if (!this.runningSessions || this.runningSessions.length === 0) {
        if (section) section.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    if (section) section.style.display = '';
    container.innerHTML = this.runningSessions.map(s => {
        const owned = !!s.created_by_cloude;
        const displayName = this._deriveRunningSessionDisplayName(s.name);
        const ageStr = s.created_at_epoch ? this._formatRelativeAge(s.created_at_epoch) : '';
        const escapedName = this._escapeHtml(s.name);
        const escapedDisplay = this._escapeHtml(displayName);
        return `
            <div class="running-session-row ${owned ? 'owned' : 'external'}" data-name="${escapedName}" data-active="${s.is_active ? '1' : '0'}">
              <div class="running-session-top">
                <span class="running-session-dot" aria-hidden="true"></span>
                <span class="running-session-name">${escapedDisplay}</span>
                <span class="running-session-kill" role="button" aria-label="Kill session" data-kill="${escapedName}">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="6" y1="6" x2="18" y2="18"/>
                    <line x1="6" y1="18" x2="18" y2="6"/>
                  </svg>
                </span>
              </div>
              <div class="running-session-badges">
                <span class="badge badge-running">RUNNING</span>
                <span class="badge ${owned ? 'badge-tmux' : 'badge-external'}">${owned ? 'TMUX' : 'EXTERNAL'}</span>
                ${ageStr ? `<span class="running-session-age">${this._escapeHtml(ageStr)}</span>` : ''}
              </div>
            </div>
        `;
    }).join('');
},

_deriveRunningSessionDisplayName(tmuxName) {
    // cloude_<rest> → <rest>; leave non-cloude names verbatim.
    if (tmuxName && tmuxName.startsWith('cloude_')) {
        const rest = tmuxName.slice('cloude_'.length);
        // Legacy hex ses_ names: show as-is (rest starts with "ses_")
        if (rest.startsWith('ses_')) return rest;
        return rest;
    }
    return tmuxName;
},

_formatRelativeAge(epoch) {
    const now = Math.floor(Date.now() / 1000);
    const diff = Math.max(0, now - epoch);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
},
```

**(d) Add** to the launchpad HTML template (wherever `renderLaunchpadUI` or similar builds the innerHTML):

```html
<div id="running-sessions-section" class="launchpad-section running-sessions-section" style="display:none;">
    <div class="launchpad-section-title">
        ► running sessions
        <!-- disclosure moved here in Task 10 -->
    </div>
    <div id="running-sessions-list"></div>
</div>
```

Insert ABOVE the "new project" section so running sessions appear at the top.

**(e) Call `loadRunningSessions` from the launchpad init/show flow.** Search for where `loadProjects()` is called and add a sibling call:

```js
async loadLaunchpad() {
    await this.loadProjects();
    await this.loadRunningSessions();
}
```

OR, wherever the current `refreshActiveSessionBanner` was called, swap in `loadRunningSessions`.

- [ ] **Step 4: Verify syntax**

Run:
```bash
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/app.js"
```
Expected: no output for either.

- [ ] **Step 5: Commit**

```bash
git add client/js/launchpad.js client/js/app.js client/index.html
git commit -m "feat: render Running Sessions list; drop banner UI"
```

---

## Task 10: Row delegation — click = return/swap, X = destroy

**Files:**
- Modify: `client/js/launchpad.js`

- [ ] **Step 1: Add click delegation on the running-sessions list**

In `renderRunningSessions` (or the init hook that runs after it paints), attach a single click listener via event delegation. Add/update:

```js
_bindRunningSessionClicks() {
    const container = document.getElementById('running-sessions-list');
    if (!container || container.__boundRunningClicks) return;
    container.addEventListener('click', async (e) => {
        const killEl = e.target.closest('.running-session-kill');
        const rowEl = e.target.closest('.running-session-row');
        if (!rowEl) return;

        // X icon path — explicit destroy
        if (killEl) {
            e.stopPropagation();
            const name = killEl.dataset.kill;
            await this._handleKillRunningSession(name);
            return;
        }

        // Row click — return or swap
        const name = rowEl.dataset.name;
        const isActive = rowEl.dataset.active === '1';
        if (isActive) {
            // Already the active backend → jump straight to terminal
            const current = await window.API.getCurrentSession();
            if (current) {
                window.App.returnToExistingTerminal(current);
            }
            return;
        }
        // Different session → existing swap flow (cloude-owned OR external)
        await this._handleAttachRunningSession(name);
    });
    container.__boundRunningClicks = true;
},

async _handleKillRunningSession(tmuxName) {
    const display = this._deriveRunningSessionDisplayName(tmuxName);
    const confirmed = await this.showConfirmModal(
        'end session?',
        `destroy "${this._escapeHtml(display)}"? this kills the tmux session permanently.`,
        'this is the only destructive action. session data in the pane will be lost.',
        'destroy',
        'cancel'
    );
    if (!confirmed) return;
    try {
        // If this is the currently-active session, use the normal destroy
        // endpoint. Otherwise, kill via tmux-level adopt+destroy shim.
        const current = await window.API.getCurrentSession();
        if (current && current.tmux_session === tmuxName) {
            await window.API.destroySession();
        } else {
            // Adopt-then-destroy: swap onto the target, then destroy it.
            await window.API.adoptSession(tmuxName, true);
            await window.API.destroySession();
        }
    } catch (err) {
        this.showError(`destroy failed: ${err.message || err}`);
    }
    await this.loadRunningSessions();
},

async _handleAttachRunningSession(tmuxName) {
    const display = this._deriveRunningSessionDisplayName(tmuxName);
    const current = await window.API.getCurrentSession().catch(() => null);
    if (current && current.tmux_session && current.tmux_session !== tmuxName) {
        const currentDisplay = this._deriveRunningSessionDisplayName(current.tmux_session);
        const ok = await this.showConfirmModal(
            'switch session?',
            `attaching to "${this._escapeHtml(display)}" will detach from your current session "${this._escapeHtml(currentDisplay)}".`,
            'the tmux session will keep running — you can rejoin it later from the running-sessions list. cancel to stay on the launchpad.',
            `attach to ${display}`,
            'cancel'
        );
        if (!ok) return;
    }
    try {
        const result = await window.API.adoptSession(tmuxName, true);
        // Same dispatch as the existing adopt flow
        window.dispatchEvent(new CustomEvent('session-created', {
            detail: {
                session: result.session,
                initialScrollbackB64: result.initial_scrollback_b64,
                fifoStartOffset: result.fifo_start_offset,
                adopted: true,
            }
        }));
    } catch (err) {
        this.showError(`attach failed: ${err.message || err}`);
    }
},
```

- [ ] **Step 2: Call `_bindRunningSessionClicks()` once after init**

Either call it from the end of `renderRunningSessions` OR from `renderLaunchpadUI`. The `__boundRunningClicks` flag prevents double-binding, so call-from-render is safe.

- [ ] **Step 3: Verify syntax**

Run:
```bash
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
```

- [ ] **Step 4: Commit**

```bash
git add client/js/launchpad.js
git commit -m "feat: row click + X icon delegation for running sessions"
```

---

## Task 11: Move `?` disclosure to the running-sessions heading

**Files:**
- Modify: `client/js/launchpad.js`

- [ ] **Step 1: Locate the existing disclosure**

Search for the existing `<details class="adopt-disclosure">` in `client/js/launchpad.js`:

```bash
grep -n "adopt-disclosure" "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
```

- [ ] **Step 2: Relocate + rewrite the opening sentence**

Move the entire `<details>` block into the `running-sessions-section` title (Task 9 Step 3d). Update the first `<p>` to reflect the new section name:

```html
<details class="adopt-disclosure">
    <summary>?</summary>
    <div class="adopt-disclosure-body">
        <p>Sessions shown here run on the <code>cloude</code> tmux socket. Start one externally with <code>tmux -L cloude new -s &lt;name&gt;</code> — it'll appear here.</p>
        <p>To launch claude in one line:</p>
        <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "claude --dangerously-skip-permissions; exec $SHELL"</code></pre>
        <p>The <code>exec $SHELL</code> trick keeps the pane alive with a shell prompt after claude exits.</p>
        <p>If you have a custom launcher alias (e.g. <code>cld</code>) defined in your <code>~/.zshrc</code> or <code>~/.bashrc</code>, wrap the inner command in an interactive shell:</p>
        <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "$SHELL -ic 'cld; exec $SHELL'"</code></pre>
        <p>Full setup in the <a href="https://github.com/Adoom666/CloudeCode#launching-claude-with-a-custom-alias" target="_blank" rel="noopener">README</a>.</p>
    </div>
</details>
```

Keep the existing `.adopt-disclosure`/`.adopt-disclosure-body`/`.adopt-disclosure-code` CSS rules (they still apply).

- [ ] **Step 3: Verify the old Adopt section references are fully gone**

```bash
grep -n "adopt an external session\|adopt external session" "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
```
Expected: at most a comment / internal function name reference (`openProjectFromFolder` etc.) — no user-visible "adopt an external session" heading text.

- [ ] **Step 4: Verify syntax**

Run:
```bash
node --check "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/client/js/launchpad.js"
```

- [ ] **Step 5: Commit**

```bash
git add client/js/launchpad.js
git commit -m "feat: move adopt-disclosure to running-sessions heading with updated copy"
```

---

## Task 12: Full-suite regression check + live smoke

**Files:** none (verification-only task)

- [ ] **Step 1: Run full pytest suite**

```bash
cd "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode"
source venv/bin/activate
python3 -m pytest tests/ -q
```
Expected: all green. Baseline before this plan is 163 — expect 163 + 11 new ≈ 174 passing.

- [ ] **Step 2: Restart the live server**

```bash
lsof -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill; sleep 2
cd "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode"
nohup bash -c "source venv/bin/activate && exec python3 -m src.main" > /tmp/cloude-server.log 2>&1 &
disown
sleep 5
curl -sS http://127.0.0.1:8000/health && echo ""
```
Expected: `{"status":"healthy",...}`.

- [ ] **Step 3: Live CLI smoke — verbatim naming**

```bash
# Mint token
TOKEN=$(python3 -c "
import pyotp, pathlib, requests
env = pathlib.Path('/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/.env').read_text()
for l in env.splitlines():
    if l.startswith('TOTP_SECRET='):
        secret = l.split('=',1)[1].strip().strip(chr(34))
        break
code = pyotp.TOTP(secret).now()
r = requests.post('http://127.0.0.1:8000/api/v1/auth/verify', json={'code': code})
print(r.json()['access_token'])
")

# Create session with project_name
curl -sS -X POST http://127.0.0.1:8000/api/v1/sessions \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"working_dir":"/tmp","auto_start_claude":false,"copy_templates":false,"project_name":"Smoke Plan Test"}' \
    | python3 -m json.tool | head -10

# Verify tmux session name
tmux -L cloude list-sessions | grep "Smoke Plan Test"
```
Expected: `cloude_Smoke Plan Test: 1 windows ...` in tmux output.

- [ ] **Step 4: Live smoke — adopt-on-collision**

```bash
# Call create_session AGAIN with the same name; should adopt, not 409
curl -sS -X POST http://127.0.0.1:8000/api/v1/sessions \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"working_dir":"/tmp","auto_start_claude":false,"copy_templates":false,"project_name":"Smoke Plan Test"}' \
    -w "\nHTTP %{http_code}\n"
```
Expected: HTTP 200 (not 409). The response's session id should reflect the adopted session.

- [ ] **Step 5: Cleanup**

```bash
tmux -L cloude kill-session -t "cloude_Smoke Plan Test" 2>/dev/null || true
```

- [ ] **Step 6: validator-agent browser smoke**

Dispatch the validator-agent with this prompt (if available in the environment):

> Launchpad UI smoke. URL: http://127.0.0.1:8000. Login via TOTP (mint via `python3 -c "import pyotp,pathlib; [print(pyotp.TOTP(l.split('=',1)[1].strip().strip(chr(34))).now()) for l in pathlib.Path('/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode/.env').read_text().splitlines() if l.startswith('TOTP_SECRET=')]"`). After login, verify:
> - "running sessions" section appears at top of launchpad
> - Each row shows a pulsing dot, session name (NOT hex id), RUNNING + TMUX/EXTERNAL badges, age, and an X icon on the right
> - Clicking the row (not X) navigates into the terminal
> - Clicking X opens a destroy confirmation modal; cancel closes it, confirm ends the session
> - Opening "Cloude Code Dev" from existing projects creates a tmux session literally named `cloude_Cloude Code Dev` (verify via parallel `tmux -L cloude list-sessions`)
> - Old "session running:" banner is gone
> - Old "Adopt an external session" standalone section is gone

Expected: all behaviors as described. Capture screenshots for any failures.

- [ ] **Step 7: Commit smoke artifacts if any + push**

```bash
git push origin weekend-mvp-v3.1
```

---

## Task 13: Final self-check + spec cross-reference

**Files:** none

- [ ] **Step 1: Re-read the spec**

Open `docs/superpowers/specs/2026-04-23-launchpad-consolidation-design.md`. Walk through each section:
- "Two launchpad sections" → implemented in Tasks 9, 10, 11
- "V3 row anatomy" → Tasks 8, 9
- "Session naming verbatim" → Tasks 1, 2, 3, 4
- "Adopt-on-collision" → Task 5
- "Interaction model" (row=return/swap, X=destroy) → Task 10
- "Disclosure placement" → Task 11
- "Legacy session names preserved" → Task 4 Step 3 fallback path
- Testing items 1-8 + integration 9 → Tasks 1, 4, 5 + live smoke

If any spec requirement is not covered by a task, add a task or flag it to the user before closing.

- [ ] **Step 2: Confirm commits are clean**

```bash
cd "/Users/Adam/Dropbox/My Projects/Cloude Code Repos/Dev/cloudecode"
git log --oneline weekend-mvp-v3.1 | head -15
```

Expected: each Task's commit appears as a distinct line. No uncommitted files.

- [ ] **Step 3: Push + notify**

```bash
git push origin weekend-mvp-v3.1
```

Tell the user: "Launchpad consolidation + verbatim naming shipped in N commits on `weekend-mvp-v3.1`. Hard-reload the browser."

---

## Risks & rollback

- **Adopt-on-collision probe spawns a throwaway backend.** This is harmless — `discover_existing()` is pure tmux CLI read. If it ever fails, the surrounding `try/except` logs and falls back to normal creation.
- **Legacy `cloude_ses_<hex>` sessions remain in the list** until users end them. The display-name derivation shows them as-is. Acceptable.
- **Running-session row click when user already has a different session active** triggers the swap modal. If the user cancels, nothing happens. No state drift.
- **Rollback:** `git revert` the individual commits in reverse order. Tasks are independent after Task 4 (which establishes the new API surface).
