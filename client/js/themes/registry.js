/**
 * Themes Registry — client-side theme manifest store + apply pipeline.
 *
 * Phase 2 surface (per spec sections "Architecture B" / "Architecture G"):
 *   - Themes.init()                  fetch /api/v1/themes, apply persisted global
 *   - Themes.applyGlobal(id)         set <html data-theme>, write CSS vars on :root,
 *                                    fire xterm listeners, persist to localStorage
 *   - Themes.applySession(agentType) STUB — sets terminal-screen attr + console.log
 *                                    Phase 5 wires the full xterm side
 *   - Themes.clearSession()          remove session attr; restore global xterm
 *   - Themes.getActiveGlobal()       active global manifest
 *   - Themes.listAll()               all manifests (built-ins first)
 *   - Themes.onXtermThemeChange(cb)  subscribe to xterm palette changes
 *
 * Auth: fetches with Bearer token via window.Auth (matches the rest of the app).
 * Fallback: if /api/v1/themes fails for ANY reason, a hardcoded Claude manifest is
 *           injected so the page still renders.
 * No localStorage cache of the manifest list (DAR cut).
 * No-FOUC: callers should set <html data-theme="..."> from localStorage SYNCHRONOUSLY
 *          before init() resolves — see app.js for that early-paint hook.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'cloude.theme';
    var DEFAULT_THEME_ID = 'claude';
    // Phase 9: 3-state allowlist for theme effects.js scripts.
    // Shape: { [themeId]: true | false }. Missing key = "ask".
    var JS_ALLOWLIST_KEY = 'cloude.themeJsAllowlist';

    // Hardcoded fallback so the page survives a missing/broken endpoint. This
    // MUST stay in lock-step with client/css/themes/claude/theme.json — the
    // values come from the :root block in styles.css. If those drift, the
    // fallback drifts; on real boot the endpoint wins so this only matters
    // when /api/v1/themes is down.
    var CLAUDE_FALLBACK = {
        id: 'claude',
        name: 'Claude',
        description: 'The original coral-on-dark Cloude Code look.',
        author: 'Cloude Code',
        version: '1.0.0',
        source: 'builtin',
        cssVars: {},  // empty → no overrides → :root defaults from styles.css apply
        xterm: {
            background: '#1e1e1e',
            foreground: '#d4d4d4',
            cursor: '#d4d4d4',
            black: '#000000', red: '#cd3131', green: '#0dbc79', yellow: '#e5e510',
            blue: '#2472c8', magenta: '#bc3fbc', cyan: '#11a8cd', white: '#e5e5e5',
            brightBlack: '#666666', brightRed: '#f14c4c', brightGreen: '#23d18b',
            brightYellow: '#f5f543', brightBlue: '#3b8eea', brightMagenta: '#d670d6',
            brightCyan: '#29b8db', brightWhite: '#ffffff'
        }
    };

    // Module state
    var manifests = new Map();          // id -> ThemeManifest
    var activeGlobalId = DEFAULT_THEME_ID;
    var activeSessionAgent = null;
    // SESSION-IDENTITY-V2 — name of the currently-active session, set by
    // app.js when transitioning to/from the terminal screen. When non-null
    // applyGlobal() PATCHes the server-side pinned theme INSTEAD of writing
    // localStorage, so per-session pins survive reloads without polluting
    // the user's "default" theme.
    var activeSessionName = null;
    var xtermListeners = [];
    var initialized = false;
    var appliedCssVarNames = [];        // tracked so we can cleanly unset on :root swap
    // Phase 4-5: track which CSS var names are currently set INLINE on
    // #terminal-screen via applySession(). Cleared on clearSession() so
    // we don't leak orphaned vars across session swaps. Use a Set so the
    // keys are unique even if a theme accidentally lists a var twice.
    var sessionAppliedVarNames = new Set();
    // Phase 4-5: replay gate. While `replay_in_progress` is true (set by
    // the WS replay path elsewhere), applySession() calls are deferred
    // onto a queue and replayed once the flag clears. Prevents mid-replay
    // theme flicker when bytes are still being painted into xterm at
    // session-attach time. The flag itself is owned by the replay code;
    // Phase 4-5 only consumes it.
    var replayInProgress = false;
    var deferredSessionQueue = [];

    /**
     * Read the stored global theme id (sync, safe at any time).
     */
    function getStoredThemeId() {
        try {
            var v = localStorage.getItem(STORAGE_KEY);
            return v && typeof v === 'string' ? v : DEFAULT_THEME_ID;
        } catch (_) {
            return DEFAULT_THEME_ID;
        }
    }

    /**
     * Resolve auth header from window.Auth if available. Returns {} if not yet
     * initialized — the endpoint will then 401 and we fall back to the bundled
     * Claude manifest. Caller will retry post-auth via Themes.init().
     */
    function authHeaders() {
        var headers = { 'Accept': 'application/json' };
        try {
            if (window.Auth && typeof window.Auth.getAccessToken === 'function') {
                var tok = window.Auth.getAccessToken();
                if (tok) headers['Authorization'] = 'Bearer ' + tok;
            } else if (window.Auth && typeof window.Auth.getToken === 'function') {
                var tok2 = window.Auth.getToken();
                if (tok2) headers['Authorization'] = 'Bearer ' + tok2;
            }
        } catch (_) { /* fall through to no-auth */ }
        return headers;
    }

    /**
     * Fetch the manifest list from the server. Returns [] on any failure
     * (caller wires up the Claude fallback).
     */
    async function fetchManifests() {
        try {
            var res = await fetch('/api/v1/themes', {
                method: 'GET',
                headers: authHeaders(),
                cache: 'no-store',
                credentials: 'same-origin'
            });
            if (!res.ok) {
                console.warn('Themes: /api/v1/themes returned HTTP ' + res.status + ' — using fallback');
                return [];
            }
            var data = await res.json();
            if (!Array.isArray(data)) {
                console.warn('Themes: /api/v1/themes returned non-array — using fallback');
                return [];
            }
            return data;
        } catch (err) {
            console.warn('Themes: /api/v1/themes fetch failed — using fallback', err);
            return [];
        }
    }

    /**
     * Load manifests into the in-memory map. Falls back to CLAUDE_FALLBACK if
     * the endpoint produced nothing OR if it produced rows that don't include
     * a "claude" entry (we always need a baseline).
     */
    async function loadManifests() {
        manifests.clear();
        var rows = await fetchManifests();
        rows.forEach(function (m) {
            if (m && typeof m.id === 'string') manifests.set(m.id, m);
        });
        if (!manifests.has(DEFAULT_THEME_ID)) {
            manifests.set(DEFAULT_THEME_ID, CLAUDE_FALLBACK);
        }
    }

    /**
     * Apply a manifest's cssVars to :root. Tracks names applied so the next
     * apply() can unset stale ones cleanly (otherwise switching from a theme
     * that overrode --foo to one that doesn't would leave --foo orphaned).
     */
    function paintCssVars(cssVars) {
        var rootStyle = document.documentElement.style;
        // Unset previously-applied vars that the new theme doesn't define.
        var nextNames = Object.keys(cssVars || {});
        var nextSet = new Set(nextNames);
        appliedCssVarNames.forEach(function (name) {
            if (!nextSet.has(name)) rootStyle.removeProperty(name);
        });
        // Apply the new set.
        nextNames.forEach(function (name) {
            try { rootStyle.setProperty(name, cssVars[name]); } catch (_) { /* ignore bad vars */ }
        });
        appliedCssVarNames = nextNames;
    }

    function fireXtermChange(xtermPalette) {
        xtermListeners.forEach(function (cb) {
            try { cb(xtermPalette); } catch (e) { console.error('Themes: xterm listener threw', e); }
        });
    }

    // -----------------------------------------------------------------------
    // Phase 9 — theme effects.js loader + consent prompt
    //
    // User-authored effects.js is loaded same-origin per the LAN-only threat
    // model — see spec section "Context" (Architecture F: Pluggability
    // Surface) for the security reasoning. Bundled themes ALSO go through
    // this gate (belt-and-suspenders) so a malicious diff that ships a
    // bundled effects.js still requires explicit user consent on first run.
    //
    // 3-state localStorage allowlist (key: cloude.themeJsAllowlist):
    //   true  → load and run silently
    //   false → skip silently
    //   missing → prompt the user (Allow once / Always / Never)
    // -----------------------------------------------------------------------

    // Track scripts we've already injected so a re-applyGlobal() of the same
    // theme doesn't re-execute the FX (xterm listeners would double-fire).
    var loadedEffectsScripts = new Set();   // Set<themeId>
    // Currently-mounted effects module (the one whose init() has run and
    // whose destroy() must be called when we swap themes). Distinct from
    // loadedEffectsScripts (which is the in-flight + completed loader cache):
    // a module can be "loaded" but not "active" if its theme has been swapped
    // away. We need the module REFERENCE, not just the id, to call destroy().
    var activeEffectsModule = null;         // { destroy?: Function } | null
    var activeEffectsThemeId = null;        // themeId the active module belongs to
    // Coalesce concurrent prompts: if applyGlobal fires twice in a row before
    // the user clicks, don't stack two modals on top of each other.
    var pendingConsentForTheme = null;      // themeId currently awaiting click
    var consentResolveQueue = [];           // pending Promises for the same theme

    function readJsAllowlist() {
        try {
            var raw = localStorage.getItem(JS_ALLOWLIST_KEY);
            if (!raw) return {};
            var parsed = JSON.parse(raw);
            return (parsed && typeof parsed === 'object') ? parsed : {};
        } catch (_) {
            return {};
        }
    }

    function writeJsAllowlistEntry(themeId, value) {
        try {
            var current = readJsAllowlist();
            current[themeId] = !!value;
            localStorage.setItem(JS_ALLOWLIST_KEY, JSON.stringify(current));
        } catch (_) { /* localStorage full or disabled — non-fatal */ }
    }

    /**
     * Build + render the consent modal. Returns a Promise<'once'|'always'|'never'>.
     * Modal is theme-styled via existing .modal-* classes (uses --color-bg,
     * --color-accent, etc. from the currently-active theme so it never looks
     * out of place).
     *
     * Coalesces: if a prompt is already on-screen for the same themeId, the
     * caller piggybacks on it rather than stacking another modal.
     */
    function showConsentModal(manifest) {
        if (pendingConsentForTheme === manifest.id) {
            return new Promise(function (resolve) { consentResolveQueue.push(resolve); });
        }
        pendingConsentForTheme = manifest.id;

        return new Promise(function (resolve) {
            var overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.setAttribute('data-modal', 'theme-effects-consent');
            // Inline z-index bump in case another modal-overlay is mid-display.
            overlay.style.zIndex = '10001';

            // Use the same DOM shape as showConfirmModal in app.js so it picks
            // up theme styles automatically. Three buttons here instead of two.
            var safeName = String(manifest.name || manifest.id).replace(/[<>&"']/g, function (c) {
                return ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' })[c];
            });
            overlay.innerHTML = (
                '<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="theme-fx-title">' +
                '  <div class="modal-header" id="theme-fx-title">' +
                '    Theme effects script' +
                '  </div>' +
                '  <div class="modal-body">' +
                '    <div class="modal-message">' +
                '      Theme &ldquo;' + safeName + '&rdquo; ships a JavaScript module ' +
                '      (<code>effects.js</code>) that will run in this page.' +
                '    </div>' +
                '    <div class="modal-description">' +
                '      Allow it to run? This choice can be revoked by clearing ' +
                '      the <code>cloude.themeJsAllowlist</code> entry in localStorage.' +
                '    </div>' +
                '  </div>' +
                '  <div class="modal-footer">' +
                '    <button class="modal-btn modal-btn-secondary" data-action="never">Never</button>' +
                '    <button class="modal-btn modal-btn-secondary" data-action="once">Allow once</button>' +
                '    <button class="modal-btn modal-btn-primary" data-action="always">Always allow</button>' +
                '  </div>' +
                '</div>'
            );

            function finish(decision) {
                try { document.body.removeChild(overlay); } catch (_) {}
                pendingConsentForTheme = null;
                resolve(decision);
                // Drain any queued resolvers waiting on the same prompt.
                var pending = consentResolveQueue.slice();
                consentResolveQueue = [];
                pending.forEach(function (r) {
                    try { r(decision); } catch (_) {}
                });
            }

            overlay.addEventListener('click', function (e) {
                var btn = e.target.closest('button[data-action]');
                if (!btn) return;
                finish(btn.getAttribute('data-action'));
            });

            document.body.appendChild(overlay);
            // Default focus on the safest option ("Allow once" — no persistence).
            var onceBtn = overlay.querySelector('button[data-action="once"]');
            if (onceBtn) { try { onceBtn.focus(); } catch (_) {} }
        });
    }

    /**
     * Resolve the URL for a theme asset. Bundled themes live under the static
     * mount; user themes live under the /themes mount. The server stamps
     * `source` so this is unambiguous.
     */
    function effectsUrlFor(manifest) {
        var file = manifest.effects;
        if (!file || typeof file !== 'string') return null;
        // Defensive: forbid path traversal in a user-supplied filename.
        if (file.indexOf('..') !== -1 || file.indexOf('/') !== -1) {
            console.warn('Themes: rejecting effects path with traversal/slash:', file);
            return null;
        }
        var base = (manifest.source === 'user')
            ? '/themes/'
            : '/static/css/themes/';
        return base + encodeURIComponent(manifest.id) + '/' + encodeURIComponent(file);
    }

    /**
     * Load the effects module via dynamic import() and call its exported
     * init(). Same-origin so it inherits the existing `script-src 'self'`
     * CSP — no nonce needed.
     *
     * effects.js files are authored as ES modules (export function init()),
     * so a classic <script src=> tag would SyntaxError on `export` and
     * never run. dynamic import() loads them as modules AND gives us the
     * exported namespace so we can invoke init() ourselves — without it
     * the module would parse cleanly but do nothing (no top-level side
     * effects in our bundled FX files).
     *
     * Tracks loaded modules so re-applyGlobal() of the same theme is a
     * no-op (init() is idempotent itself, but we save a network round-trip).
     */
    function injectEffectsScript(manifest) {
        if (loadedEffectsScripts.has(manifest.id)) return;
        var url = effectsUrlFor(manifest);
        if (!url) return;
        loadedEffectsScripts.add(manifest.id);
        import(url).then(function (mod) {
            var initFn = (mod && typeof mod.init === 'function')
                ? mod.init
                : (mod && mod.default && typeof mod.default.init === 'function')
                    ? mod.default.init
                    : null;
            if (!initFn) {
                console.warn('Themes: effects module for', manifest.id, 'has no init() export');
                return;
            }
            try {
                initFn({ themeContext: { id: manifest.id, manifest: manifest } });
                // Race guard: by the time import() resolves, the user may have
                // already swapped to another theme. If this module's theme is
                // no longer active, destroy it immediately rather than letting
                // it leak (canvas + RAF would persist forever).
                if (activeGlobalId !== manifest.id) {
                    try {
                        if (mod && typeof mod.destroy === 'function') mod.destroy();
                        else if (mod && mod.default && typeof mod.default.destroy === 'function') mod.default.destroy();
                    } catch (e2) {
                        console.warn('Themes: stale effects destroy() threw for', manifest.id, e2);
                    }
                    loadedEffectsScripts.delete(manifest.id);
                    return;
                }
                // Resolve the destroyable handle (top-level export OR default).
                var destroyableMod = (mod && typeof mod.destroy === 'function')
                    ? mod
                    : (mod && mod.default && typeof mod.default.destroy === 'function')
                        ? mod.default
                        : null;
                activeEffectsModule = destroyableMod;
                activeEffectsThemeId = manifest.id;
                console.log('Themes: effects loaded for', manifest.id, '→', url);
            } catch (e) {
                console.warn('Themes: effects init() threw for', manifest.id, e);
            }
        }).catch(function (err) {
            // Roll back so a transient failure can be retried on next applyGlobal.
            loadedEffectsScripts.delete(manifest.id);
            console.warn('Themes: effects.js failed to load for', manifest.id, url, err);
        });
    }

    /**
     * Decide whether to load a manifest's effects.js, prompting the user on
     * first encounter. Returns a Promise<void>; never throws to caller — any
     * failure degrades to "skip the script" so the CSS theme still applies.
     */
    async function maybeLoadEffects(manifest) {
        if (!manifest || !manifest.effects) return;
        if (loadedEffectsScripts.has(manifest.id)) return;

        // Bundled themes ship with the app — they ARE our code, not third-party.
        // The consent prompt exists to gate user-authored themes dropped into
        // the /themes mount. Forcing users to click through a modal for a
        // theme we shipped in the repo is friction with no security upside
        // (an attacker who can ship a malicious bundled effects.js can also
        // ship a malicious registry.js). Per DAR + spec, bypass for builtins.
        if (manifest.source === 'builtin') {
            injectEffectsScript(manifest);
            return;
        }

        var allowlist = readJsAllowlist();
        var entry = allowlist[manifest.id];
        if (entry === true) {
            injectEffectsScript(manifest);
            return;
        }
        if (entry === false) {
            console.log('Themes: effects.js skipped per user allowlist for', manifest.id);
            return;
        }

        // Unknown — prompt.
        var decision;
        try {
            decision = await showConsentModal(manifest);
        } catch (e) {
            console.warn('Themes: consent modal failed, skipping effects', e);
            return;
        }
        if (decision === 'always') {
            writeJsAllowlistEntry(manifest.id, true);
            injectEffectsScript(manifest);
        } else if (decision === 'once') {
            // Don't persist. Inject this run only.
            injectEffectsScript(manifest);
        } else {
            // 'never' (or unknown — fail-closed)
            writeJsAllowlistEntry(manifest.id, false);
        }
    }

    /**
     * SESSION-IDENTITY-V2 — set / clear the active-session name. Called by
     * app.js on screen transitions (showTerminal sets, showLaunchpad/Auth
     * clears). When set, applyGlobal() routes persistence to the server
     * via PATCH /api/v1/sessions/<name>/pinned-theme INSTEAD of localStorage.
     */
    function setActiveSession(name) {
        activeSessionName = name || null;
    }

    /**
     * SESSION-IDENTITY-V2 — pure DOM/effects apply. No persistence side-effects.
     * Returns true on success, false if id is unknown. The shared paint
     * pipeline used by both the user-driven applyGlobal() (which then
     * persists) and the screen-transition restore path in app.js (which
     * MUST NOT persist — it's just re-painting whatever was already chosen).
     *
     * @param {string} themeId
     * @param {object} [opts]
     * @param {boolean} [opts.persist] — if true, write the choice to
     *   localStorage as the new global default. Server-side per-session
     *   pinning is handled by applyGlobal() which calls this internally;
     *   this primitive intentionally does NOT touch the server.
     */
    function applyTheme(themeId, opts) {
        var m = manifests.get(themeId);
        if (!m) {
            console.warn('Themes.applyTheme: unknown theme id ' + themeId + ' — keeping current');
            return false;
        }
        // Tear down the previously-active effects module if we're switching
        // AWAY from a theme that mounted one (e.g. Matrix → Claude). Without
        // this the matrix-rain canvas + RAF leak across the swap and only a
        // page refresh clears them. Skip when re-applying the same theme so
        // we don't destroy-then-init a still-good mount.
        if (activeEffectsModule && activeEffectsThemeId !== themeId) {
            try {
                if (typeof activeEffectsModule.destroy === 'function') {
                    activeEffectsModule.destroy();
                }
            } catch (e) {
                console.warn('Themes: previous effects destroy() threw for', activeEffectsThemeId, e);
            }
            // Drop loader cache for the outgoing theme so a future re-apply
            // re-fetches and re-inits cleanly (init() is idempotent but the
            // module-level state in effects.js needs a fresh init pass).
            loadedEffectsScripts.delete(activeEffectsThemeId);
            activeEffectsModule = null;
            activeEffectsThemeId = null;
        }
        document.documentElement.dataset.theme = themeId;
        paintCssVars(m.cssVars || {});
        activeGlobalId = themeId;

        if (opts && opts.persist === true) {
            try { localStorage.setItem(STORAGE_KEY, themeId); } catch (_) { /* ignore */ }
        }

        // Fire xterm listeners only when no session scope is active. When a
        // session theme IS active it owns the terminal palette; switching the
        // global shouldn't repaint it mid-session.
        if (!activeSessionAgent) {
            fireXtermChange(m.xterm || {});
        }

        // Phase 9: gated effects.js loader. Fire-and-forget so the call
        // stays sync from the caller's perspective; the modal/script-load
        // resolves asynchronously without blocking the CSS theme swap.
        if (m.effects) {
            maybeLoadEffects(m).catch(function (e) {
                console.warn('Themes: maybeLoadEffects rejected', e);
            });
        }
        return true;
    }

    /**
     * SESSION-IDENTITY-V2 — POST the pinned theme for an active session.
     * Best-effort: failures are logged but never throw to the caller — the
     * DOM paint already succeeded; persistence is recoverable. Server returns
     * the updated SessionInfo; we don't consume it (the local DOM is already
     * the source of truth for this paint).
     */
    function pinThemeForSession(sessionName, themeId) {
        try {
            var url = '/api/v1/sessions/' + encodeURIComponent(sessionName) + '/pinned-theme';
            var headers = authHeaders();
            headers['Content-Type'] = 'application/json';
            fetch(url, {
                method: 'PATCH',
                headers: headers,
                credentials: 'same-origin',
                body: JSON.stringify({ pinned_theme: themeId })
            }).then(function (res) {
                if (!res.ok) {
                    console.warn('Themes.pinThemeForSession: HTTP ' + res.status + ' for ' + sessionName);
                }
            }).catch(function (err) {
                console.warn('Themes.pinThemeForSession: network error', err);
            });
        } catch (err) {
            console.warn('Themes.pinThemeForSession: threw before fetch', err);
        }
    }

    /**
     * Apply a global theme. Returns true on success, false if id is unknown.
     *
     * Persistence is context-aware (SESSION-IDENTITY-V2):
     *   - activeSessionName SET    → PATCH the server-side pinned theme.
     *                                localStorage is NOT touched, so swapping
     *                                themes mid-session doesn't clobber the
     *                                user's "default" theme.
     *   - activeSessionName NULL   → write to localStorage cloude.theme as
     *                                the new global default, original behavior.
     *
     * Fires xtermThemeChange listeners with the manifest's xterm palette
     * UNLESS a session-scoped theme is active (then the session palette wins).
     */
    function applyGlobal(themeId) {
        var ok = applyTheme(themeId, { persist: !activeSessionName });
        if (!ok) return false;
        if (activeSessionName) {
            pinThemeForSession(activeSessionName, themeId);
        }
        return true;
    }

    /**
     * Apply a per-session theme scoped to #terminal-screen.
     *
     * Behavior (Phase 4-5):
     *   1. If `agentType` is null/undefined or doesn't match any manifest →
     *      delegate to clearSession() and return. "Unknown agent" is the
     *      same UX as "no agent": global theme governs the terminal too.
     *   2. Set #terminal-screen[data-session-theme=<id>] so the per-theme
     *      theme.css blocks (gradients, glows, scanlines) apply.
     *   3. Apply the matched manifest's cssVars INLINE on #terminal-screen
     *      via element.style.setProperty(). Inline > :root cascade so the
     *      session palette wins over the global without a specificity war
     *      and without !important. Track the set of names applied so
     *      clearSession() can cleanly reverse without leaking.
     *   4. Fire xtermThemeChange listeners with the matched manifest's
     *      xterm palette (terminal.js subscribes and assigns to term.options.theme).
     *
     * Replay gate: if replay_in_progress is true the call is queued onto
     * deferredSessionQueue and applied once setReplayInProgress(false) runs.
     * The most recent enqueued agentType wins (we coalesce intermediate
     * applySession() calls that landed mid-replay so we don't flicker
     * through multiple themes when the queue drains).
     */
    function applySession(agentType) {
        // Replay gate — defer until replay completes. Coalesce: only the
        // latest agentType matters when the queue drains.
        if (replayInProgress) {
            deferredSessionQueue.push(agentType);
            console.log('Themes.applySession: replay in progress, deferred', agentType);
            return;
        }

        // No agent or unknown agent → revert to global theme rules.
        if (!agentType) {
            clearSession();
            return;
        }
        var m = manifests.get(agentType);
        if (!m) {
            console.log('Themes.applySession: no manifest for "' + agentType + '" — falling back to global');
            clearSession();
            return;
        }

        var el = document.getElementById('terminal-screen');
        if (!el) {
            console.warn('Themes.applySession: #terminal-screen not found in DOM');
            return;
        }

        // 1. Mark the screen so per-theme theme.css blocks engage.
        el.dataset.sessionTheme = agentType;

        // 2. Sync inline cssVars: unset stale, apply current. Same diff-set
        //    pattern as paintCssVars() but scoped to the element instead
        //    of :root, so the global theme on documentElement is untouched.
        var nextVars = m.cssVars || {};
        var nextNames = Object.keys(nextVars);
        var nextSet = new Set(nextNames);
        sessionAppliedVarNames.forEach(function (name) {
            if (!nextSet.has(name)) {
                try { el.style.removeProperty(name); } catch (_) { /* ignore */ }
            }
        });
        nextNames.forEach(function (name) {
            try { el.style.setProperty(name, nextVars[name]); } catch (_) { /* ignore bad var */ }
        });
        sessionAppliedVarNames = nextSet;

        activeSessionAgent = agentType;
        console.log('Themes.applySession: ' + agentType + ' (' + nextNames.length + ' inline vars)');

        // 3. Push the session's xterm palette to subscribers.
        if (m.xterm) {
            fireXtermChange(m.xterm);
        }
    }

    /**
     * Remove the session scope. Strips the data attribute, removes every
     * inline cssVar that applySession() set, and reverts xterm to the
     * active global theme's palette.
     */
    function clearSession() {
        var el = document.getElementById('terminal-screen');
        if (el) {
            if (el.dataset.sessionTheme) delete el.dataset.sessionTheme;
            // Wipe every inline cssVar we put there. Track-and-remove
            // (vs. style.cssText = '') so we don't clobber any inline
            // styles that other code may legitimately set on the element.
            sessionAppliedVarNames.forEach(function (name) {
                try { el.style.removeProperty(name); } catch (_) { /* ignore */ }
            });
        }
        sessionAppliedVarNames = new Set();
        activeSessionAgent = null;
        var g = manifests.get(activeGlobalId);
        if (g && g.xterm) fireXtermChange(g.xterm);
    }

    /**
     * Replay-gate setter. Called by the WS replay path:
     *   setReplayInProgress(true)  before painting buffered scrollback
     *   setReplayInProgress(false) once the buffer is drained
     * On the trailing edge we drain the deferred applySession queue;
     * intermediate calls are coalesced (only the last enqueued agentType
     * is applied).
     */
    function setReplayInProgress(flag) {
        var was = replayInProgress;
        replayInProgress = !!flag;
        if (was && !replayInProgress && deferredSessionQueue.length) {
            var last = deferredSessionQueue[deferredSessionQueue.length - 1];
            deferredSessionQueue = [];
            console.log('Themes: replay finished — draining deferred session apply', last);
            applySession(last);
        }
    }

    function getActiveGlobal() {
        return manifests.get(activeGlobalId) || null;
    }

    /**
     * List all manifests, built-ins first then user themes. Each group sorted
     * by name (case-insensitive).
     */
    function listAll() {
        var rows = Array.from(manifests.values());
        rows.sort(function (a, b) {
            var aBuilt = (a.source === 'builtin') ? 0 : 1;
            var bBuilt = (b.source === 'builtin') ? 0 : 1;
            if (aBuilt !== bBuilt) return aBuilt - bBuilt;
            return (a.name || a.id).toLowerCase().localeCompare((b.name || b.id).toLowerCase());
        });
        return rows;
    }

    function onXtermThemeChange(cb) {
        if (typeof cb !== 'function') return function () {};
        xtermListeners.push(cb);
        return function () {
            var idx = xtermListeners.indexOf(cb);
            if (idx >= 0) xtermListeners.splice(idx, 1);
        };
    }

    /**
     * One-shot init — fetches manifests, applies the persisted global theme.
     * Idempotent: subsequent calls re-fetch (useful after auth) but never
     * double-apply listeners or break state.
     */
    async function init() {
        await loadManifests();
        var stored = getStoredThemeId();
        // If the stored id isn't in the manifest list (e.g. user uninstalled
        // a theme), fall back to claude.
        if (!manifests.has(stored)) stored = DEFAULT_THEME_ID;
        applyGlobal(stored);
        initialized = true;
        console.log('Themes: initialized — ' + manifests.size + ' manifest(s), active=' + activeGlobalId);
    }

    window.Themes = {
        init: init,
        applyGlobal: applyGlobal,
        applyTheme: applyTheme,
        setActiveSession: setActiveSession,
        applySession: applySession,
        clearSession: clearSession,
        setReplayInProgress: setReplayInProgress,
        getActiveGlobal: getActiveGlobal,
        listAll: listAll,
        onXtermThemeChange: onXtermThemeChange,
        // Expose constants for the selector + tests
        STORAGE_KEY: STORAGE_KEY,
        DEFAULT_THEME_ID: DEFAULT_THEME_ID,
        // Sync helper used by app.js to set <html data-theme> BEFORE init()
        // fetches anything — kills FOUC for repeat visitors.
        applyStoredThemeIdSync: function () {
            var id = getStoredThemeId();
            document.documentElement.dataset.theme = id;
            return id;
        },
        get initialized() { return initialized; }
    };
})();
