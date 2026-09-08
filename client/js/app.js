// Main app bootstrap - extracted from index.html for CSP compliance (script-src 'self').

// SESSION-IDENTITY-V2 - header identity asset. Single source of truth so the
// path is editable from one spot. Both are raster PNGs derived directly
// from macOS/assets/AppIcon-1024.png (see scripts/generate-web-icons.sh's
// sibling header sizes, header-icon.png/header-icon@2x.png) rather than a
// hand-drawn vector approximation - the real app icon is rendered 3D
// volumetric artwork (soft shaded cloud, pixel-art face) that primitives
// cannot reproduce. HEADER_BRAND_ICON_URL is the 1x fallback (64px);
// HEADER_BRAND_ICON_URL_2X is the 2x/retina source (128px), wired in via
// srcset so both densities render sharp inside the 1.2em box.
// client/index.html's static #header-icon markup embeds this SAME pair as a
// literal <img src/srcset> so the mark is correct on first paint, before
// this script has even run - tests/test_icon_assets.py asserts the two stay
// in sync. If you change these constants, update that markup too.
const HEADER_BRAND_ICON_URL = '/static/assets/icons/header-icon.png';
const HEADER_BRAND_ICON_URL_2X = '/static/assets/icons/header-icon@2x.png';
// fix/icon-consistency - the real mark now renders on EVERY screen (see
// setHeaderIdentity below). This emoji is no longer a per-screen choice; it
// is only the last-resort fallback if the SVG fails to load (offline cache
// miss, asset renamed out from under the constant above, etc). Kept
// visually distinct from silent failure: _onHeaderIconLoadError logs to the
// console and stamps data-icon-fallback="emoji" on #header-icon so the
// fallback is detectable rather than looking like an intentional design.
const HEADER_BRAND_EMOJI = '☁️'; // ☁️ cloud emoji

/**
 * fix/icon-consistency - if the brand SVG 404s or otherwise fails to load,
 * fall back to the emoji rather than an empty box, but make the fallback
 * detectable (console.error + a data attribute) instead of silently
 * looking like a deliberate choice. This exact silence - an emoji fallback
 * that was indistinguishable from a real design decision - is how the
 * launchpad/auth screens went months showing the wrong mark unnoticed.
 *
 * @param {Event} _event - the <img> error event (unused, kept for the
 *   addEventListener signature).
 */
function _onHeaderIconLoadError(_event) {
    console.error('Header brand icon failed to load: ' + HEADER_BRAND_ICON_URL);
    var iconEl = document.getElementById('header-icon');
    if (iconEl) {
        iconEl.innerHTML = '';
        iconEl.textContent = HEADER_BRAND_EMOJI;
        iconEl.dataset.iconFallback = 'emoji';
    }
}

/**
 * SESSION-IDENTITY-V2 - swap the header icon + title in one DOM operation.
 *
 * @param {{ icon: 'brand' | 'cloude', title: string, subheader?: string|null }} opts
 *   icon → fix/icon-consistency: no longer selects between an emoji and
 *     the SVG (the SVG now renders on every screen, see below). Only
 *     decides whether the title span is wired for inline rename -
 *     'cloude' means "this is a live session", so only the terminal
 *     screen passes it. Kept as a named value rather than a bool so a
 *     future third screen state doesn't have to be shoehorned into true/false.
 *   title → text content of the title span (alongside the .version chip)
 *   subheader → HOME-HEADER-CONSOLIDATION: when present, the header grows a
 *     second row under `.header-row` carrying this text, and `.header-row`
 *     (the wrapper around toggle/title/controls - see its CSS comment)
 *     switches to the `.header--home` grid layout so the title is
 *     genuinely centred regardless of `.controls` width. Omitted/null on
 *     every other screen, which removes the row and reverts to the plain
 *     flex layout. Only the launchpad screen passes this - see
 *     showLaunchpad() below.
 */
function setHeaderIdentity(opts) {
    var iconEl = document.getElementById('header-icon');
    var textEl = document.getElementById('header-title-text');
    // `.header--home` is applied to `.header-row`, NOT `.header` itself -
    // `#home-subheader` must stay OUTSIDE the element h1's own
    // header-title-fit.js measures its siblings against, or its
    // full-width second row gets subtracted from the title's shrink
    // budget and silently truncates it mid-word. See `.header-row`'s CSS
    // comment for the incident this fixed.
    var headerRowEl = document.querySelector('.header-row');
    var subheaderEl = document.getElementById('home-subheader');
    if (headerRowEl && subheaderEl) {
        if (opts.subheader) {
            headerRowEl.classList.add('header--home');
            // DELIBERATELY A DIFFERENT CLASS NAME from the one on
            // .header-row, not a duplicate. `.header--home { display: grid }`
            // in styles.css is a bare class selector - it matches ANY
            // element carrying that class. Reusing the exact same name on
            // <body> once made the whole page body a grid container
            // instead of a flex column (bodyDisplay: 'grid', header
            // collapsed to grid-content width ~517px instead of 1280px -
            // caught by the Playwright measurement harness).
            // `home-header-active` exists purely so
            // --home-subheader-extra (styles.css) reaches --header-h
            // consumers outside .header, e.g. .fab-menu-notice. Keep this
            // toggle in lockstep with the .header-row one above.
            document.body.classList.add('home-header-active');
            subheaderEl.textContent = opts.subheader;
            subheaderEl.hidden = false;
        } else {
            headerRowEl.classList.remove('header--home');
            document.body.classList.remove('home-header-active');
            subheaderEl.hidden = true;
        }
    }
    if (iconEl) {
        // fix/icon-consistency - the brand mark is identical on every
        // screen now (auth, launchpad, terminal all identify as the same
        // app), so this no longer branches on opts.icon. Idempotent: if
        // the real image is already in place (the common case - the
        // static markup in index.html ships it by default, see
        // HEADER_BRAND_ICON_URL's comment) this is a no-op rather than
        // tearing down and reloading the asset on every screen swap,
        // which would also re-arm the error listener needlessly.
        var existingImg = iconEl.querySelector('img[data-brand-icon]');
        if (!existingImg) {
            // Use an <img> rather than inlining the artwork so the asset can
            // be swapped without re-editing markup, and so the browser
            // caches it. srcset gives the 2x asset to retina displays.
            iconEl.innerHTML = '<img data-brand-icon src="' + HEADER_BRAND_ICON_URL +
                '" srcset="' + HEADER_BRAND_ICON_URL + ' 1x, ' + HEADER_BRAND_ICON_URL_2X + ' 2x" alt="" />';
            delete iconEl.dataset.iconFallback;
            existingImg = iconEl.querySelector('img');
        }
        // Covers BOTH paths: the freshly-created <img> above, and the one
        // index.html ships statically (present on the very first call,
        // before this function has ever run) - that static <img> has no
        // error listener yet until this line, so a load failure on first
        // paint would otherwise go undetected.
        if (existingImg && !existingImg.dataset.errorWired) {
            existingImg.dataset.errorWired = 'true';
            existingImg.addEventListener('error', _onHeaderIconLoadError, { once: true });
        }
    }
    if (textEl) {
        // Route through HeaderTitleFit so a long session name is
        // MIDDLE-elided (keeping the distinguishing tail) rather than
        // end-truncated by CSS. Falls back to a plain write when the
        // module is absent - the CSS ellipsis still prevents overflow.
        var fullTitle = opts.title || 'Cloude Code';
        if (window.HeaderTitleFit) {
            window.HeaderTitleFit.setTitle(fullTitle);
        } else {
            textEl.textContent = fullTitle;
        }
    }
    // v0.7.2 - when we're painting a session identity (icon='cloude'),
    // make the title span itself the click target for inline rename.
    // On the launchpad / auth screens we unwire so the affordance never
    // bleeds across screen transitions.
    if (opts.icon === 'cloude') {
        _wireHeaderTitleRename();
    } else {
        _unwireHeaderTitleRename();
    }
}

