/**
 * Launchpad Module - Project selection UI with terminal aesthetic
 */

console.log('[Launchpad Module] Loading...');

class Launchpad {
    constructor() {
        this.launchpadScreen = null;
        this.projects = [];
        // Running tmux sessions on the `cloude` socket. Populated by
        // loadRunningSessions() — a merged view of:
        //   (a) the currently-active backend (from GET /sessions), and
        //   (b) attachable/external sessions (from GET /sessions/attachable).
        // Each row carries an is_active flag so the render pass can style
        // the live one differently without a second DOM query.
        this.runningSessions = [];
    }

    /**
     * Initialize launchpad screen
     */
    init() {
        this.launchpadScreen = document.getElementById('launchpad-screen');
        this.renderLaunchpadUI();
        // Wire the inline "+ new" speed-dial FAB. Markup was just injected
        // by renderLaunchpadUI() into the right side of the "recent
        // projects" section heading row; the 3 sub-actions route back
        // into the same handlers the old inline "new project" section used.
        this.setupNewFab();
        // Note: loadProjects() will be called by App.showLaunchpad()
        this._startRunningSessionsPoller();
    }

    /**
     * Wire the inline "+ new" speed-dial FAB.
     *
     * Markup is injected by renderLaunchpadUI() into the right side of
     * the "recent projects" section heading row (#new-fab). Three
     * sub-actions route into the same handlers the old inline "new
     * project" section used — no logic duplicated. Idempotent: safe to
     * call multiple times (guarded by a flag).
     *
     * Because the FAB lives inside #launchpad-screen, it shows/hides
     * naturally with the screen — no separate visibility plumbing
     * required from app.js.
     *
     * Behaviors:
     *   • Trigger click toggles .new-fab--open + aria-expanded
     *   • Backdrop click and ESC close the menu
     *   • Item click invokes the routed handler then closes
     */
    setupNewFab() {
        if (this._newFabWired) return;
        const fab = document.getElementById('new-fab');
        const trigger = document.getElementById('new-fab-trigger');
        const backdrop = document.getElementById('new-fab-backdrop');
        if (!fab || !trigger || !backdrop) {
            console.warn('Launchpad: new-fab markup missing — skipping wire');
            return;
        }

        // Map the FAB's data-action attrs onto our existing handlers.
        // Wrapped so `this` resolves correctly inside the dispatch table.
        const actions = {
            'new-project':  () => this.createNewSession(),
            'open-folder':  () => this.openProjectFromFolder(),
            'clone-github': () => this.showCloneFromGithubModal(),
        };

        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            this.toggleNewFab();
        });

        // Item dispatch via delegation — survives any future re-render
        fab.querySelectorAll('.new-fab__item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                const action = item.getAttribute('data-action');
                const fn = actions[action];
                if (typeof fn === 'function') {
                    this.closeNewFab();
                    // Defer the handler so the close animation gets a frame
                    // to start before any modal opens on top of it.
                    setTimeout(fn, 0);
                } else {
                    console.warn('Launchpad: unknown FAB action', action);
                    this.closeNewFab();
                }
            });
        });

        backdrop.addEventListener('click', () => this.closeNewFab());

        // ESC closes when open
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && fab.classList.contains('new-fab--open')) {
                this.closeNewFab();
            }
        });

        // Click-outside closes (ignore clicks inside the FAB itself)
        document.addEventListener('click', (e) => {
            if (!fab.classList.contains('new-fab--open')) return;
            if (fab.contains(e.target)) return;
            this.closeNewFab();
        });

        this._newFabWired = true;
        console.log('Launchpad: new-fab wired');
    }

    /**
     * Toggle the FAB open/closed (helper used by trigger click).
     */
    toggleNewFab() {
        const fab = document.getElementById('new-fab');
        if (!fab) return;
        if (fab.classList.contains('new-fab--open')) {
            this.closeNewFab();
        } else {
            this.openNewFab();
        }
    }

    /**
     * Open the FAB menu (idempotent).
     */
    openNewFab() {
        const fab = document.getElementById('new-fab');
        const trigger = document.getElementById('new-fab-trigger');
        const backdrop = document.getElementById('new-fab-backdrop');
        if (!fab || !trigger || !backdrop) return;
        fab.classList.add('new-fab--open');
        trigger.setAttribute('aria-expanded', 'true');
        backdrop.hidden = false;
        backdrop.setAttribute('data-open', '1');
        // Make menu items focusable when open
        fab.querySelectorAll('.new-fab__item').forEach(it => it.setAttribute('tabindex', '0'));
    }

    /**
     * Close the FAB menu (idempotent — also called from app.js when
     * the launchpad screen is being torn down).
     */
    closeNewFab() {
        const fab = document.getElementById('new-fab');
        const trigger = document.getElementById('new-fab-trigger');
        const backdrop = document.getElementById('new-fab-backdrop');
        if (!fab) return;
        fab.classList.remove('new-fab--open');
        if (trigger) trigger.setAttribute('aria-expanded', 'false');
        if (backdrop) {
            backdrop.removeAttribute('data-open');
            // Hide after the fade-out so it doesn't intercept clicks
            setTimeout(() => { backdrop.hidden = true; }, 200);
        }
        fab.querySelectorAll('.new-fab__item').forEach(it => it.setAttribute('tabindex', '-1'));
    }

    /**
     * Kick off a 5s interval that re-fetches the running-sessions list.
     *
     * Idempotent — guarded by ``this._runningPollInterval`` so repeated
     * calls (e.g. re-entering the launchpad after a session swap) don't
     * stack multiple intervals. Auth-gated per tick: skips the fetch
     * entirely when the user isn't logged in, so we don't hammer /sessions
     * with anonymous requests before the OTP flow completes.
     *
     * Runs forever; does not pause on tab hide — external tmux sessions
     * born while the tab is backgrounded should still surface the moment
     * the user returns.
     */
    _startRunningSessionsPoller() {
        if (this._runningPollInterval) return;
        this._runningPollInterval = setInterval(() => {
            if (!(window.Auth && typeof window.Auth.isAuthenticated === 'function' && window.Auth.isAuthenticated())) {
                return;
            }
            this.loadRunningSessions().catch(err => {
                console.warn('Launchpad: running-sessions poll tick failed:', err);
            });
        }, 5000);
        console.log('Launchpad: running-sessions poller started (5s)');
    }

    /**
     * Best-effort: get current xterm cell-grid dims from the live Terminal
     * instance so we can pass them to POST /sessions. Returns {} when the
     * terminal isn't ready yet (the server falls back to its own defaults
     * and the WS handshake reshapes shortly after anyway).
     */
    _getTerminalDims() {
        try {
            const t = window.TerminalController && window.TerminalController.term;
            if (t && typeof t.cols === 'number' && typeof t.rows === 'number'
                    && t.cols > 0 && t.rows > 0) {
                // Try to fit first so we hand over the dims the xterm.js
                // renderer will actually use post-connect.
                try {
                    if (window.TerminalController.fitAddon) {
                        window.TerminalController.fitAddon.fit();
                    }
                } catch (_) { /* non-fatal */ }
                return { cols: t.cols, rows: t.rows };
            }
        } catch (e) {
            console.warn('Launchpad: _getTerminalDims failed', e);
        }
        return {};
    }

    /**
     * Load and display projects, then refresh the running-sessions list.
     * Both fetches are non-fatal — the projects error path shows a UI
     * error, the sessions path is logged and silently renders empty.
     */
    async loadProjects() {
        try {
            this.projects = await window.API.getProjects();
            this.renderProjectList();
        } catch (error) {
            console.error('Launchpad: Failed to load projects:', error);
            this.showError('failed to load projects: ' + error.message);
        }
        // Refresh running sessions in parallel with the projects view.
        // Failure is non-fatal and handled inside loadRunningSessions.
        this.loadRunningSessions();
    }

    /**
     * Fetch the unified "running sessions" list and repaint the section.
     *
     * Combines two server endpoints:
     *   - ``GET /sessions/attachable`` — external tmux sessions on the
     *     cloude socket, plus cloude-owned sessions NOT currently bound
     *     to an active backend (detached-but-alive).
     *   - ``GET /sessions`` — the currently-active backend, if any. The
     *     server's /attachable filter drops this row to prevent a
     *     self-adopt footgun, so we refetch and merge it in here.
     *
     * Each merged row gains an ``is_active`` flag and the list is sorted:
     * active first, then owned (cloude-created), then external; within
     * each bucket, newest first by ``created_at_epoch``.
     */
    async loadRunningSessions() {
        try {
            const list = await window.API.listAttachableSessions();
            this.runningSessions = Array.isArray(list) ? list : [];
        } catch (err) {
            // Surface the failure loud + observable in DevTools instead of
            // swallowing it — a silent catch here was masking 401s / CORS /
            // stale-cache bugs where mobile browsers would render an empty
            // section with zero diagnostic trail. Keep the [] fallback so
            // the rest of the render pipeline stays stable.
            //
            // Status extraction: the `call()` wrapper in api.js throws
            // Error("HTTP <code>") for non-401s and Error("Authentication
            // required...") for 401s after refresh fails. Parse what we
            // can from the message so the log line is actionable.
            let status = null;
            if (err && typeof err.status === 'number') {
                status = err.status;
            } else if (err && typeof err.message === 'string') {
                const m = err.message.match(/HTTP\s+(\d{3})/);
                if (m) status = parseInt(m[1], 10);
                else if (/Authentication required/i.test(err.message)) status = 401;
            }
            console.error(
                '[launchpad] loadRunningSessions failed:',
                status !== null ? `status=${status}` : '(no status)',
                err
            );
            // On 401, fire a reauth event for the auth layer to pick up.
            // NOTE: api.js:call() already dispatches `auth-required` on 401
            // after refresh fails, so this is defense-in-depth only. If
            // Auth.js doesn't listen for `cloude:reauth-needed` that's fine
            // — `auth-required` remains the primary signal.
            if (status === 401) {
                try {
                    window.dispatchEvent(new CustomEvent('cloude:reauth-needed', {
                        detail: { source: 'launchpad.loadRunningSessions' }
                    }));
                } catch (_) { /* non-fatal */ }
            }
            this.runningSessions = [];
        }
        // Augment with the CURRENTLY ACTIVE backend, which the server filters
        // out of /sessions/attachable to prevent self-adopt. Refetch via GET
        // /sessions (returns 404 when none active) and merge.
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
                    const row = this.runningSessions.find(s => s.name === current.tmux_session);
                    if (row) row.is_active = true;
                }
            }
        } catch (err) {
            // 404 = no active session, fine
        }
        // Sort: active first, then owned, then external; within each, newest first
        this.runningSessions.sort((a, b) => {
            if (!!a.is_active !== !!b.is_active) return a.is_active ? -1 : 1;
            if (!!a.created_by_cloude !== !!b.created_by_cloude) {
                return a.created_by_cloude ? -1 : 1;
            }
            return (b.created_at_epoch || 0) - (a.created_at_epoch || 0);
        });
        this.renderRunningSessions();
    }

    /**
     * Paint (or hide) the Running Sessions section. Hides via display:none
     * when empty — opacity:0 would still capture clicks, which we don't want.
     *
     * Click handlers (row → return/adopt, X → kill) land in Task 10; this
     * pass only builds the DOM. ``data-name`` / ``data-active`` attributes
     * are the hooks event delegation will use.
     */
    renderRunningSessions() {
        const container = document.getElementById('running-sessions-list');
        if (!container) return;
        const section = document.getElementById('running-sessions-section');
        if (!this.runningSessions || this.runningSessions.length === 0) {
            // Only rewrite the DOM when transitioning into the empty state —
            // repeated renders while already empty would thrash the
            // section's display flip for no reason.
            if (this._lastRunningSig !== 'empty') {
                this._lastRunningSig = 'empty';
                if (section) section.style.display = 'none';
                container.innerHTML = '';
            }
            return;
        }
        // Signature-diff: skip the innerHTML rewrite when the set of rows
        // (name + ownership + active flag) hasn't changed. Previously the
        // 5s poller was restarting the `.running-session-row` pulse-glow
        // CSS animations every tick, which visibly flickered. Age labels
        // still need updating each tick, so we punt those through a
        // cheap text-only DOM update instead.
        const sig = JSON.stringify(this.runningSessions.map(s => ({
            name: s.name,
            owned: !!s.created_by_cloude,
            active: !!s.is_active,
        })));
        if (sig === this._lastRunningSig) {
            this._updateRunningSessionAges();
            return;
        }
        this._lastRunningSig = sig;
        if (section) section.style.display = '';
        container.innerHTML = this.runningSessions.map(s => {
            const owned = !!s.created_by_cloude;
            const displayName = this._deriveRunningSessionDisplayName(s.name);
            const ageStr = s.created_at_epoch ? this._formatRelativeTime(s.created_at_epoch) : '';
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
        // Idempotent — re-calling after subsequent renders is a no-op
        // because the listener is bound to the (stable) container element,
        // not the (re-painted) row children, and the flag gates re-bind.
        this._bindRunningSessionClicks();
    }

    /**
     * Text-only age refresh — walks existing rows and rewrites just the
     * ``.running-session-age`` textContent. Used on poll ticks when the
     * row set is unchanged so we avoid the innerHTML rewrite that would
     * restart the pulse-glow CSS animations.
     *
     * Guarded for all the obvious missing-data cases: row without a
     * data-name, session no longer in the list, session without an
     * epoch, row without an age element. Any miss is a silent skip —
     * the next full render will reconcile.
     */
    _updateRunningSessionAges() {
        const rows = document.querySelectorAll('#running-sessions-list .running-session-row');
        rows.forEach(row => {
            const name = row.dataset.name;
            if (!name) return;
            const s = this.runningSessions.find(x => x.name === name);
            if (!s || !s.created_at_epoch) return;
            const ageEl = row.querySelector('.running-session-age');
            if (!ageEl) return;
            ageEl.textContent = this._formatRelativeTime(s.created_at_epoch);
        });
    }

    /**
     * Strip the ``cloude_`` prefix from tmux session names for display.
     * Non-cloude (external) names are rendered verbatim.
     */
    _deriveRunningSessionDisplayName(tmuxName) {
        if (tmuxName && tmuxName.startsWith('cloude_')) {
            return tmuxName.slice('cloude_'.length);
        }
        return tmuxName;
    }

    /**
     * Best-effort read of the currently-active backend's tmux session name.
     * Used for the self-adopt UI filter and the session-collision modal copy.
     * Returns null when no session is active or the controller isn't wired
     * up yet.
     */
    _getActiveSessionName() {
        try {
            const t = window.TerminalController;
            if (t && t.sessionActive && t._currentSession && t._currentSession.tmux_session) {
                return t._currentSession.tmux_session;
            }
        } catch (_) { /* non-fatal */ }
        return null;
    }

    /**
     * HTML-escape helper. Session names come from the tmux daemon and are
     * technically user-controlled — any embedded `<`, `>`, `"`, `'`, `&`
     * in a name would break innerHTML.
     */
    _escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Format "N seconds / minutes / hours / days ago" for a unix epoch
     * timestamp. Mirrors standard UX copy for session-age display.
     */
    _formatRelativeTime(epochSeconds) {
        if (!epochSeconds || typeof epochSeconds !== 'number') return 'unknown';
        const delta = Math.max(0, Math.floor(Date.now() / 1000) - epochSeconds);
        if (delta < 60) return `${delta}s ago`;
        if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
        if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
        return `${Math.floor(delta / 86400)}d ago`;
    }

    /**
     * Bind a single delegated click listener on #running-sessions-list.
     *
     * Event delegation over per-row listeners: avoids re-binding on every
     * render and survives DOM swaps. The `__boundRunningClicks` flag is
     * a one-shot idempotence guard — re-calling from renderRunningSessions
     * is a no-op after the first paint.
     *
     * Click target disambiguation:
     *   - `.running-session-kill` (or its SVG child) → kill flow
     *   - anywhere else on `.running-session-row`   → return/swap flow
     *
     * stopPropagation on the kill branch is the important bit — without it
     * the row handler would also fire and we'd race a swap against a
     * destroy.
     */
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
                try {
                    const current = await window.API.getCurrentSession();
                    if (current) {
                        window.App.returnToExistingTerminal(current);
                    }
                } catch (err) {
                    this.showError('failed to return to terminal: ' + (err.message || err));
                }
                return;
            }
            // Different session → swap
            await this._handleAttachRunningSession(name);
        });
        container.__boundRunningClicks = true;
    }

    /**
     * Kill flow for a running tmux session.
     *
     * Two paths:
     *   1. Target IS the currently-active backend → DELETE /sessions
     *      (full destroy: tears down backend, idle watcher, tunnels,
     *      metadata; then kills tmux).
     *   2. Target is a different session → DELETE /sessions/external/{name}
     *      (direct `tmux kill-session`, no adoption). This used to be
     *      adopt-then-destroy, but adoption refuses dead panes (raised
     *      `RuntimeError("pane already dead")` in tmux_backend.attach_existing,
     *      surfacing as HTTP 500 from POST /sessions/adopt) — leaving any
     *      session whose foreground process exited permanently un-killable
     *      from the UI. The dedicated external-destroy endpoint sidesteps
     *      adoption entirely and is also idempotent if the session is
     *      already gone server-side.
     */
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
            const current = await window.API.getCurrentSession().catch(() => null);
            if (current && current.tmux_session === tmuxName) {
                await window.API.destroySession();
            } else {
                await window.API.destroyExternalSession(tmuxName);
            }
        } catch (err) {
            this.showError(`destroy failed: ${err.message || err}`);
        }
        await this.loadRunningSessions();
    }

    /**
     * Attach/swap flow for a running tmux session (non-active row click).
     *
     * Swap immediately — the prior session is detached (not killed) so it
     * stays alive in tmux and reappears in the running-sessions list. On
     * adopt success, dispatch `session-created` with the full adopt-specific
     * detail payload (initialScrollbackB64, fifoStartOffset, adopted:true)
     * so App.showTerminal() can plumb scrollback into the terminal
     * controller.
     */
    async _handleAttachRunningSession(tmuxName) {
        try {
            const response = await window.API.adoptSession(tmuxName, true);
            const session = response.session || response;
            const initialScrollbackB64 = response.initial_scrollback_b64 || '';
            const fifoStartOffset = typeof response.fifo_start_offset === 'number'
                ? response.fifo_start_offset
                : null;
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session, initialScrollbackB64, fifoStartOffset, adopted: true }
            }));
        } catch (err) {
            this.showError(`attach failed: ${err.message || err}`);
        }
    }

    /**
     * Render launchpad UI structure
     */
    renderLaunchpadUI() {
        this.launchpadScreen.innerHTML = `
            <div class="launchpad-container">
                <div class="launchpad-header">☁️ Cloude Code Launcher</div>
                <div class="launchpad-prompt">select a project or create a new project</div>

                <div id="running-sessions-section" class="launchpad-section running-sessions-section" style="display:none;">
                    <div class="launchpad-section-title">
                        ► running sessions
                        <details class="adopt-disclosure">
                            <summary>?</summary>
                            <div class="adopt-disclosure-body">
                                <p>Sessions shown here run on the <code>cloude</code> tmux socket. Start one externally with <code>tmux -L cloude new -s &lt;name&gt;</code> — it'll appear here.</p>
                                <p>To launch claude in one line:</p>
                                <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "claude --dangerously-skip-permissions; exec \$SHELL"</code></pre>
                                <p>The <code>exec \$SHELL</code> trick keeps the pane alive with a shell prompt after claude exits.</p>
                                <p>If you have a custom launcher alias (e.g. <code>cld</code>) defined in your <code>~/.zshrc</code> or <code>~/.bashrc</code>, wrap the inner command in an interactive shell:</p>
                                <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "\$SHELL -ic 'cld; exec \$SHELL'"</code></pre>
                                <p>Full setup in the <a href="https://github.com/Adoom666/CloudeCode#launching-claude-with-a-custom-alias" target="_blank" rel="noopener">README</a>.</p>
                            </div>
                        </details>
                    </div>
                    <div id="running-sessions-list"></div>
                </div>

                <!-- "new project" actions live in the inline speed-dial FAB
                     to the right of the "recent projects" heading. The FAB
                     trigger sits as a sibling of the heading text inside a
                     flex row (justify-content: space-between). The fan-out
                     menu anchors absolutely off the .new-fab wrapper.
                     Wired in setupNewFab(). -->

                <div class="launchpad-section" id="projects-section">
                    <div class="launchpad-section-title launchpad-section-title--row">
                        <span class="launchpad-section-title__text">► recent projects</span>
                        <div class="new-fab" id="new-fab">
                            <button class="new-fab__trigger" id="new-fab-trigger" type="button" aria-label="New" aria-haspopup="menu" aria-expanded="false">
                                <svg class="new-fab__plus" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
                                    <line x1="12" y1="5" x2="12" y2="19"/>
                                    <line x1="5" y1="12" x2="19" y2="12"/>
                                </svg>
                            </button>
                            <div class="new-fab__menu" role="menu" aria-label="New session actions">
                                <button class="new-fab__item" type="button" role="menuitem" data-action="new-project" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <path d="M13 2L3 12l9 9 10-10V2z"/>
                                            <circle cx="8.5" cy="7.5" r="1.2" fill="currentColor" stroke="none"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">create new project</span>
                                </button>
                                <button class="new-fab__item" type="button" role="menuitem" data-action="open-folder" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">open from folder</span>
                                </button>
                                <button class="new-fab__item" type="button" role="menuitem" data-action="clone-github" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.04 1.53 1.04.9 1.52 2.34 1.08 2.91.83.09-.65.35-1.08.63-1.33-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.6 9.6 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.69-4.57 4.93.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10 10 0 0 0 12 2z"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">clone from github</span>
                                </button>
                            </div>
                        </div>
                    </div>
                    <div id="project-list" class="project-list">
                        <div class="launchpad-empty">loading projects...</div>
                    </div>
                </div>

                <div class="launchpad-section">
                    <div class="launchpad-section-title">► server management</div>
                    <button class="reset-server-btn" id="reset-server-btn">
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                            <path d="M13 8C13 10.7614 10.7614 13 8 13C5.23858 13 3 10.7614 3 8C3 5.23858 5.23858 3 8 3C9.87677 3 11.5 4.01207 12.3284 5.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                            <path d="M12 2.5V5.5H9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <span>reset server</span>
                    </button>
                </div>

                <div class="launchpad-footer">
                    <a href="https://nyedis.ai" target="_blank" rel="noopener noreferrer">
                        <svg version="1.1" xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 986 937" role="img" aria-label="Black bird silhouette">
                            <path d="M 409.0 883.5 L 408.5 882.0 L 458.5 804.0 L 489.5 748.0 L 488.0 747.5 L 453.0 783.5 L 437.0 797.5 L 403.0 823.5 L 377.0 839.5 L 376.5 838.0 L 398.5 816.0 L 438.5 771.0 L 469.5 732.0 L 478.5 718.0 L 474.0 719.5 L 436.0 750.5 L 388.0 785.5 L 394.5 766.0 L 409.5 739.0 L 408.0 738.5 L 386.0 753.5 L 377.0 758.5 L 375.5 758.0 L 382.5 743.0 L 394.5 725.0 L 410.5 705.0 L 410.5 703.0 L 374.0 704.5 L 361.0 702.5 L 360.5 701.0 L 409.0 681.5 L 481.0 647.5 L 520.0 625.5 L 546.0 607.5 L 565.5 589.0 L 570.5 580.0 L 570.5 576.0 L 561.0 575.5 L 542.0 580.5 L 545.5 574.0 L 560.5 556.0 L 594.0 522.5 L 632.5 489.0 L 630.0 487.5 L 588.0 488.5 L 551.0 493.5 L 516.0 500.5 L 529.5 487.0 L 532.5 480.0 L 532.0 473.5 L 515.0 472.5 L 491.0 468.5 L 451.0 455.5 L 435.5 448.0 L 452.0 439.5 L 456.5 435.0 L 456.0 433.5 L 420.0 426.5 L 402.0 420.5 L 394.5 416.0 L 427.0 414.5 L 442.0 411.5 L 445.0 410.5 L 445.0 408.5 L 399.0 408.5 L 375.0 406.5 L 333.0 400.5 L 305.5 393.0 L 306.0 391.5 L 309.0 391.5 L 344.0 394.5 L 429.0 395.5 L 461.0 394.5 L 461.0 392.5 L 426.0 390.5 L 378.0 384.5 L 302.0 370.5 L 249.0 358.5 L 180.0 339.5 L 138.0 331.5 L 75.0 314.5 L 34.0 299.5 L 19.0 291.5 L 15.5 287.0 L 18.0 285.5 L 173.5 287.0 L 173.0 285.5 L 125.0 275.5 L 91.0 264.5 L 68.0 252.5 L 59.5 244.0 L 59.0 238.5 L 134.0 251.5 L 227.5 271.0 L 225.5 266.0 L 218.0 259.5 L 181.5 238.0 L 185.0 237.5 L 297.0 264.5 L 434.0 294.5 L 546.0 316.5 L 613.0 326.5 L 613.5 325.0 L 607.0 320.5 L 591.0 312.5 L 561.0 301.5 L 509.0 287.5 L 450.0 276.5 L 449.5 275.0 L 483.0 262.5 L 505.0 257.5 L 534.0 253.5 L 600.0 252.5 L 625.0 255.5 L 632.5 255.0 L 622.0 245.5 L 609.0 239.5 L 588.0 233.5 L 551.5 228.0 L 568.0 220.5 L 585.0 217.5 L 612.0 217.5 L 644.0 221.5 L 692.0 232.5 L 737.0 247.5 L 741.0 247.5 L 747.0 241.5 L 754.0 237.5 L 771.0 233.5 L 797.0 235.5 L 814.0 240.5 L 827.0 246.5 L 841.5 259.0 L 844.5 265.0 L 845.5 281.0 L 843.5 288.0 L 836.5 301.0 L 824.5 316.0 L 805.0 334.5 L 782.0 351.5 L 769.5 365.0 L 761.5 379.0 L 761.5 390.0 L 765.0 393.5 L 767.0 393.5 L 778.0 388.5 L 795.0 383.5 L 807.0 381.5 L 827.0 381.5 L 852.0 387.5 L 865.0 393.5 L 879.0 402.5 L 904.5 426.0 L 925.5 454.0 L 946.5 492.0 L 957.5 518.0 L 961.5 532.0 L 944.0 513.5 L 932.0 503.5 L 923.0 497.5 L 903.0 488.5 L 889.0 485.5 L 873.0 485.5 L 853.0 490.5 L 839.0 497.5 L 829.0 504.5 L 814.5 519.0 L 783.5 561.0 L 754.5 604.0 L 737.5 635.0 L 737.5 659.0 L 740.0 661.5 L 765.0 671.5 L 816.0 696.5 L 826.0 699.5 L 843.0 709.5 L 857.0 720.5 L 879.5 743.0 L 894.5 762.0 L 895.5 768.0 L 881.0 780.5 L 879.5 771.0 L 875.5 764.0 L 870.0 758.5 L 856.5 750.0 L 847.5 731.0 L 834.0 716.5 L 821.0 708.5 L 808.0 704.5 L 799.0 704.5 L 800.0 700.5 L 780.0 689.5 L 722.0 666.5 L 716.5 662.0 L 715.5 650.0 L 710.0 643.5 L 705.0 641.5 L 688.0 641.5 L 683.5 644.0 L 682.5 651.0 L 689.0 666.5 L 752.0 692.5 L 793.0 711.5 L 808.0 722.5 L 824.5 738.0 L 834.5 751.0 L 840.5 762.0 L 838.5 766.0 L 828.0 772.5 L 816.0 774.5 L 815.5 762.0 L 811.5 753.0 L 805.5 744.0 L 794.0 732.5 L 786.0 729.5 L 777.0 728.5 L 765.0 729.5 L 764.5 728.0 L 768.0 724.5 L 774.0 722.5 L 774.5 721.0 L 767.0 719.5 L 734.0 699.5 L 701.0 684.5 L 681.0 679.5 L 670.0 679.5 L 668.5 671.0 L 664.0 665.5 L 657.0 663.5 L 651.0 664.5 L 646.5 669.0 L 643.5 677.0 L 645.5 705.0 L 644.5 753.0 L 643.5 756.0 L 641.0 756.5 L 637.5 748.0 L 633.0 742.5 L 627.0 739.5 L 620.5 740.0 L 623.5 754.0 L 623.5 772.0 L 620.5 790.0 L 616.0 803.5 L 614.5 795.0 L 611.0 789.5 L 603.0 783.5 L 595.0 782.5 L 593.5 798.0 L 589.5 812.0 L 581.5 826.0 L 567.0 840.5 L 564.5 841.0 L 566.5 830.0 L 566.5 816.0 L 565.5 807.0 L 564.0 806.5 L 549.5 828.0 L 532.0 845.5 L 514.0 858.5 L 512.5 858.0 L 519.5 849.0 L 523.5 840.0 L 526.5 828.0 L 526.0 823.5 L 503.0 844.5 L 479.0 860.5 L 487.5 844.0 L 495.5 819.0 L 501.5 788.0 L 500.0 786.5 L 461.5 835.0 L 427.0 869.5 L 409.0 883.5 Z" fill="currentColor"/>
                        </svg>
                    </a>
                </div>
            </div>
        `;

        // Event listeners
        // Note: the 3 "new project" actions (create / open-folder / clone-github)
        // are wired in setupNewFab() — the inline speed-dial sits to the right
        // of the "recent projects" section heading on the launchpad screen.

        document.getElementById('reset-server-btn').addEventListener('click', () => {
            this.resetServer();
        });

        // Note: loadProjects() will be called by App.showLaunchpad().
        // Running-sessions row/X click handlers land in Task 10 via event
        // delegation on #running-sessions-list.
    }

    /**
     * Render project list
     */
    renderProjectList() {
        const projectListEl = document.getElementById('project-list');

        if (this.projects.length === 0) {
            projectListEl.innerHTML = `
                <div class="launchpad-empty">
                    no projects configured yet<br>
                    <small style="color: #666;">edit config.json to add projects</small>
                </div>
            `;
            return;
        }

        // Render projects
        projectListEl.innerHTML = this.projects.map((project, index) => {
            const description = project.description || 'no description';
            return `
                <div class="project-item" data-index="${index}" data-name="${project.name}">
                    <button class="project-edit-btn" data-name="${project.name}" title="Edit project">✎</button>
                    <button class="project-delete-btn" data-name="${project.name}" title="Delete project">×</button>
                    <div class="project-name">» ${project.name}</div>
                    <div class="project-path">${project.path}</div>
                    <div class="project-description">${description}</div>
                </div>
            `;
        }).join('');

        // Add click handlers for project selection
        const projectItems = projectListEl.querySelectorAll('.project-item');
        projectItems.forEach(item => {
            item.addEventListener('click', (e) => {
                // Don't open project if clicking an inline action button
                if (e.target.classList.contains('project-delete-btn') ||
                    e.target.classList.contains('project-edit-btn')) {
                    return;
                }
                const index = parseInt(item.dataset.index);
                this.selectProject(this.projects[index]);
            });
        });

        // Add click handlers for delete buttons
        const deleteButtons = projectListEl.querySelectorAll('.project-delete-btn');
        deleteButtons.forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation(); // Prevent project selection
                const projectName = btn.dataset.name;
                await this.deleteProject(projectName);
            });
        });

        // Add click handlers for edit buttons
        const editButtons = projectListEl.querySelectorAll('.project-edit-btn');
        editButtons.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation(); // Prevent project selection
                const projectName = btn.dataset.name;
                const project = this.projects.find(p => p.name === projectName);
                if (project) {
                    this.editProject(project);
                }
            });
        });
    }

    /**
     * Delete a project
     */
    async deleteProject(projectName) {
        try {
            // Show confirmation modal
            const confirmed = await this.showConfirmModal(
                'delete project',
                `are you sure you want to delete "${projectName}"?`,
                'this will only remove it from the launcher. the actual files will not be deleted.'
            );

            if (!confirmed) {
                return;
            }

            // Show loading state
            this.updateStatus(`deleting ${projectName}...`);

            // Delete project via API
            await window.API.deleteProject(projectName);

            console.log('Launchpad: Project deleted:', projectName);

            // Reload projects list
            await this.loadProjects();

            this.updateStatus('project deleted');

        } catch (error) {
            console.error('Launchpad: Failed to delete project:', error);
            this.showError('failed to delete project: ' + error.message);
        }
    }

    /**
     * Open the edit-project modal for ``project`` and persist any changes.
     *
     * Display name only — the folder on disk is never touched.
     */
    async editProject(project) {
        try {
            const result = await this.showEditProjectModal(project);
            if (!result) {
                return; // user cancelled
            }

            const { name: newName, description: newDescription } = result;
            const nameChanged = newName !== project.name;
            const descChanged = (newDescription || '') !== (project.description || '');

            if (!nameChanged && !descChanged) {
                return; // nothing to do
            }

            this.updateStatus(`updating ${project.name}...`);

            const fields = {};
            if (nameChanged) fields.newName = newName;
            if (descChanged) fields.description = newDescription;

            await window.API.updateProject(project.name, fields);

            console.log('Launchpad: Project updated:', project.name, '→', newName);

            // Refresh the list so the row reflects the new label
            await this.loadProjects();

            this.updateStatus('project updated');
        } catch (error) {
            console.error('Launchpad: Failed to update project:', error);
            this.showError('failed to update project: ' + error.message);
        }
    }

    /**
     * Show the edit-project modal pre-filled with the current name and
     * description. Resolves with ``{name, description}`` on save, or
     * ``null`` on cancel/escape/click-outside.
     *
     * Inline 409 conflicts are reported via ``API.updateProject`` rejecting
     * with an error whose ``message`` contains "already exists" — handled
     * by ``editProject`` via ``showError``.
     */
    showEditProjectModal(project) {
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            const escapeHtml = (s) => String(s)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');

            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» edit project</div>
                    <div class="modal-body">
                        <div class="modal-input-group">
                            <div class="modal-label">folder</div>
                            <div class="folder-picker-path">${escapeHtml(project.path)}</div>
                            <div class="modal-description">
                                the folder on disk is never renamed — only the launcher label changes.
                            </div>
                        </div>
                        <div class="modal-input-group">
                            <label class="modal-label">project name</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="edit-project-name"
                                value="${escapeHtml(project.name)}"
                                autocomplete="off"
                            />
                        </div>
                        <div class="modal-input-group">
                            <label class="modal-label">description (optional)</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="edit-project-description"
                                placeholder="e.g., Building an AI-powered chatbot"
                                value="${escapeHtml(project.description || '')}"
                                autocomplete="off"
                            />
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="edit-modal-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="edit-modal-save">save</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const nameInput = overlay.querySelector('#edit-project-name');
            const descInput = overlay.querySelector('#edit-project-description');
            const saveBtn = overlay.querySelector('#edit-modal-save');
            const cancelBtn = overlay.querySelector('#edit-modal-cancel');

            // Focus name input and select existing content
            setTimeout(() => {
                nameInput.focus();
                nameInput.select();
            }, 100);

            const submit = () => {
                const name = nameInput.value.trim();
                if (!name) {
                    nameInput.focus();
                    return;
                }
                const description = descInput.value.trim();
                document.body.removeChild(overlay);
                resolve({ name, description });
            };

            const cancel = () => {
                document.body.removeChild(overlay);
                resolve(null);
            };

            // Enter on name → move to description; Enter on description → submit
            nameInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (nameInput.value.trim()) {
                        descInput.focus();
                    }
                }
            });
            descInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    submit();
                }
            });

            // Escape cancels
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    cancel();
                }
            });

            saveBtn.addEventListener('click', submit);
            cancelBtn.addEventListener('click', cancel);

            // Click outside cancels
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    cancel();
                }
            });
        });
    }

    /**
     * Reset the server
     */
    async resetServer() {
        try {
            // Show confirmation modal
            const confirmed = await this.showConfirmModal(
                'reset server',
                'are you sure you want to reset the server?',
                'the python server will restart and re-attach your tmux sessions — sessions keep running, only the web connection briefly drops.'
            );

            if (!confirmed) {
                return;
            }

            // Show loading state
            this.updateStatus('resetting server...');

            // Call reset API
            await window.API.resetServer();

            console.log('Launchpad: Server reset initiated');

            // Show success message
            this.updateStatus('server reset initiated - reconnecting...');

            // Wait a moment for the server to restart, then reload the page
            setTimeout(() => {
                window.location.reload();
            }, 3000);

        } catch (error) {
            console.error('Launchpad: Failed to reset server:', error);
            this.showError('failed to reset server: ' + error.message);
        }
    }

    /**
     * Show confirmation modal
     * @param {string} title - Modal title
     * @param {string} message - Main message
     * @param {string} [details] - Additional details (optional)
     * @param {string} [primaryLabel='confirm'] - Label for the primary (destructive / intent) button
     * @param {string} [secondaryLabel='cancel'] - Label for the safe no-op button
     * @returns {Promise<boolean>} - True if confirmed, false if cancelled. Cancel is ALWAYS a no-op — callers must never map cancel to a destructive action.
     */
    showConfirmModal(title, message, details = null, primaryLabel = 'confirm', secondaryLabel = 'cancel') {
        return new Promise((resolve) => {
            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            // Create modal content
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» ${title}</div>
                    <div class="modal-body">
                        <div class="modal-message">${message}</div>
                        ${details ? `<div class="modal-description">${details}</div>` : ''}
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
     * Create new project with auto-generated workspace
     */
    async createNewSession() {
        console.log('Launchpad: Creating new project');

        try {
            // Show modal to get project details
            const projectDetails = await this.showProjectNameModal();

            if (!projectDetails) {
                console.log('Launchpad: Project creation cancelled');
                return; // User cancelled
            }

            // Show loading state
            this.updateStatus('creating new project...');

            // Create session with auto-generated path and template copying.
            // Include current xterm cell grid dims so the tmux pane is
            // birthed at the right size (avoids the 132x40 default → resize
            // flash before the WS handshake reshapes it).
            const _dims = this._getTerminalDims();
            const session = await window.API.createSession({
                auto_start_claude: true,
                copy_templates: true,
                project_name: projectDetails.name,
                ..._dims
            });

            console.log('Launchpad: New project created:', session);

            // Save project to config with the actual path from the session
            try {
                await window.API.createProject({
                    name: projectDetails.name,
                    path: session.working_dir,
                    description: projectDetails.description || null
                });
                console.log('Launchpad: Project saved to config');
            } catch (error) {
                // If project already exists, that's ok - continue anyway
                if (!error.message.includes('already exists')) {
                    console.error('Launchpad: Failed to save project:', error);
                }
            }

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session }
            }));

        } catch (error) {
            console.error('Launchpad: Failed to create session:', error);

            // If a session already exists, the user's stated intent was
            // "create a new project" — carry it out immediately. The prior
            // tmux session is detached (not killed) and stays available in
            // the running-sessions list / banner for rejoin.
            if (error.message.includes('already running')) {
                this.detachAndCreateNew();
            } else {
                this.showError('failed to create session: ' + error.message);
            }
        }
    }

    /**
     * Show modal to prompt for project name and description
     * @param {object} [options]
     * @param {string} [options.defaultName] - Prefill the name input
     * @param {string} [options.title] - Override the modal title
     * @param {string} [options.confirmLabel] - Override the confirm button label
     * @param {string} [options.pathHint] - Display the path being added as a hint
     * @returns {Promise<{name: string, description: string}|null>} Project details or null if cancelled
     */
    showProjectNameModal(options = {}) {
        const {
            defaultName = '',
            title = 'name this project',
            confirmLabel = 'create session',
            pathHint = null,
        } = options;

        return new Promise((resolve) => {
            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            const escapeHtml = (s) => String(s)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');

            const pathHintHtml = pathHint
                ? `<div class="modal-input-group"><div class="modal-label">folder</div><div class="folder-picker-path">${escapeHtml(pathHint)}</div></div>`
                : '';

            // Create modal content
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» ${escapeHtml(title)}</div>
                    <div class="modal-body">
                        ${pathHintHtml}
                        <div class="modal-input-group">
                            <label class="modal-label">project name</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="modal-project-name"
                                placeholder="e.g., My Awesome Project"
                                value="${escapeHtml(defaultName)}"
                                autocomplete="off"
                            />
                            <div class="modal-description">
                                give your project a memorable name. you can reconnect to it later from the launcher.
                            </div>
                        </div>
                        <div class="modal-input-group">
                            <label class="modal-label">description (optional)</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="modal-project-description"
                                placeholder="e.g., Building an AI-powered chatbot"
                                autocomplete="off"
                            />
                            <div class="modal-description">
                                add a short description to help remember what this project is about.
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="modal-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="modal-confirm">${escapeHtml(confirmLabel)}</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const nameInput = overlay.querySelector('#modal-project-name');
            const descInput = overlay.querySelector('#modal-project-description');
            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            // Focus name input and select existing content if prefilled
            setTimeout(() => {
                nameInput.focus();
                if (defaultName) {
                    nameInput.select();
                }
            }, 100);

            // Handle Enter key on name input (moves to description)
            nameInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (nameInput.value.trim()) {
                        descInput.focus();
                    }
                }
            });

            // Handle Enter key on description input (submits)
            descInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const name = nameInput.value.trim();
                    if (name) {
                        const description = descInput.value.trim();
                        document.body.removeChild(overlay);
                        resolve({ name, description });
                    } else {
                        nameInput.focus();
                    }
                }
            });

            // Handle Escape key
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    document.body.removeChild(overlay);
                    resolve(null);
                }
            });

            // Handle confirm button
            confirmBtn.addEventListener('click', () => {
                const name = nameInput.value.trim();
                if (name) {
                    const description = descInput.value.trim();
                    document.body.removeChild(overlay);
                    resolve({ name, description });
                } else {
                    nameInput.focus();
                }
            });

            // Handle cancel button
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(null);
            });

            // Handle click outside modal
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    document.body.removeChild(overlay);
                    resolve(null);
                }
            });
        });
    }

    /**
     * Show the "clone from github" modal — collects URL + parent dir +
     * description, calls the backend ``POST /projects/clone`` endpoint
     * (which runs ``gh repo clone``), then refreshes the project list and
     * lands the user in a session pointed at the freshly cloned folder.
     *
     * Errors are surfaced inline (no alert()) and mapped from HTTP status:
     *   401 → gh auth failed
     *   404 → repo not found / no access
     *   409 → folder or project name collision
     *   503 → gh CLI missing on server
     *   504 → clone took >5 min
     *   other → server-provided detail text.
     */
    async showCloneFromGithubModal() {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';

        const escapeHtml = (s) => String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');

        overlay.innerHTML = `
            <div class="modal-content">
                <div class="modal-header">» clone from github</div>
                <div class="modal-body">
                    <div class="modal-input-group">
                        <label class="modal-label">github repo url</label>
                        <input
                            type="text"
                            class="modal-input"
                            id="modal-clone-url"
                            placeholder="https://github.com/owner/repo or owner/repo"
                            autocomplete="off"
                            spellcheck="false"
                        />
                        <div class="modal-description">
                            paste the full url or use gh shorthand (owner/repo). server runs <code>gh repo clone</code> — gh must be authenticated.
                        </div>
                    </div>
                    <div class="modal-input-group">
                        <label class="modal-label">parent directory</label>
                        <input
                            type="text"
                            class="modal-input"
                            id="modal-clone-parent"
                            placeholder="~/projects"
                            value="~/projects"
                            autocomplete="off"
                            spellcheck="false"
                        />
                        <div class="modal-description">
                            the cloned folder will be created inside this directory.
                        </div>
                    </div>
                    <div class="modal-input-group">
                        <label class="modal-label">description (optional)</label>
                        <input
                            type="text"
                            class="modal-input"
                            id="modal-clone-description"
                            placeholder="e.g., upstream library i'm patching"
                            autocomplete="off"
                        />
                    </div>
                    <div class="modal-description" id="modal-clone-status" style="display:none;"></div>
                </div>
                <div class="modal-footer">
                    <button class="modal-btn modal-btn-secondary" id="modal-clone-cancel">cancel</button>
                    <button class="modal-btn modal-btn-primary" id="modal-clone-confirm">clone &amp; open</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const urlInput = overlay.querySelector('#modal-clone-url');
        const parentInput = overlay.querySelector('#modal-clone-parent');
        const descInput = overlay.querySelector('#modal-clone-description');
        const confirmBtn = overlay.querySelector('#modal-clone-confirm');
        const cancelBtn = overlay.querySelector('#modal-clone-cancel');
        const statusEl = overlay.querySelector('#modal-clone-status');

        let busy = false;

        const closeModal = () => {
            if (overlay.parentNode) {
                document.body.removeChild(overlay);
            }
        };

        const setStatus = (msg, isError = false) => {
            statusEl.style.display = msg ? 'block' : 'none';
            statusEl.textContent = msg;
            statusEl.style.color = isError ? '#d77757' : '';
        };

        const mapErrorToMessage = (error) => {
            // api.js throws Error(errorData.detail || `HTTP <code>`). Match
            // on signature substrings the backend embeds in its detail text.
            const msg = String(error && error.message || error || '');
            const lower = msg.toLowerCase();
            if (lower.includes('not authenticated') || lower.includes('auth/network') || lower.includes('gh auth login')) {
                return 'gh CLI not authenticated. run `gh auth login` in a terminal on the server.';
            }
            if (lower.includes('repository not found') || lower.includes('repo not found') || lower.startsWith('not found')) {
                return 'repo not found or no access. check the url and your gh auth scopes.';
            }
            if (lower.includes('already exists')) {
                return 'folder or project name already exists.';
            }
            if (lower.includes('gh cli not') || lower.includes('install with `brew install gh`')) {
                return 'gh CLI not installed on server. install with `brew install gh`.';
            }
            if (lower.includes('timed out') || lower.includes('timeout')) {
                return 'clone timed out after 5 minutes.';
            }
            // Strip a bare "HTTP NNN" prefix if api.js fell back to it.
            const cleaned = msg.replace(/^HTTP\s+\d{3}:?\s*/i, '').trim();
            return cleaned || 'clone failed.';
        };

        const submit = async () => {
            if (busy) return;
            const repoUrl = urlInput.value.trim();
            if (!repoUrl) {
                setStatus('paste a github url first.', true);
                urlInput.focus();
                return;
            }
            const parentDir = parentInput.value.trim() || '~/projects';
            const description = descInput.value.trim();

            busy = true;
            confirmBtn.disabled = true;
            cancelBtn.disabled = true;
            urlInput.disabled = true;
            parentInput.disabled = true;
            descInput.disabled = true;
            setStatus('cloning... (may take a minute)');

            try {
                const project = await window.API.cloneProjectFromGithub({
                    repoUrl,
                    parentDir,
                    description: description || undefined,
                });
                // Success — refresh project list, close modal, open session
                // in the cloned dir. selectProject does the heavy lifting.
                await this.loadProjects();
                closeModal();
                await this.selectProject({
                    name: project.name,
                    path: project.path,
                    description: project.description || null,
                });
            } catch (error) {
                console.error('Launchpad: clone-from-github failed:', error);
                setStatus(mapErrorToMessage(error), true);
                busy = false;
                confirmBtn.disabled = false;
                cancelBtn.disabled = false;
                urlInput.disabled = false;
                parentInput.disabled = false;
                descInput.disabled = false;
            }
        };

        // Focus url input.
        setTimeout(() => urlInput.focus(), 100);

        // Enter on url → focus parent. Enter on parent → focus desc.
        // Enter on desc → submit. Escape anywhere → cancel.
        urlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                if (urlInput.value.trim()) parentInput.focus();
            }
        });
        parentInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                descInput.focus();
            }
        });
        descInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                submit();
            }
        });
        overlay.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !busy) {
                closeModal();
            }
        });

        confirmBtn.addEventListener('click', submit);
        cancelBtn.addEventListener('click', () => {
            if (!busy) closeModal();
        });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay && !busy) closeModal();
        });
    }

    /**
     * Connect to existing session
     */
    async connectToExistingSession() {
        try {
            this.updateStatus('connecting to existing session...');
            const data = await window.API.getSession();
            const session = data.session || data;

            console.log('Launchpad: Connecting to existing session:', session);

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session }
            }));
        } catch (error) {
            console.error('Launchpad: Failed to get existing session:', error);
            this.showError('failed to connect: ' + error.message);
        }
    }

    /**
     * Detach from the existing session (tmux keeps running) and create a
     * fresh one. Mirror of ``detachAndOpenProject`` for the "new project"
     * path — prior session lingers and can be re-adopted later.
     */
    async detachAndCreateNew() {
        try {
            this.updateStatus('detaching from current session...');
            await window.API.detachSession();

            // Wait a moment, then create new. Same race-avoidance rationale
            // as ``detachAndOpenProject``.
            setTimeout(() => this.createNewSession(), 500);
        } catch (error) {
            console.error('Launchpad: Failed to detach session:', error);
            this.showError('failed to detach session: ' + error.message);
        }
    }

    /**
     * Open a project by name (used by the deep-link router, Item 9).
     *
     * The router already validated the name against a strict regex, but
     * we re-verify membership in `this.projects` before calling into
     * `selectProject` — if the user clicks a deep link for a project
     * that was deleted / renamed, we surface a clear error instead of
     * calling the backend with an unknown path.
     *
     * This method is idempotent and safe to call before `loadProjects()`
     * completes — it waits up to ~2s for the project list to populate,
     * which is normally ready within one tick of `App.showLaunchpad()`.
     */
    async openProjectByName(name) {
        console.log('Launchpad: openProjectByName:', name);

        // Wait for the project list if it hasn't loaded yet. App.showLaunchpad
        // calls loadProjects() inline; this handles the race where the
        // router fires right after auth but before loadProjects resolves.
        const deadline = Date.now() + 2000;
        while ((!this.projects || this.projects.length === 0) && Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 50));
        }

        // Match by exact name first, then case-insensitive fallback.
        let project = (this.projects || []).find(p => p.name === name);
        if (!project) {
            project = (this.projects || []).find(
                p => p.name && p.name.toLowerCase() === name.toLowerCase()
            );
        }

        if (!project) {
            console.warn('Launchpad: deep-link project not found:', name);
            this.showError(`project not found: ${name}`);
            return;
        }

        await this.selectProject(project);
    }

    /**
     * Open a project by picking a folder via the server-side filesystem browser,
     * then save it to the project list (history) before opening.
     */
    async openProjectFromFolder() {
        console.log('Launchpad: Opening project from folder');

        try {
            const selectedPath = await this.showFolderPickerModal();
            if (!selectedPath) {
                console.log('Launchpad: Folder selection cancelled');
                return;
            }

            // Derive a default name from the folder basename
            const defaultName = selectedPath.split('/').filter(Boolean).pop() || selectedPath;

            // Ask the user to confirm/adjust name + description
            const details = await this.showProjectNameModal({
                defaultName,
                title: 'add project',
                confirmLabel: 'open project',
                pathHint: selectedPath,
            });
            if (!details) {
                console.log('Launchpad: Project metadata entry cancelled');
                return;
            }

            this.updateStatus(`adding ${details.name}...`);

            // Save to projects config so it shows up in history.
            // If the name collides, append a short suffix until it's unique.
            const savedName = await this.saveProjectWithUniqueName({
                name: details.name,
                path: selectedPath,
                description: details.description || null,
            });

            // Refresh project list so the new entry shows up at the top
            await this.loadProjects();

            // Open the project
            await this.selectProject({
                name: savedName,
                path: selectedPath,
                description: details.description || null,
            });
        } catch (error) {
            console.error('Launchpad: Failed to open project from folder:', error);
            this.showError('failed to open folder: ' + error.message);
        }
    }

    /**
     * Try to save a project, appending a suffix if the name already exists.
     * Returns the name that was actually saved, or the original name if the
     * project already existed (we treat that as success).
     */
    async saveProjectWithUniqueName({ name, path, description }) {
        let attempt = name;
        for (let i = 0; i < 20; i++) {
            try {
                await window.API.createProject({
                    name: attempt,
                    path,
                    description,
                });
                return attempt;
            } catch (error) {
                if (!error.message || !error.message.includes('already exists')) {
                    throw error;
                }
                // If an existing project already has this path, reuse it
                const existing = this.projects.find(p => p.path === path);
                if (existing) {
                    return existing.name;
                }
                attempt = `${name} (${i + 2})`;
            }
        }
        throw new Error('could not find a unique name for this project');
    }

    /**
     * Show a folder-picker modal that browses the server filesystem.
     * Resolves with the chosen absolute path, or null if cancelled.
     */
    showFolderPickerModal() {
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            overlay.innerHTML = `
                <div class="modal-content folder-picker-modal">
                    <div class="modal-header">» select a folder</div>
                    <div class="modal-body">
                        <div class="folder-picker-path" id="folder-picker-path">loading...</div>
                        <div class="folder-picker-toolbar">
                            <button class="folder-picker-toolbar-btn" id="folder-picker-up" title="go to parent directory">⬆ up</button>
                            <button class="folder-picker-toolbar-btn" id="folder-picker-home" title="go to home directory">🏠 home</button>
                        </div>
                        <div class="folder-picker-list" id="folder-picker-list">
                            <div class="folder-picker-empty">loading...</div>
                        </div>
                        <div class="modal-description">
                            select a folder, then click "open here" to use it as the project root.
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="folder-picker-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="folder-picker-confirm">open here</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const pathEl = overlay.querySelector('#folder-picker-path');
            const listEl = overlay.querySelector('#folder-picker-list');
            const upBtn = overlay.querySelector('#folder-picker-up');
            const homeBtn = overlay.querySelector('#folder-picker-home');
            const confirmBtn = overlay.querySelector('#folder-picker-confirm');
            const cancelBtn = overlay.querySelector('#folder-picker-cancel');

            let currentPath = null;
            let currentParent = null;

            const close = (value) => {
                document.body.removeChild(overlay);
                resolve(value);
            };

            const loadPath = async (targetPath) => {
                listEl.innerHTML = '<div class="folder-picker-empty">loading...</div>';
                try {
                    const data = await window.API.browseDirectory(targetPath);
                    currentPath = data.path;
                    currentParent = data.parent;
                    pathEl.textContent = data.path;
                    upBtn.disabled = !data.parent;

                    if (!data.entries || data.entries.length === 0) {
                        listEl.innerHTML = '<div class="folder-picker-empty">no subfolders here</div>';
                        return;
                    }

                    listEl.innerHTML = data.entries.map(entry => `
                        <div class="folder-picker-item" data-path="${entry.path.replace(/"/g, '&quot;')}">
                            <span class="folder-picker-icon">📁</span>
                            <span class="folder-picker-name">${entry.name}</span>
                        </div>
                    `).join('');

                    listEl.querySelectorAll('.folder-picker-item').forEach(item => {
                        item.addEventListener('click', () => {
                            loadPath(item.dataset.path);
                        });
                    });
                } catch (error) {
                    console.error('Launchpad: Folder browse failed:', error);
                    listEl.innerHTML = `<div class="folder-picker-empty">error: ${error.message}</div>`;
                }
            };

            upBtn.addEventListener('click', () => {
                if (currentParent) {
                    loadPath(currentParent);
                }
            });

            homeBtn.addEventListener('click', () => {
                loadPath('~');
            });

            confirmBtn.addEventListener('click', () => {
                if (currentPath) {
                    close(currentPath);
                }
            });

            cancelBtn.addEventListener('click', () => close(null));

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    close(null);
                }
            });

            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') close(null);
            });

            // Start at the server's default location
            loadPath(null);

            setTimeout(() => confirmBtn.focus(), 100);
        });
    }

    /**
     * Select and open existing project
     */
    async selectProject(project) {
        console.log('Launchpad: Selecting project:', project.name);

        try {
            // Show loading state
            this.updateStatus(`opening ${project.name}...`);

            // Create session with project path (no template copying for existing projects).
            // Include current xterm cell grid dims so the tmux pane is birthed
            // at the right size — see the "new project" path for rationale.
            const _dims = this._getTerminalDims();
            const session = await window.API.createSession({
                working_dir: project.path,
                auto_start_claude: true,
                copy_templates: false,
                project_name: project.name,
                ..._dims
            });

            console.log('Launchpad: Project session created:', session);

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session, project }
            }));

        } catch (error) {
            console.error('Launchpad: Failed to open project:', error);

            // If a session already exists, SWAP to the project the user just
            // clicked. The old tmux session is DETACHED (not destroyed) so
            // it keeps running on the server and reappears in the
            // running-sessions list / banner for rejoin.
            if (error.message.includes('already running')) {
                this.detachAndOpenProject(project);
            } else {
                this.showError(`failed to open ${project.name}: ${error.message}`);
            }
        }
    }

    /**
     * Detach from the existing session (tmux keeps running) and open the
     * selected project in a fresh session. The prior session lingers on
     * the tmux side and shows up in the Adopt list tagged as cloude-owned,
     * so the user can rejoin it later without losing any state.
     */
    async detachAndOpenProject(project) {
        try {
            this.updateStatus('detaching from current session...');
            await window.API.detachSession();

            // Wait a moment, then open project. The brief delay lets the
            // server finish clearing its backend handles before the new
            // create-session call lands — avoids a race where we try to
            // create while the old backend is still tearing down.
            setTimeout(() => this.selectProject(project), 500);
        } catch (error) {
            console.error('Launchpad: Failed to detach session:', error);
            this.showError('failed to detach session: ' + error.message);
        }
    }

    /**
     * Update status message
     */
    updateStatus(message) {
        const statusEl = document.getElementById('statusText');
        if (statusEl) {
            statusEl.setAttribute('data-status', message);
        }
        console.log('Launchpad:', message);
    }

    /**
     * Show error message
     */
    showError(message) {
        // For now, just log and use browser alert
        // Could be improved with a proper error UI element
        console.error('Launchpad Error:', message);
        alert(`Error: ${message}`);
    }
}

// Export singleton instance
window.Launchpad = new Launchpad();
console.log('[Launchpad Module] Exported as window.Launchpad:', window.Launchpad);