/**
 * v0.7.2 - Mount a small pencil button next to ``#header-title-text`` for
 * inline rename. The button is idempotent (re-wiring does not double-mount).
 * Click handler delegates to TerminalController which owns the rename input state.
 */
function _wireHeaderTitleRename() {
    const titleEl = document.getElementById('header-title-text');
    if (!titleEl) return;
    // Drop legacy editable behavior on the title text itself
    titleEl.classList.remove('header-title-editable');
    titleEl.removeAttribute('title');
    // Mount a small pencil button next to the title (idempotent)
    let pencilEl = document.getElementById('header-rename-pencil');
    if (!pencilEl) {
        pencilEl = document.createElement('span');
        pencilEl.id = 'header-rename-pencil';
        pencilEl.className = 'header-rename-pencil';
        pencilEl.setAttribute('role', 'button');
        pencilEl.setAttribute('tabindex', '0');
        pencilEl.setAttribute('title', 'rename');
        pencilEl.textContent = '✎'; // ✎
        titleEl.insertAdjacentElement('afterend', pencilEl);
    }
    const trigger = (e) => {
        e.stopPropagation();
        if (window.TerminalController && typeof window.TerminalController._enterHeaderRename === 'function') {
            window.TerminalController._enterHeaderRename();
        }
    };
    pencilEl.onclick = trigger;
    pencilEl.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') trigger(e);
    };
}

function _unwireHeaderTitleRename() {
    const titleEl = document.getElementById('header-title-text');
    if (titleEl) {
        titleEl.classList.remove('header-title-editable');
        titleEl.removeAttribute('title');
        titleEl.onclick = null;
    }
    const pencilEl = document.getElementById('header-rename-pencil');
    if (pencilEl && pencilEl.parentNode) pencilEl.parentNode.removeChild(pencilEl);
}

/**
 * The string a HUMAN should see for one session, or null.
 *
 * Description: a thin front for window.SessionLabel.resolve, which holds
 *   the ONE fallback rule this app has for "what is this session called"
 *   (label, else the cloude_-stripped tmux name, else null). It is here
 *   so the two screen-transition paths below and the tab-title function
 *   all ask the same question in the same words instead of each carrying
 *   their own chain.
 *
 *   A SESSION'S NAME IS A LABEL, NOT ITS TMUX NAME. These two used to be
 *   one string. They are not any more, and every surface that renders
 *   identity has to read the label - a surface still painting the tmux
 *   handle looks broken the moment a label differs from it, which is the
 *   normal case rather than the exotic one.
 * Inputs: sessionLike (object|null) - anything carrying ``label`` and
 *   ``name``, or a SessionInfo carrying ``label`` and ``tmux_session``.
 * Output: string|null - null means this session cannot be named at all.
 * Example: sessionDisplayName({label: 'Media', tmux_session: 'cloude_m'})
 */
function sessionDisplayName(sessionLike) {
    if (!sessionLike || typeof sessionLike !== 'object') return null;
    var row = {
        label: sessionLike.label,
        name: sessionLike.name || sessionLike.tmux_session || null,
    };
    if (window.SessionLabel) return window.SessionLabel.resolve(row);
    // session-label.js is loaded before this file by client/index.html.
    // If it somehow is not, fall back rather than blanking the tab.
    if (typeof row.label === 'string' && row.label.trim()) return row.label.trim();
    return row.name || null;
}
window.sessionDisplayName = sessionDisplayName;

/**
 * v0.7.1 - Browser tab title sync.
 *
 * The page title reflects whichever session is active for the user's
 * current screen. On the launchpad / auth screens it falls back to the
 * brand. On the terminal screen we use ``<name> - Cloude Code`` so the
 * window title in a multi-tab browser is identifiable at a glance
 * (matches the convention used by VS Code, IntelliJ, etc.).
 *
 * THE NAME IS RESOLVED IN HERE, NOT BY THE CALLERS. It used to take a
 * pre-resolved string, and three separate call sites each decided what
 * that string was - which is three chances to forget that a session's
 * displayed name is its LABEL and not its tmux handle. Resolving here
 * makes forgetting structurally impossible for the tab title.
 *
 * Called from:
 *   - showTerminal / returnToExistingTerminal - paint session name
 *   - showLaunchpad - clear back to brand
 *   - terminal.js WS handler on session.renamed - live-update for the
 *     attached session
 *
 * @param {?(string|object)} session  A session-shaped object carrying
 *   ``label`` and ``name``/``tmux_session``, or an already-resolved
 *   display string, or null/empty to reset to the brand. A session that
 *   cannot be named resolves to the bare brand - never to the literal
 *   word "null", which is what String()-ing a JSON null would put in the
 *   user's tab.
 */
function setPageTitle(session) {
    var brand = 'Cloude Code';
    var name = typeof session === 'string' ? session : sessionDisplayName(session);
    if (name && String(name).trim()) {
        document.title = String(name).trim() + ' - ' + brand;
    } else {
        document.title = brand;
    }
}
// Expose so terminal.js's WS message handler can call it without
// reaching into the app instance.
window.setPageTitle = setPageTitle;

/**
 * App Controller - Manages application state and screen transitions
 */
class AppController {
    constructor() {
        this.currentScreen = null;
        this.logoutBtn = null;
        // No destroyBtn: delete is no longer reachable from the session
        // header (see the conversation sidebar + launcher rows instead).
        // NO detachBtn either. Detach moved into the session editor FAB
        // (session-editor-menu.js): it acts on the SESSION, so it belongs
        // with the session-scoped control, not in the app-scoped header
        // that also mounts on the launchpad where there is no session.
        // NO homeBtn. Clicking #appTitle is the one home control; see
        // the DismissGuard wiring in _wireControls() and goHome().
        // Settings gear - visible whenever authenticated (launchpad AND
        // terminal), hidden pre-auth. Same visibility wiring as
        // logoutBtn, not gated to a single screen.
        this.settingsBtn = null;
        // Claude-config editor button - same always-visible-when-authenticated
        // wiring as settingsBtn (config applies whether or not a session
        // is open).
        this.configEditorBtn = null;
        // Health poller state. Poll every 15s against /health so the
        // top-right status dot reflects server reachability on the
        // auth + launchpad screens. The terminal screen manages the
        // same dot via its WS updateStatus() calls, so the poller
        // yields whenever currentScreen === 'terminal'.
        this._healthPollerInterval = null;
        // MutationObserver mirroring #statusText's data-status into the
        // home bar's label. Created once, in _observeStatusText().
        this._statusTextObserver = null;
    }

    /**
     * Move the ONE connection-light node to the screen that is showing.
     *
     * WHY MOVE AND NOT CLONE: `#statusText` is written by id from two
     * places (this class's `_pollHealth()` and TerminalController's
     * `updateStatus()`). A second copy would need a second writer and
     * would drift, which is the same reasoning header-menu.js records for
     * re-parenting header controls instead of mirroring them.
     *
     * THE LIGHT IS NEVER IN THE HEADER. It used to return to
     * `.header .controls` on the auth and terminal screens; it does not
     * any more. The header row is actions, the light is state, and the
     * rule is "the light lives in the screen's bottom furniture, or
     * nowhere".
     *
     * HOME SCREEN: into `#home-bar-status` in `.home-bar`, beside a
     * visible text label - the light is app-scoped state and the bar has
     * the room to say what it means.
     *
     * TERMINAL SCREEN: into `#terminal-bar-status`, inside `.info` -
     * the terminal screen's real bottom bar, in flow, spanning the full
     * width the same way `.home-bar` does on the home screen. `.info`
     * already existed (it has always shown "Session: ... | PID: ...");
     * the light moved into it instead of getting a floating chip of its
     * own. Same reasoning as the home bar: the dot leads a shrinkable
     * text label, both on the right of a spacer that pushes them away
     * from the session id on the left. See terminal-tools.css.
     *
     * AUTH SCREEN: neither `#home-bar-status` nor `#terminal-bar-status`
     * exists in the auth screen's DOM, so the node has no target and
     * stays wherever it last was, unattached and invisible. No bar, no
     * light - the stated rule, applied honestly. The auth screen reports
     * its own failures inline.
     *
     * ARCHIVE: `#archive-bar-status` lives in the shell that
     * archive-screen.js builds at SCRIPT LOAD, precisely so it exists
     * before this runs - showArchive() calls this before ArchiveScreen
     * .show(), the same order showLaunchpad() uses. A third named target
     * rather than an else-branch: left alone, the archive screen would
     * put its light in the terminal bar, which is not on screen.
     *
     * @param {'auth'|'launchpad'|'terminal'|'archive'} screen - Screen being shown.
     * @returns {void}
     */
    _placeStatusLight(screen) {
        const el = document.getElementById('statusText');
        if (!el) return;
        const target = screen === 'launchpad'
            ? document.getElementById('home-bar-status')
            : screen === 'archive'
                ? document.getElementById('archive-bar-status')
                : document.getElementById('terminal-bar-status');
        if (!target || el.parentElement === target) return;
        // Before the label span in both bars, so the dot leads the pair.
        target.insertBefore(el, target.firstChild);
        this._syncStatusLabel();
    }

    /**
     * Copy `#statusText`'s current `data-status` into whichever bar
     * label is present - home bar, terminal bar, or (on the auth screen)
     * neither.
     *
     * The attribute stays the single source of truth; this only renders
     * it somewhere a touch user can read without hovering.
     *
     * @returns {void}
     */
    _syncStatusLabel() {
        const el = document.getElementById('statusText');
        const text = el ? (el.getAttribute('data-status') || '') : '';
        const homeLabel = document.getElementById('home-bar-status-text');
        if (homeLabel) homeLabel.textContent = text;
        const terminalLabel = document.getElementById('terminal-bar-status-text');
        if (terminalLabel) terminalLabel.textContent = text;
        const archiveLabel = document.getElementById('archive-bar-status-text');
        if (archiveLabel) archiveLabel.textContent = text;
    }

    /**
     * Watch `#statusText` so the home bar label can never go stale.
     *
     * An observer rather than a call added to each writer: there are two
     * writers today (`_pollHealth()` here and TerminalController's
     * `updateStatus()`) and the next one would have to remember. Watching
     * the attribute means the label follows whoever wrote it.
     *
     * @returns {void}
     */
    _observeStatusText() {
        if (this._statusTextObserver) return;
        const el = document.getElementById('statusText');
        if (!el || typeof MutationObserver !== 'function') return;
        this._statusTextObserver = new MutationObserver(() => this._syncStatusLabel());
        this._statusTextObserver.observe(el, { attributes: true, attributeFilter: ['data-status'] });
    }

    /**
     * Initialize application
     */
    async init() {
        console.log('App: Initializing');

        this.logoutBtn = document.getElementById('logoutBtn');
        this.settingsBtn = document.getElementById('settingsBtn');
        this.configEditorBtn = document.getElementById('configEditorBtn');
        this._observeStatusText();

        // Phase 2: paint persisted theme id onto <html> SYNCHRONOUSLY before
        // any async work - kills FOUC for repeat visitors. The full manifest
        // (cssVars + xterm) loads post-auth via Themes.init() below; until
        // then the :root defaults from styles.css already render claude.
        if (window.Themes && typeof window.Themes.applyStoredThemeIdSync === 'function') {
            try { window.Themes.applyStoredThemeIdSync(); } catch (_) { /* no-op */ }
        }

        // Initialize per-theme background-music plumbing. There is no
        // app-level audio control to wire: audio is session-only, gated
        // solely by the session editor FAB's "play music" row. init() also
        // runs the settings migration that drops the retired master switch.
        if (window.ThemeAudio && typeof window.ThemeAudio.init === 'function') {
            try { window.ThemeAudio.init(); } catch (_) { /* no-op */ }
        }

        // Setup event listeners
        this.setupEventListeners();

        // Initialize auth module (always needed first)
        window.Auth.init();

        // Kick off server health polling before auth resolves - the
        // /health endpoint is unauthenticated, so the dot works on the
        // auth screen too.
        this._startHealthPoller();

        // Check if user is authenticated
        if (window.Auth.isAuthenticated()) {
            console.log('App: User has token, verifying...');
            const isValid = await window.Auth.verifyToken();
            if (isValid) {
                // Phase 2: load full theme manifests + mount selector BEFORE
                // launchpad render or any deep-link resolves. Failure here is
                // non-fatal - registry has its own claude fallback.
                await this._initThemes();
                this.showLaunchpad();
            } else {
                console.log('App: Token invalid, showing auth');
                this.showAuth();
            }
        } else {
            console.log('App: No token, showing auth');
            this.showAuth();
        }
    }

    /**
     * Phase 2: bring up the theme registry. Called post-auth so the
     * manifest fetch goes through with a valid Bearer token. Idempotent -
     * safe to call again on re-auth.
     *
     * feat/settings-screen: no longer mounts a `<select>` into the
     * header - the theme chooser moved into the settings panel (gear
     * icon; see settings-panel.js's renderAppearanceSection /
     * mountThemeSlot, which calls window.ThemeSelector.mount() itself
     * the first time the panel opens). Only the registry needs to be
     * live at boot; the picker DOM is built lazily on demand.
     */
    async _initThemes() {
        if (!window.Themes) return;
        try {
            await window.Themes.init();
        } catch (e) {
            console.warn('App: Themes.init failed - registry will use fallback', e);
        }
    }

    /**
     * Start the server-health poller. Idempotent - safe to call more
     * than once. Fires an initial probe immediately, then every 15s.
     */
    _startHealthPoller() {
        if (this._healthPollerInterval) return;
        this._healthPollerInterval = setInterval(() => this._pollHealth(), 15000);
        this._pollHealth();
    }

    /**
     * Probe GET /health and paint the top-right status dot.
     *
     * States:
     *   - green (.connected): HTTP 200
     *   - red (.error):       network error, timeout, or non-2xx
     *   - orange (default):   initial state before first probe
     *
     * Yields to the terminal screen's WS updateStatus() by returning
     * early when currentScreen === 'terminal' - otherwise the 15s
     * tick would clobber the live WS status (e.g. "Connected").
     */
    async _pollHealth() {
        if (this.currentScreen === 'terminal') return;
        const statusEl = document.getElementById('statusText');
        if (!statusEl) return;
        try {
            const r = await fetch('/health', { method: 'GET', cache: 'no-store' });
            let text;
            if (r.ok) {
                statusEl.className = 'status connected';
                text = 'server OK';
            } else {
                statusEl.className = 'status error';
                text = `server error · HTTP ${r.status}`;
            }
            statusEl.setAttribute('data-status', text);
            // aria-label mirrors the ::after tooltip text so screen readers
            // get the same live state a sighted hover shows.
            statusEl.setAttribute('aria-label', text);
        } catch (err) {
            statusEl.className = 'status error';
            const text = `server unreachable · ${err && err.message ? err.message : err}`;
            statusEl.setAttribute('data-status', text);
            statusEl.setAttribute('aria-label', text);
        }
    }

    /**
     * Open the settings panel when the URL asks for it.
     *
     * Description: honours `#settings` (the menu bar's deep link) exactly
     *   once, then strips the hash so a refresh does not reopen the modal
     *   over whatever the user moved on to.
     * Inputs: none - reads window.location.
     * Output: void.
     */
    _openSettingsIfDeepLinked() {
        if (window.location.hash !== '#settings') return;
        history.replaceState(null, '', window.location.pathname + window.location.search);
        if (window.SettingsPanel) window.SettingsPanel.open(this.settingsBtn || null);
    }

    /**
     * Setup event listeners
     */
    setupEventListeners() {
        // Auth events
        window.addEventListener('authenticated', () => {
            console.log('App: User authenticated');
            // Bring up the theme registry post-auth (for the TOTP-flow path
            // that doesn't go through init()'s `if (verifyToken())` branch).
            this._initThemes().finally(() => this.showLaunchpad());
        });

        window.addEventListener('auth-required', () => {
            console.log('App: Auth required');
            this.showAuth();
        });

        window.addEventListener('logged-out', () => {
            console.log('App: User logged out');
            this.showAuth();
        });

        // Session events. The `detail` payload may include adopt-path
        // extras (`initialScrollbackB64`, `fifoStartOffset`) when the
        // launchpad dispatched after adopting an external session -
        // forward the whole thing so showTerminal() can plumb to the
        // terminal controller's connectToSession() opts.
        window.addEventListener('session-created', (e) => {
            console.log('App: Session created', e.detail);
            this.showTerminal(e.detail.session, {
                initialScrollbackB64: e.detail.initialScrollbackB64,
                fifoStartOffset: e.detail.fifoStartOffset,
            });
        });

        window.addEventListener('session-destroyed', () => {
            console.log('App: Session destroyed');
            this.showLaunchpad();
        });

        // Title click - go home. THE ONLY HOME CONTROL NOW.
        //
        // #homeBtn is gone: the title already did this, and two controls
        // for one navigation is a control the header cannot afford at
        // phone width. It calls goHome() rather than showLaunchpad(),
        // which is the behaviour the button carried and the title did
        // NOT - goHome() also pauses the terminal's WebSocket via
        // pauseForHome(). Wiring the title to bare showLaunchpad() while
        // deleting the button would have silently dropped that pause.
        //
        // Routed through DismissGuard.onContainerActivate, not a bare
        // click listener. #appTitle is also the mount point for the
        // rename pencil and the inline rename input (see
        // TerminalController._enterHeaderRename): with a bare listener,
        // clicking into the rename field navigated away from the session
        // mid-edit. Only a click on the title chrome itself counts as
        // "go back". Do not replace this with addEventListener.
        // See client/js/dismiss-guard.js.
        const appTitle = document.getElementById('appTitle');
        window.DismissGuard.onContainerActivate(appTitle, () => {
            if (this.currentScreen === 'terminal') {
                console.log('App: Title clicked, returning to launcher');
                this.goHome();
                return;
            }
            // THE ARCHIVE'S ONLY IN-APP EXIT. Its own Back button steps
            // panes within the archive and bottoms out at the root pane,
            // so before this the browser's Back button was the only way
            // out. Routed through ArchiveEntry.close() rather than
            // showLaunchpad() because the address bar has to be written
            // too - see that function for why showLaunchpad() cannot do
            // it itself.
            if (this.currentScreen === 'archive') {
                console.log('App: Title clicked, leaving the archive');
                if (window.ArchiveEntry && typeof window.ArchiveEntry.close === 'function') {
                    window.ArchiveEntry.close();
                } else {
                    // Named, not silent: a title that does nothing is
                    // indistinguishable from a title nobody clicked.
                    console.warn('App: ArchiveEntry.close is unavailable; ' +
                                 'the archive has no exit.');
                }
            }
        });

        // Logout - click wiring only; visibility is toggled alongside
        // settingsBtn in showAuth/showLaunchpad/showTerminal.
        //
        // THIS LISTENER IS THE FIX, NOT A TIDY-UP. The button used to
        // carry `onclick="App.logout()"` in client/index.html, and
        // src/main.py's `script-src 'self'` has forbidden inline event
        // handlers since the CSP landed - so the click ran nothing and
        // reported nothing the user could see. Wiring it here is what
        // makes the button work at all; see the comment on the element
        // in index.html before moving it back.
        if (this.logoutBtn) {
            this.logoutBtn.addEventListener('click', () => this.logout());
        }

        // Settings gear - click wiring only; visibility is toggled
        // alongside logoutBtn in showAuth/showLaunchpad/showTerminal.
        if (this.settingsBtn) {
            this.settingsBtn.addEventListener('click', () => {
                if (window.SettingsPanel) window.SettingsPanel.open(this.settingsBtn);
            });
        }

        // Claude-config editor gear-neighbor - click wiring only;
        // visibility toggled alongside settingsBtn in showAuth/
        // showLaunchpad/showTerminal.
        if (this.configEditorBtn) {
            this.configEditorBtn.addEventListener('click', () => {
                if (window.ConfigEditorPanel) window.ConfigEditorPanel.open(this.configEditorBtn);
            });
        }
    }

    /**
     * Home: return to the launcher while leaving the current session
     * fully attached and running server-side.
     *
     * Description: never calls API.detachSession() or destroySession(),
     *   and never touches TerminalController.sessionActive - the session
     *   stays adopted on the server exactly as if the user had done
     *   nothing, so it keeps appearing in GET /sessions/list (not just
     *   /attachable) and clicking its launcher row reconnects immediately
     *   via TerminalController.reconnectToExistingSession(), the same
     *   path the launcher's existing "return to running session" row
     *   click already uses. Closes the browser-side WebSocket first (via
     *   TerminalController.pauseForHome()) purely to save battery/data
     *   while the session sits unattended on the launcher screen - the
     *   return path force-closes and reopens the socket regardless, so
     *   this costs nothing functionally on re-entry. No confirmation: this
     *   is pure navigation, not a destructive action.
     * Inputs: none.
     * Output: void. No-op if not currently on the terminal screen (the
     *   button is hidden everywhere else, but this guards direct calls).
     */
    goHome() {
        if (this.currentScreen !== 'terminal') return;
        console.log('App: Home clicked, returning to launcher (session stays attached)');
        if (window.TerminalController && typeof window.TerminalController.pauseForHome === 'function') {
            window.TerminalController.pauseForHome();
        }
        this.showLaunchpad();
    }

    /**
     * Show auth screen
     */
    showAuth() {
        console.log('App: Showing auth screen');
        this.hideAllScreens();
        document.getElementById('auth-screen').classList.add('active');
        // NO PER-BUTTON HIDE LIST HERE ANY MORE. Three
        // classList.add('hidden') calls used to sit on this line,
        // naming logoutBtn/settingsBtn/configEditorBtn, and every header
        // control that was not on that list rendered on the login
        // screen - which is how the slash-commands button and the header
        // kebab both leaked. The marker below flips the default: CSS
        // hides the authenticated-only chrome whenever it is absent, so
        // a control nobody remembers is hidden rather than shown. See
        // client/js/screen-chrome.js. Deliberately NOT guarded with an
        // `if (window.ScreenChrome)` the way the optional collaborators
        // below are: this is the gate, and a missing gate must fail
        // loudly here rather than quietly leave the login screen
        // showing controls again.
        window.ScreenChrome.apply('auth');
        if (window.SessionSidebar) window.SessionSidebar.hide();
        this.currentScreen = 'auth';
        this._placeStatusLight('auth');
        if (window.GlobalAudioToggle) window.GlobalAudioToggle.place('auth');
        // The auth screen is not a session: drop the session theme scope and
        // paint the user's own global theme. Same one call every navigation
        // makes - see client/js/theme-navigation.js for why the three
        // hand-copied versions of this block became one function. It also
        // re-syncs the audio gate, which must happen after the scope clears.
        window.ThemeNavigation.applyForGlobal();
        setHeaderIdentity({ icon: 'brand', title: 'Cloude Code' });
        // v0.7.1 - auth screen has no session context; reset tab title.
        setPageTitle(null);
        // Outbound URL sync: no session context on the auth screen either.
        if (window.Router && typeof window.Router.resetToLauncher === 'function') {
            window.Router.resetToLauncher();
        }
    }

    /**
     * Description: hand a stashed archive route to showArchive(), if
     *   router.js parked one because auth had not completed when the URL
     *   was read. Idempotent: clears the stash on the way through, so a
     *   later showLaunchpad() does not bounce the user back.
     *
     *   CALLED FROM showLaunchpad()'S FIRST LINE, AND IT RETURNS THERE.
     *   Called from showLaunchpad() for the same reason the settings deep
     *   link is: only ONE of the two login paths fires the
     *   `authenticated` event (init()'s existing-token branch calls
     *   showLaunchpad() directly), and BOTH paths reach it.
     *   FIRST, because MEASURED 2026-08-31: delivering the archive route
     *   from router.js's `authenticated` listener instead produced a race
     *   the launchpad won every time - both handlers run on their own
     *   setTimeout(0), the archive screen was activated, and then
     *   hideAllScreens() from showLaunchpad() put it straight back. The
     *   observable result was a fully built and loaded archive screen -
     *   50 transcript rows in the DOM - inside a `display: none` div,
     *   with /archive in the address bar, the launcher on screen and no
     *   error anywhere. Consuming the stash before showLaunchpad() has
     *   touched any screen state removes the race rather than re-tuning
     *   it, which is why it is the first line and not the last.
     * Inputs: none.
     * Output: boolean - true when a route was delivered; the caller must
     *   return on true, or it will render the launchpad over the archive.
     */
    _showArchiveIfDeepLinked() {
        const target = window.ArchiveDeepLinkTarget;
        if (!target) return false;
        window.ArchiveDeepLinkTarget = null;
        this.showArchive(target);
        return true;
    }

    /**
     * Description: show the archive (message browser) screen.
     *
     * Modelled directly on showLaunchpad(). It does not call
     * SessionSidebar.show(): the sidebar is the working set of LIVE
     * sessions and the archive is a different corpus with its own
     * navigation, so showing both puts two unrelated trees on one screen.
     *
     * IT NOW CALLS SessionSidebar.hide(), AND THE COMMENT THAT SAID IT
     * MUST NOT WAS STALE. That comment read "hide() persists a closed
     * state that then affects the launchpad". True when it was written;
     * false since 4af93ca, which changed hide() to close({persist: false})
     * for exactly that reason - see hide()'s own doc comment, which now
     * says leaving a screen is not the user closing the bar. Nobody
     * re-read the claim after the thing it described was fixed, so the
     * archive went on being the one authenticated screen that left the
     * conversation list on top of itself. What the user reported:
     * "when clicking into it, it should take over the page, no
     * conversations sidebar". With the bar PINNED the cost is not merely
     * cosmetic - `body.session-sidebar-pinned .screen` pads every screen,
     * `#archive-screen` included, by --sidebar-dock-w, so the archive was
     * rendering into a 320px-narrower box with the panel sitting in the
     * gap. hide() takes the panel off screen, drops that body class via
     * SessionSidebarPin.apply(), stops the poller, and leaves the user's
     * open/pinned PREFERENCE untouched, so showLaunchpad()'s and
     * showTerminal()'s existing show() call restores it on the way out.
     *
     * IT ALSO DROPS THE SESSION IDENTITY, which is the second half of the
     * same report ("it stays on current session"). Arriving from a
     * terminal, showArchive() used to leave FOUR live references to the
     * session the user had just navigated away from: the sidebar's
     * active-row pin (setActiveSession was never cleared), the header
     * title, the browser tab title, and the per-session theme scope.
     * Clearing them is not cosmetic tidying - a screen that still claims
     * to be a session is a screen whose Back, theme and audio controls
     * all act on something that is no longer on screen.
     *
     * THE WEBSOCKET IS PAUSED, NOT DETACHED, and the distinction is the
     * whole point. This reuses TerminalController.pauseForHome() - the
     * same call App.goHome() makes - which closes only the browser-side
     * socket under the existing _intentionalClose flag. The tmux session
     * and the server's session record both stay alive and adopted, so the
     * session keeps appearing in GET /sessions/list and re-entering it
     * reconnects through the usual reconnectToExistingSession() path.
     * Anything stronger here (detachSession, destroySession) would mean a
     * user lost a live session by looking at the archive, which is a far
     * worse bug than the layout one this change is fixing.
     *
     * Inputs: params (object) - {view, projectId, transcriptId, lineNo,
     *   query} from router.js. May be {} for the bare /archive route.
     * Output: void.
     */
    showArchive(params) {
        console.log('App: Showing archive screen', params);
        // Captured BEFORE hideAllScreens()/currentScreen is reassigned,
        // for the same reason showTerminal() captures cameFromTerminal:
        // the question is what the user was on a moment ago, and by the
        // end of this function that is no longer readable anywhere.
        const cameFromTerminal = this.currentScreen === 'terminal';
        if (cameFromTerminal && window.TerminalController
            && typeof window.TerminalController.pauseForHome === 'function') {
            window.TerminalController.pauseForHome();
        }
        this.hideAllScreens();
        document.getElementById('archive-screen').classList.add('active');
        // Same one-way opt-in as showLaunchpad(): these ship
        // class="hidden" in index.html so they are absent on first paint.
        // Stripping it is not the screen gate; ScreenChrome.apply() is.
        this.logoutBtn.classList.remove('hidden');
        if (this.settingsBtn) this.settingsBtn.classList.remove('hidden');
        if (this.configEditorBtn) this.configEditorBtn.classList.remove('hidden');

        this.currentScreen = 'archive';
        window.ScreenChrome.apply('archive');
        this._placeStatusLight('archive');
        if (window.GlobalAudioToggle && typeof window.GlobalAudioToggle.place === 'function') {
            window.GlobalAudioToggle.place('archive');
        }
        // THE ARCHIVE IS A FULL-PAGE MODE. Order matters: clear the
        // active-session pin FIRST so that if anything re-renders the row
        // list on the way down it cannot re-mark a row active, then take
        // the bar off screen. hide() does not persist, so the user's own
        // open/pinned choice survives and is restored by the show() that
        // showLaunchpad() and showTerminal() already call on the way back.
        if (window.SessionSidebar) {
            window.SessionSidebar.setActiveSession(null, null);
            window.SessionSidebar.hide();
        }
        // THE ARCHIVE IS NOT A SESSION. Drop the pin scope and restore the
        // user's global theme, so a ThemeSelector swap made while browsing
        // the archive cannot PATCH a pin onto a session the user is no
        // longer looking at. This used to be a six-line block copy-pasted
        // from showLaunchpad() under a comment declaring the duplication
        // deliberate; that decision is what let the session-to-session
        // switch ship with no restore at all. One function now.
        window.ThemeNavigation.applyForGlobal();
        // NO `subheader` HERE, DELIBERATELY. A subheader switches
        // `.header-row` to the `.header--home` GRID layout and stamps
        // `home-header-active` on <body>; that is the launchpad's layout
        // and borrowing it would change the archive's header geometry as
        // a side effect of wanting a caption. The archive takes the plain
        // flex header the terminal screen uses.
        setHeaderIdentity({ icon: 'brand', title: 'Message archive' });
        // The tab title said the session's name for as long as the user
        // browsed the archive. It is not that session any more.
        setPageTitle(null);
        if (window.ArchiveScreen && typeof window.ArchiveScreen.show === 'function') {
            window.ArchiveScreen.show(params || {});
        } else {
            // A NAMED refusal. The screen div is active and empty at this
            // point, and a blank screen with no console line is the
            // hardest defect to trace back to a missing script tag.
            console.error('App: ArchiveScreen is not loaded - check the ' +
                'archive script tags in index.html.');
        }
    }

    /**
     * Show launchpad screen
     */
    showLaunchpad() {
        console.log('App: Showing launchpad screen');
        // Archive deep link: consumed FIRST, and it RETURNS. See
        // _showArchiveIfDeepLinked() for why the position matters.
        if (this._showArchiveIfDeepLinked()) return;
        this.hideAllScreens();
        document.getElementById('launchpad-screen').classList.add('active');
        // These three ship `class="hidden"` in index.html so they are
        // absent on first paint; stripping it is a one-way opt-in, not
        // the screen gate. The screen gate is the marker below.
        this.logoutBtn.classList.remove('hidden');
        if (this.settingsBtn) this.settingsBtn.classList.remove('hidden');
        if (this.configEditorBtn) this.configEditorBtn.classList.remove('hidden');
        // THE SIDEBAR IS AVAILABLE ON THE HOME SCREEN TOO, and pinnable
        // there. It used to be hidden here, which meant a pinned bar
        // vanished the moment you went home and (because hide() closed it
        // and close() persisted '0') came back CLOSED on the next session.
        // The home screen keeps its own project-to-session tree; this bar
        // is the working set of sessions, not a project browser.
        // `#launchpad-screen` already carries `.screen`, so the docked
        // layout offset in session-sidebar.css applies here unchanged.
        if (window.SessionSidebar) {
            window.SessionSidebar.setActiveSession(null, null);
            window.SessionSidebar.show();
        }
        this.currentScreen = 'launchpad';
        window.ScreenChrome.apply('launchpad');
        // The launchpad is not a session: drop the session theme scope so
        // the home chrome renders under pure global-theme rules, and paint
        // the user's own theme. This is the restore the owner could see
        // working - clicking the title DID change the theme back - while
        // the session-to-session switch had no equivalent. Both go through
        // the same function now.
        window.ThemeNavigation.applyForGlobal();
        // HOME-HEADER-CONSOLIDATION: the launchpad title + prompt used to be
        // a standalone block at the top of .launchpad-container (see
        // launchpad.js renderLaunchpadUI). It now lives in the header
        // itself, centred, with the prompt as a second row underneath -
        // reclaims the vertical space the standalone block used to cost.
        setHeaderIdentity({
            icon: 'brand',
            title: 'Cloude Code Launcher',
            subheader: 'select a project or create a new project'
        });
        // v0.7.1 - back on the launchpad, no active session; reset tab title.
        setPageTitle(null);
        // feat/settings-gui - honour the menu bar's `#settings` deep link.
        // Hooked HERE rather than on the `authenticated` event because
        // only one of the two login paths fires that event: init()'s
        // existing-token branch calls showLaunchpad() directly. Both
        // paths reach this line, and reaching it means a session exists,
        // which the panel needs - its first act is GET /config/settings.
        this._openSettingsIfDeepLinked();
        // Outbound URL sync: leaving a session (detach/delete) or just
        // navigating here resets the address bar to `/` so a refresh
        // lands on the launcher, not a stale/gone session URL. No-ops if
        // a deep-link target is still pending delivery - see
        // Router.resetToLauncher()'s doc comment.
        if (window.Router && typeof window.Router.resetToLauncher === 'function') {
            window.Router.resetToLauncher();
        }

        // Hide D-pad on launchpad
        if (window.DPad) {
            window.DPad.hide();
        }

        // Hide slash command button on launchpad
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.hide();
        }

        // Initialize launchpad if first time
        if (!window.Launchpad.launchpadScreen) {
            window.Launchpad.init();
        }

        // The home bar only exists once the launchpad markup is rendered,
        // which is why this is here and not beside the currentScreen
        // assignment above like the other two screens.
        this._placeStatusLight('launchpad');
        if (window.GlobalAudioToggle) window.GlobalAudioToggle.place('launchpad');

        // Reload projects
        window.Launchpad.loadProjects();
    }

    /**
     * Show terminal screen
     * @param {object} session - Session data from the backend
     * @param {object} [opts]
     * @param {string} [opts.initialScrollbackB64] - Adopt-path: base64
     *   scrollback bytes to paint into xterm before the WS opens.
     * @param {number} [opts.fifoStartOffset] - Adopt-path: fifo byte
     *   offset the server's tailer will start from. Passed through for
     *   symmetry/logging; not directly consumed by the client.
     */
    async showTerminal(session, opts = {}) {
        console.log('App: Showing terminal screen');
        // Outbound URL sync: capture whether we were ALREADY viewing a
        // session before this call flips currentScreen below. Deciding
        // push-vs-replace off the PREVIOUS screen is what tells "entering
        // a session from the launcher" (push - Back should return to the
        // launcher) apart from "switching to a different session while
        // already in one" (replace - the sidebar's adopt-not-yet-
        // attached flow calls showTerminal() too; we don't want Back to
        // have to click through every session the user visited).
        var cameFromTerminal = this.currentScreen === 'terminal';
        this.hideAllScreens();
        document.getElementById('terminal-screen').classList.add('active');
        // These three ship `class="hidden"` in index.html so they are
        // absent on first paint; stripping it is a one-way opt-in, not
        // the screen gate. The screen gate is the marker below.
        this.logoutBtn.classList.remove('hidden');
        if (this.settingsBtn) this.settingsBtn.classList.remove('hidden');
        if (this.configEditorBtn) this.configEditorBtn.classList.remove('hidden');
        this.currentScreen = 'terminal';
        window.ScreenChrome.apply('terminal');
        this._placeStatusLight('terminal');
        if (window.GlobalAudioToggle) window.GlobalAudioToggle.place('terminal');

        // SESSION-IDENTITY-V2 - enter per-session theme scope. Subsequent
        // ThemeSelector swaps will PATCH the server-side pin instead of
        // writing localStorage. Use tmux_session (canonical bare tmux name) or
        // session.name. Do NOT fall through to session.id - that's
        // "adopted:<name>" for adopted sessions, which the backend rejects
        // and causes the PATCH to 404, silently breaking pin persistence.
        var sessionName = (session && (session.tmux_session || session.name)) || null;
        // Enter this session's theme: its pin if it has one, the user's own
        // global theme if it does not. The "if it does not" half is the fix
        // for 2026-09-07 - this used to be a bare
        // `if (session.pinned_theme) applyTheme(...)` further down with no
        // else, so switching from a pinned session straight into an unpinned
        // one left the previous session's theme painted. Also sets the pin
        // scope and re-syncs the audio gate in the required order.
        window.ThemeNavigation.applyForSession(session, sessionName);
        // Outbound URL sync: reuses Router's SAME slug/encoding scheme
        // build_deep_link() (server) and parseCurrentPath() (inbound
        // router) already use - see Router.enterSession()'s doc comment
        // and _deepLinkSlug()'s below for why the name gets stripped
        // first.
        this._syncSessionUrl(sessionName, cameFromTerminal);
        // Session sidebar: reveal the hamburger and tell it which session
        // is now attached (so its row list can mark this one active).
        if (window.SessionSidebar) {
            window.SessionSidebar.show();
            window.SessionSidebar.setActiveSession(session && session.id, sessionName);
        }
        // Header identity: brand icon + the session's LABEL as title.
        // NOT sessionName - that is the tmux handle, which identity is
        // keyed on and which a rename deliberately never moves. What the
        // user called this session is what the header must say.
        var displayName = sessionDisplayName({ label: session && session.label,
                                               name: sessionName });
        setHeaderIdentity({
            icon: 'cloude',
            title: displayName || 'session'
        });
        // v0.7.1 - reflect the attached session in the browser tab title.
        setPageTitle({ label: session && session.label, name: sessionName });

        // Phase 4-5: scope the terminal screen + xterm palette to this
        // session's agent theme. If session.agent_type is null/undefined
        // (Phase 6 hasn't shipped yet, or the agent is unknown to the
        // theme registry), applySession() falls through to clearSession()
        // - meaning the global theme also rules the terminal. That's the
        // desired fallback: no flicker, no broken-state.
        if (window.Themes && typeof window.Themes.applySession === 'function') {
            window.Themes.applySession(session && session.agent_type);
        }

        // Initialize terminal if first time
        if (!window.TerminalController.term) {
            await window.TerminalController.init();
        }

        // Initialize D-pad (mobile only)
        if (window.DPad && !window.DPad.floatingButton) {
            window.DPad.init();
        }

        // Show D-pad on terminal screen
        if (window.DPad) {
            window.DPad.show();
        }

        // Initialize slash commands modal
        if (window.SlashCommandsModal && !window.SlashCommandsModal.button) {
            await window.SlashCommandsModal.init((command) => {
                // Insert command into terminal without Enter
                window.TerminalController.insertText(command);
            }, session && session.working_dir);
        }

        // Show slash command button on terminal screen
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.show();
        }

        // Connect terminal to session. Adopt-path opts (scrollback,
        // fifo offset) are forwarded through - a plain new-session
        // create leaves them undefined and connectToSession treats
        // that as a normal (non-adopt) path.
        window.TerminalController.connectToSession(session, opts);
        this.focusTerminal();
    }

    /**
     * Description: hand keyboard focus to the terminal's own input. Every
     *   path that puts a session on screen ends with this, because
     *   whatever the user clicked to get here - a sidebar row, most often
     *   - otherwise KEEPS focus, and that element's key handlers then
     *   swallow what the user believes they are typing at the terminal.
     *   Doing it in showTerminal() and returnToExistingTerminal() covers
     *   both arrival paths; the sidebar separately stops being focusable
     *   once closed (session-sidebar.js), because fixing only the switch
     *   path would leave every OTHER way of closing the bar stranded the
     *   same way.
     * Inputs: none.
     * Output: void.
     * Example: this.focusTerminal();
     */
    focusTerminal() {
        // Deferred for the same reason SlashCommandsModal.selectCommand()
        // defers it: .xterm-helper-textarea is created by xterm during its
        // own attach, so a synchronous query can find nothing at all.
        setTimeout(() => {
            const input = document.querySelector('.xterm-helper-textarea');
            if (input) input.focus();
        }, 100);
    }

    /**
     * Return to an ALREADY-ACTIVE terminal session without creating or
     * adopting anything. Used by the launchpad's active-session banner
     * when the user clicks "return to terminal" after navigating away
     * via the logo.
     *
     * The screen-transition side of this mirrors showTerminal() exactly
     * (so D-pad/slash-commands/header buttons land in the same state),
     * but the terminal-controller side calls reconnectToExistingSession
     * instead of connectToSession - the backend is already alive and a
     * POST /sessions would either error (single-session invariant) or
     * silently birth a new unrelated pane.
     *
     * @param {object} session - Session object (from GET /sessions).
     */
    async returnToExistingTerminal(session) {
        console.log('App: Returning to existing terminal', session && session.id);
        // Outbound URL sync: see showTerminal()'s identical comment -
        // same push-vs-replace rule, off the screen we were on BEFORE
        // this call. Callers: the launchpad's active-session banner
        // (currentScreen 'launchpad' → push) and the conversation
        // sidebar's row click (currentScreen already 'terminal' →
        // replace, since the sidebar only shows while in a session).
        var cameFromTerminal = this.currentScreen === 'terminal';
        this.hideAllScreens();
        document.getElementById('terminal-screen').classList.add('active');
        // These three ship `class="hidden"` in index.html so they are
        // absent on first paint; stripping it is a one-way opt-in, not
        // the screen gate. The screen gate is the marker below.
        this.logoutBtn.classList.remove('hidden');
        if (this.settingsBtn) this.settingsBtn.classList.remove('hidden');
        if (this.configEditorBtn) this.configEditorBtn.classList.remove('hidden');
        this.currentScreen = 'terminal';
        window.ScreenChrome.apply('terminal');
        this._placeStatusLight('terminal');
        if (window.GlobalAudioToggle) window.GlobalAudioToggle.place('terminal');

        // SESSION-IDENTITY-V2 - same wiring as showTerminal(). The session
        // arg here is typically a SessionInfo (carries tmux_session +
        // pinned_theme at the top level); fall back to nested .session for
        // older callers that pass the inner Session row.
        var inner = (session && session.session) ? session.session : session;
        var sessionName = (session && (session.tmux_session || session.name))
            || (inner && (inner.tmux_session || inner.name))
            || null;
        // THIS IS THE PATH THE SIDEBAR'S SESSION-TO-SESSION SWITCH TAKES
        // (session-sidebar-clicks.js calls straight into here, never via the
        // home screen). It used to read the pin, then paint it only
        // `if (pinnedTheme)`, so switching into an UNPINNED session painted
        // nothing and inherited the previous session's theme - the defect
        // the owner reported on 2026-09-07. applyForSession() is total: an
        // unpinned target restores the user's own global theme. It also
        // resolves the pin off both payload levels (`pinned_theme` rides on
        // the SessionInfo wrapper, not the nested `.session`), sets the pin
        // scope, and re-syncs the audio gate afterwards - the gate keys off
        // the active session name, so that ordering is not optional.
        window.ThemeNavigation.applyForSession(session, sessionName);
        // Outbound URL sync: same encoding Router.enterSession() shares
        // with build_deep_link() (server) and the inbound router parser.
        this._syncSessionUrl(sessionName, cameFromTerminal);
        // Session sidebar: same wiring as showTerminal().
        if (window.SessionSidebar) {
            var activeSid = (inner && inner.id) || (session && session.id) || null;
            window.SessionSidebar.show();
            window.SessionSidebar.setActiveSession(activeSid, sessionName);
        }
        // Same rule as showTerminal(): the header says the LABEL. The
        // outer SessionInfo carries it; an older caller handing us the
        // inner Session row carries none, which falls back to the tmux
        // name exactly as this surface always did.
        var reLabel = (session && session.label) || (inner && inner.label) || null;
        var reDisplay = sessionDisplayName({ label: reLabel, name: sessionName });
        setHeaderIdentity({
            icon: 'cloude',
            title: reDisplay || 'session'
        });
        // v0.7.1 - sync browser tab title to the re-entered session.
        setPageTitle({ label: reLabel, name: sessionName });

        // Phase 4-5: re-scope to the session's theme on re-entry. Same
        // null-tolerant semantics as showTerminal() - agent_type may be
        // missing in pre-Phase-6 builds; registry handles the fallback.
        var agentType = (session && session.agent_type)
            || (inner && inner.agent_type)
            || null;
        if (window.Themes && typeof window.Themes.applySession === 'function') {
            window.Themes.applySession(agentType);
        }

        // First-time init if the user never hit showTerminal() this page load
        // (e.g. refreshed directly onto launchpad while session was running).
        if (!window.TerminalController.term) {
            await window.TerminalController.init();
        }
        if (window.DPad && !window.DPad.floatingButton) {
            window.DPad.init();
        }
        if (window.DPad) {
            window.DPad.show();
        }
        if (window.SlashCommandsModal && !window.SlashCommandsModal.button) {
            await window.SlashCommandsModal.init((command) => {
                window.TerminalController.insertText(command);
            }, session && session.working_dir);
        }
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.show();
        }

        window.TerminalController.reconnectToExistingSession(session);
        this.focusTerminal();
    }

    /**
     * Hide all screens
     */
    hideAllScreens() {
        document.querySelectorAll('.screen').forEach(screen => {
            screen.classList.remove('active');
        });
    }

    /**
     * Logout
     */
    async logout() {
        // Show confirmation modal
        const confirmed = await this.showConfirmModal(
            'logout',
            'are you sure you want to logout?',
            'any active session will be destroyed.'
        );

        if (confirmed) {
            // Destroy active session if exists.
            //
            // `confirmedBy` is passed because the dialog above already
            // told the user "any active session will be destroyed" and
            // they said yes. Without it, destroySession() raises its own
            // confirm and the user is asked a second, differently worded
            // question about the destruction they just authorised.
            if (window.TerminalController.sessionActive) {
                try {
                    await window.TerminalController.destroySession(
                        null, { confirmedBy: 'App.logout' });
                } catch (error) {
                    console.error('App: Error destroying session during logout:', error);
                }
            }

            // Logout
            window.Auth.logout();
        }
    }

    /**
     * Show confirmation modal.
     *
     * This is the SINGLE confirmation-modal implementation for the whole
     * app - every destructive action (logout, delete session, delete
     * project, kill running session, reset server) routes through this
     * one function so there is exactly one modal to read, style, and
     * test. `LaunchpadController.showConfirmModal()` is a thin delegate
     * to this method, kept only so existing launchpad call sites don't
     * need to reach across modules.
     *
     * Description: builds a modal overlay with title/message/optional
     *   details and two buttons, and resolves once the user picks one
     *   or dismisses it. Escape, the cancel button, and a click on the
     *   overlay backdrop are all treated as cancel - every dismissal
     *   path resolves false, never true, so a destructive action can
     *   only fire from an explicit confirm click.
     * Inputs:
     *   title (string) - modal title, shown as "» <title>".
     *   message (string) - main message body.
     *   details (string|null) - optional secondary line (e.g. "this
     *     cannot be undone").
     *   primaryLabel (string) - label for the confirming button.
     *   secondaryLabel (string) - label for the cancelling button.
     * Output: Promise<boolean> - true only on an explicit confirm click.
     *
     * Security: title/message/details are attacker-reachable in some
     * callers (e.g. an interpolated session or project name that
     * ultimately traces back to hand-edited config or a tmux name) -
     * escaped here, once, at the shared sink, rather than trusting every
     * caller to pre-escape its own interpolated values.
     */
    showConfirmModal(title, message, details = null, primaryLabel = 'confirm', secondaryLabel = 'cancel') {
        return new Promise((resolve) => {
            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            const safeTitle = this._escapeHtml(title);
            const safeMessage = this._escapeHtml(message);
            const safeDetails = details ? this._escapeHtml(details) : null;

            // Create modal content
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» ${safeTitle}</div>
                    <div class="modal-body">
                        <div class="modal-message">${safeMessage}</div>
                        ${safeDetails ? `<div class="modal-description">${safeDetails}</div>` : ''}
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="modal-cancel">${this._escapeHtml(secondaryLabel)}</button>
                        <button class="modal-btn modal-btn-primary" id="modal-confirm">${this._escapeHtml(primaryLabel)}</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            // Handle Escape key
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    document.body.removeChild(overlay);
                    resolve(false);
                }
            });

            // Handle confirm button
            confirmBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(true);
            });

            // Handle cancel button
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(false);
            });

            // Handle click outside modal
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    document.body.removeChild(overlay);
                    resolve(false);
                }
            });

            // Focus confirm button
            setTimeout(() => confirmBtn.focus(), 100);
        });
    }

    /**
     * Update the address bar to reflect the session now on screen (the
     * outbound half of the deep-link feature - see router.js's
     * `enterSession()`/`buildSessionPath()` for the inbound half this
     * reuses).
     *
     * Description: `sessionName` is the canonical `tmux_session` value,
     *   which for a Cloude-owned session carries the `cloude_` prefix
     *   (`src/core/tmux_backend.py`'s `SESSION_PREFIX`) - but the
     *   deep-link resolver (`Launchpad.openProjectByName`) matches
     *   against the launcher's PROJECT name, which is always stored
     *   WITHOUT that prefix (see `Launchpad._handleAttachRunningSession`'s
     *   `cleanName` stripping when it auto-adds an adopted session to
     *   Recent Projects). Stripping here - via the launchpad's own
     *   `_deriveRunningSessionDisplayName()`, reused rather than
     *   re-implemented - is what makes a hard refresh on the resulting
     *   URL actually resolve back to the same project; skipping it would
     *   put `cloude_<name>` in the address bar, which
     *   `openProjectByName()` would never match against any project's
     *   bare `name` and the refresh would silently land on the launcher
     *   instead.
     * Inputs:
     *   sessionName (string|null) - tmux_session (or bare name) for the
     *     session now on screen. No-op if falsy.
     *   replace (boolean) - forwarded to Router.enterSession() as
     *     `{replace}` - see showTerminal()/returnToExistingTerminal()'s
     *     `cameFromTerminal` comment for the push-vs-replace reasoning.
     * Output: void.
     */
    _syncSessionUrl(sessionName, replace) {
        if (!sessionName || !window.Router || typeof window.Router.enterSession !== 'function') {
            return;
        }
        var slug = (window.Launchpad && typeof window.Launchpad._deriveRunningSessionDisplayName === 'function')
            ? window.Launchpad._deriveRunningSessionDisplayName(sessionName)
            : sessionName;
        window.Router.enterSession(slug, { replace: !!replace });
    }

    /**
     * Escape HTML special characters for safe interpolation into modal
     * markup built via innerHTML.
     * Inputs: str (any) - value to escape; stringified first.
     * Output: string - HTML-escaped text.
     */
    _escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }
}

// Create app instance
const App = new AppController();
// Expose on window so other modules (launchpad active-session banner,
// future deep-link targets) can call App.returnToExistingTerminal
// without re-wiring via custom events.
window.App = App;

// Initialize on load
window.addEventListener('load', () => {
    App.init();
    // Item 9: kick off deep-link router AFTER App.init() so the
    // auth state is already being resolved. router.js listens
    // for the `authenticated` event to deliver stashed targets.
    if (window.Router && typeof window.Router.init === 'function') {
        window.Router.init();
    }
});
