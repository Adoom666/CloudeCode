/**
 * Launchpad Module - Project selection UI with terminal aesthetic
 */

console.log('[Launchpad Module] Loading...');

class Launchpad {
    constructor() {
        this.launchpadScreen = null;
        this.projects = [];
        // feat/db-is-authoritative - the provenance report for the list
        // above: which source answered, and whether cloude.db and
        // config.json agree. null means the check has not answered, which
        // the banner renders as CANNOT DETERMINE, never as healthy.
        this.projectAuthority = null;
        // STAGE C - the sessions the evidence ladder could not attribute.
        // null means the prompt has not been fetched yet, which renders
        // as NOTHING rather than as "no questions": an unfetched prompt
        // and an empty one are different facts and only one of them is
        // safe to show as silence.
        this.attributionPrompt = null;
        // Set when the user closes the card WITHOUT answering. Deliberately
        // NOT persisted: "leave as external" is the answer that is
        // remembered, and remembering a silent close would make the
        // questions vanish without anyone answering them.
        this.attributionPromptClosed = false;
        // feat/projects-table (S3) - presence for each DB-tracked project,
        // keyed by the project's raw config path (matches config.json's
        // ProjectConfig.path, which is also what the DB import stored
        // verbatim into projects.raw_path - see project_store.py). A
        // project with no entry here has not reached the DB yet (created
        // via config write after the one-time boot import) and renders
        // as if 'unchecked': normal, no badge, actions allowed. Populated
        // by loadProjectPresence(), called from loadProjects().
        this.projectPresence = new Map();
        // Running tmux sessions on the `cloude` socket. Populated by
        // loadRunningSessions() - a merged view of:
        //   (a) the currently-active backend (from GET /sessions), and
        //   (b) attachable/external sessions (from GET /sessions/attachable).
        // Each row carries an is_active flag so the render pass can style
        // the live one differently without a second DOM query.
        this.runningSessions = [];
        // GUARD (deep-link duplicate-session regression fix): set true
        // for the duration of openProjectByName()'s resolution. selectProject()
        // checks this flag and refuses to create a session while it's set -
        // see openProjectByName()'s docstring and selectProject()'s guard
        // clause. This makes "deep-link resolution never creates" an
        // enforced invariant rather than an implicit property of call
        // order, so a future edit that re-wires openProjectByName() into
        // selectProject() fails loudly instead of silently regressing.
        this._resolvingDeepLink = false;
        // feat/project-session-tree (S8) - per-running-session project
        // attribution, keyed by tmux name. Sourced from
        // GET /sessions/records (the datastore, S7's backfilled
        // project_id / project_attribution columns), NOT from the live
        // tmux probe - neither SessionInfo nor AttachableSession carry
        // these fields, only the stored sessions row does. Populated by
        // loadSessionAttribution(), consumed by
        // _buildProjectSessionGroups() to decide which project (or "no
        // project", or NEEDS ATTENTION) each running session belongs
        // under in the home-screen tree.
        //
        // NAME-ONLY FALLBACK MAP. Two stored rows can share a tmux_name
        // (a session recreated after its pane died reuses the name with
        // a new tmux_created_epoch), so this is keyed on tmux_name but
        // built to exclude archived rows and to prefer the newest
        // tmux_created_epoch among the rest - see
        // _resolveSessionAttribution(). Consulted only when
        // sessionAttributionByInstance misses. Never populated with an
        // archived row: an archived row must never shadow a live
        // session under a name-only join.
        this.sessionAttribution = new Map();
        // INSTANCE-EXACT MAP, keyed on `${tmux_name}\u0000${tmux_created_
        // epoch}` (a NUL separator, so no tmux name can ever collide with
        // one that only differs by where the epoch digits start). Both SessionInfo and AttachableSession DO carry the
        // tmux creation epoch on the wire - as `created_at_epoch`, the
        // same underlying tmux `#{session_created}` value
        // `tmux_created_epoch` names on the stored row (see
        // AttachableSession.created_at_epoch / SessionInfo.session_row_id
        // merge in loadRunningSessions()) - so a caller holding a live
        // session's own name+epoch can resolve its EXACT stored row,
        // including one the user archived: that row still comes back
        // (this map, unlike the name-only one, does not exclude
        // archived rows), and the caller drops it on `archived_at`,
        // which is what makes "delete this session's record" keep
        // working for the exact instance the user deleted without
        // letting an unrelated archived row from an older instance of
        // the same name shadow a live one.
        this.sessionAttributionByInstance = new Map();
        // Names where two or more non-archived stored rows could not be
        // ranked against each other (equal or unmeasurable
        // tmux_created_epoch). Deliberately absent from
        // sessionAttribution - a caller must treat this as
        // could-not-evaluate, never silently pick one.
        this.sessionAttributionAmbiguous = new Set();
        // Three-outcome latch for the WHOLE attribution fetch, mirrored
        // on `runningSessionsListing` above: true only when the last
        // GET /sessions/records call actually returned rows to read. A
        // false value means every running session renders into NEEDS
        // ATTENTION, because "we could not read the attribution table"
        // must never be silently mistaken for "these sessions belong to
        // no project" - see _buildProjectSessionGroups().
        this.sessionAttributionListingOk = true;
        this.sessionAttributionListingDetail = null;
        // feat/ended-sessions-visibility - the SAME GET /sessions/records
        // payload `sessionAttribution` is keyed from, kept as a flat list
        // because the tree needs the rows whose tmux session is GONE, and
        // those are exactly the ones no live probe can name. Empty until
        // the fetch answers; a FAILED fetch leaves it empty AND drops
        // `sessionAttributionListingOk`, so "we could not read the table"
        // never renders as "you have no ended sessions".
        this.sessionRecords = [];
        // In-memory only (not persisted across reload, unlike the
        // section-level collapse state in localStorage below) - per-
        // project expand/collapse state for the tree's child-session
        // rows, keyed by a stable node key ("project:<name>" or the
        // literal "__no_project__"). Survives re-renders because it
        // lives on the instance, not in the DOM.
        this._collapsedProjectNodes = new Set();
        // Three-outcome latch for GET /projects, the same shape as
        // sessionAttributionListingOk above. null = never asked, true =
        // the list was read (so an empty list really means "no
        // projects"), false = the fetch failed and the empty list is an
        // absence of evidence, not evidence of absence. The "new
        // session" picker reads this so a failed fetch is never
        // presented to the user as "you have no projects".
        this.projectsListingOk = null;
    }

    /**
     * Initialize launchpad screen
     */
    init() {
        this.launchpadScreen = document.getElementById('launchpad-screen');
        this.renderLaunchpadUI();
        // Wire the inline "+ new" speed-dial FAB. Markup was just injected
        // by renderLaunchpadUI() into the right side of the "running
        // sessions" section heading row; the 6 sub-actions route back
        // into the same handlers the old inline "new project" section used.
        this.setupNewFab();
        this.bindHeaderHelpToggle();
        // Note: loadProjects() will be called by App.showLaunchpad()
        this._startRunningSessionsPoller();
    }

    /**
     * Wire the inline "+ new" speed-dial FAB.
     *
     * Markup is injected by renderLaunchpadUI() into the right side of
     * the "running sessions" section heading row (#new-fab). Six
     * sub-actions route into the same handlers the old inline "new
     * project" section used - no logic duplicated. Idempotent: safe to
     * call multiple times (guarded by a flag).
     *
     * Because the FAB lives inside #launchpad-screen, it shows/hides
     * naturally with the screen - no separate visibility plumbing
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
            console.warn('Launchpad: new-fab markup missing - skipping wire');
            return;
        }

        // Map the FAB's data-action attrs onto our existing handlers.
        // Wrapped so `this` resolves correctly inside the dispatch table.
        const actions = {
            'new-claude-project': () => this.startNewClaudeProject(),
            'new-session':        () => this.startSessionInExistingProject(),
            // No 'open-folder' entry: this table is keyed by the FAB's
            // data-action attributes and no menu item carries that action
            // any more. openProjectFromFolder() is still very much alive -
            // startNewClaudeProject() calls it directly as its third
            // choice - so only the dead dispatch key is gone, not the flow.
            'connect-openclaw':   () => this.createNewSessionWithAgent('openclaw'),
            'connect-hermes':     () => this.createNewSessionWithAgent('hermes'),
            'new-console':        () => this.createConsoleSession(),
        };

        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            this.toggleNewFab();
        });

        // Item dispatch via delegation - survives any future re-render
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
     * Wire the header's "?" control to the launchpad help disclosure.
     *
     * Description: item 48 moves the CONTROL to the top right of the
     *   header; the help panel itself does not move - it stays the first
     *   child of ``.launchpad-container`` where it already is, and stays
     *   a native ``<details>``. The in-pane ``<summary>`` is still in the
     *   markup (it is what makes the element a disclosure at all) but is
     *   visually hidden by CSS, so there is exactly ONE control and it is
     *   the header one. The button is bound once and resolves the
     *   ``<details>`` at click time, because ``renderLaunchpadUI()``
     *   replaces that element on every render while the header button
     *   outlives all of them.
     * Inputs: none (reads ``#launchpad-help-btn`` and the current
     *   ``.adopt-disclosure``).
     * Output: boolean - true when the control was found and wired, false
     *   when the header button is absent (nothing is claimed either way
     *   about the help panel).
     * Example: lp.bindHeaderHelpToggle();
     */
    bindHeaderHelpToggle() {
        const btn = document.getElementById('launchpad-help-btn');
        if (!btn) {
            console.warn('Launchpad: header help button missing - help control not wired');
            return false;
        }
        if (btn.__boundHelpToggle) return true;
        btn.__boundHelpToggle = true;
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const details = document.querySelector('#launchpad-screen .adopt-disclosure');
            if (!details) return;
            const next = !details.open;
            details.open = next;
            btn.setAttribute('aria-expanded', String(next));
            if (next) details.scrollIntoView({ block: 'nearest' });
        });
        return true;
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
     * Place the fan-out menu against the "+" trigger.
     *
     * `.new-fab__menu` is `position: fixed` (see the placement note in
     * client/css/styles.css), so this is the only thing that decides
     * where it lands. AnchorPopover puts it ABOVE the trigger with their
     * right edges flush, drops it below only when there is no room
     * above, and clamps into the visual viewport either way - which is
     * what makes it impossible for `.launchpad-scroll` to clip it no
     * matter how low the heading has been scrolled.
     *
     * Right-edge-flush is load-bearing: the pills share a right edge
     * (`align-items: flex-end`) and each row is `row-reverse` so the
     * icons form a single straight column under the "+". Any placement
     * rule that moved the menu's right edge off the trigger's would
     * break that alignment.
     *
     * @returns {void}
     */
    placeNewFabMenu() {
        const trigger = document.getElementById('new-fab-trigger');
        const menu = document.querySelector('#new-fab .new-fab__menu');
        if (!trigger || !menu || !window.AnchorPopover) return;
        window.AnchorPopover.place(menu, trigger);
    }

    /**
     * Open the FAB menu (idempotent).
     */
    openNewFab() {
        const fab = document.getElementById('new-fab');
        const trigger = document.getElementById('new-fab-trigger');
        const backdrop = document.getElementById('new-fab-backdrop');
        if (!fab || !trigger || !backdrop) return;
        // Measure and place BEFORE the open class lands, so the menu
        // animates in at its final position rather than sliding there.
        // The items are laid out (opacity 0 and a transform, neither of
        // which affects layout) so the menu measures its true size here.
        this.placeNewFabMenu();
        fab.classList.add('new-fab--open');
        // A viewport change while the menu is open (rotation, iOS URL bar
        // collapse, a scroll driven by the keyboard) moves the trigger
        // out from under a fixed menu. Re-place instead of drifting.
        if (!this._newFabReposition) {
            this._newFabReposition = () => this.placeNewFabMenu();
        }
        window.addEventListener('resize', this._newFabReposition);
        window.addEventListener('scroll', this._newFabReposition, true);
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', this._newFabReposition);
            window.visualViewport.addEventListener('scroll', this._newFabReposition);
        }
        trigger.setAttribute('aria-expanded', 'true');
        backdrop.hidden = false;
        backdrop.setAttribute('data-open', '1');
        // Make menu items focusable when open
        fab.querySelectorAll('.new-fab__item').forEach(it => it.setAttribute('tabindex', '0'));
    }

    /**
     * Close the FAB menu (idempotent - also called from app.js when
     * the launchpad screen is being torn down).
     */
    closeNewFab() {
        const fab = document.getElementById('new-fab');
        const trigger = document.getElementById('new-fab-trigger');
        const backdrop = document.getElementById('new-fab-backdrop');
        if (!fab) return;
        fab.classList.remove('new-fab--open');
        if (this._newFabReposition) {
            window.removeEventListener('resize', this._newFabReposition);
            window.removeEventListener('scroll', this._newFabReposition, true);
            if (window.visualViewport) {
                window.visualViewport.removeEventListener('resize', this._newFabReposition);
                window.visualViewport.removeEventListener('scroll', this._newFabReposition);
            }
        }
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
     * Idempotent - guarded by ``this._runningPollInterval`` so repeated
     * calls (e.g. re-entering the launchpad after a session swap) don't
     * stack multiple intervals. Auth-gated per tick: skips the fetch
     * entirely when the user isn't logged in, so we don't hammer /sessions
     * with anonymous requests before the OTP flow completes.
     *
     * Runs forever; does not pause on tab hide - external tmux sessions
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
        if (window.TerminalMetrics
                && typeof window.TerminalMetrics.currentGrid === 'function') {
            return window.TerminalMetrics.currentGrid();
        }
        // Module missing (load-order regression). Send nothing rather
        // than a guess; the server falls back to its own defaults.
        console.warn('Launchpad: TerminalMetrics unavailable for dims');
        return {};
    }

    /**
     * Load and display projects, then refresh the running-sessions list.
     * Both fetches are non-fatal - the projects error path shows a UI
     * error, the sessions path is logged and silently renders empty.
     */
    async loadProjects() {
        try {
            this.projects = await window.API.getProjects();
            this.projectsListingOk = true;
            // Presence and authority are BOTH fetched before the first
            // paint so neither a missing project nor a degraded datastore
            // flashes as normal for one frame - renderProjectList() reads
            // this.projectPresence and this.projectAuthority
            // synchronously, so both have to be populated first.
            await Promise.all([
                this.loadProjectPresence(),
                this.loadProjectAuthority(),
            ]);
            this.renderProjectList();
        } catch (error) {
            this.projectsListingOk = false;
            console.error('Launchpad: Failed to load projects:', error);
            this.showError('failed to load projects: ' + error.message);
        }
        // Refresh running sessions in parallel with the projects view.
        // Failure is non-fatal and handled inside loadRunningSessions.
        this.loadRunningSessions();
        // S9 - RECENT is datastore-backed, not a live probe, so it does
        // not need the 5s running-sessions poller; refreshed here (home
        // screen load) and after any restart action.
        this.loadRecentSessions();
        // STAGE C. Independent of the project load: a failure here must
        // not stop the projects rendering, and a failure THERE must not
        // swallow the question set.
        this.loadAttributionPrompt();
    }

    /**
     * STAGE C: fetch the sessions the evidence ladder could not attribute.
     *
     * Non-fatal. A failed fetch leaves this.attributionPrompt at null,
     * which renders as nothing - not as "no questions". The two are
     * different facts and showing the second for the first is the same
     * false green this whole import exists to remove.
     *
     * @returns {Promise<void>}
     */
    async loadAttributionPrompt() {
        try {
            this.attributionPrompt = await window.API.getSessionAttributionPrompt();
        } catch (error) {
            console.error('Launchpad: attribution prompt fetch failed:', error);
            this.attributionPrompt = null;
        }
        this.renderAttributionPrompt();
    }

    /**
     * STAGE C: render the prompt card, itemised, with hints as words.
     *
     * Description: writes into #attribution-prompt. Renders NOTHING for
     * state 'none', for a null prompt, and after the user closes the
     * card. State 'unavailable' renders its own line: whether there is
     * anything to ask CANNOT BE DETERMINED, which is not the same as
     * there being nothing.
     *
     * @returns {void}
     */
    renderAttributionPrompt() {
        const slot = document.getElementById('attribution-prompt');
        if (!slot) return;
        const p = this.attributionPrompt;
        if (!p || this.attributionPromptClosed) { slot.innerHTML = ''; return; }

        if (p.state === 'unavailable') {
            slot.innerHTML = `<div class="attribution-prompt attribution-prompt--unknown" data-attribution-state="unavailable">${this._escapeHtml(p.notice || 'session attribution CANNOT BE DETERMINED')}</div>`;
            return;
        }
        if (p.state !== 'pending' || !Array.isArray(p.sessions) || p.sessions.length === 0) {
            slot.innerHTML = '';
            return;
        }

        const rows = p.sessions.map((s) => {
            const name = this._escapeHtml(s.tmux_name || '');
            // WHAT THE USER READS vs WHAT THE ACTION IS KEYED ON.
            //
            // ``name`` above stays the raw tmux handle, because it is the
            // key every adopt and decline POST is sent under and a label
            // is free-form text that identifies nothing. ``shown`` is the
            // string a human is asked to recognise, and if this session
            // carries a label then the name the user typed is a far
            // better prompt than an internal handle.
            //
            // stripPrefix:false is deliberate and this is the ONE surface
            // that asks for it. The card's whole question is "did you
            // start this?", one of its own hints is that the name matches
            // the auto-generated form, and the user may need to match the
            // exact string against their own `tmux ls`. Stripping the
            // prefix here would remove evidence from an evidence card.
            const shown = this._escapeHtml(
                (window.SessionLabel
                    ? window.SessionLabel.resolve(
                        { label: s.label, name: s.tmux_name },
                        { stripPrefix: false })
                    : null)
                || s.tmux_name || ''
            );
            const started = s.epoch ? this._formatRelativeTime(s.epoch) : 'start time unknown';
            // THE HINTS ARE SENTENCES, NOT A SCORE. Each one is rendered
            // as its own line of prose so the user can weigh what was
            // actually seen. A confidence number here would look
            // authoritative and could not be checked by anyone.
            const hints = (s.hints || []).map(
                (h) => `<li class="attribution-prompt__hint">${this._escapeHtml(h)}</li>`
            ).join('');
            const why = s.reason === 'could_not_evaluate'
                ? 'we could not complete the check for this one'
                : 'we found no record either way';
            return `
                <li class="attribution-prompt__row" data-tmux-name="${name}">
                    <label class="attribution-prompt__pick">
                        <input type="checkbox" class="attribution-prompt__check" data-tmux-name="${name}" checked>
                        <span class="attribution-prompt__name" title="tmux session: ${name}">${shown}</span>
                    </label>
                    <span class="attribution-prompt__meta">started ${this._escapeHtml(started)}</span>
                    <span class="attribution-prompt__why" data-reason="${this._escapeHtml(s.reason || '')}">${why}</span>
                    ${hints ? `<ul class="attribution-prompt__hints">${hints}</ul>` : ''}
                </li>`;
        }).join('');

        slot.innerHTML = `
            <section class="attribution-prompt" data-attribution-state="pending" aria-label="sessions we could not attribute">
                <button type="button" class="attribution-prompt__close" id="attribution-prompt-close" aria-label="close for now">x</button>
                <p class="attribution-prompt__notice">${this._escapeHtml(p.notice || '')}</p>
                <ul class="attribution-prompt__list">${rows}</ul>
                <div class="attribution-prompt__actions attribution-prompt__actions--all">
                    <button type="button" class="attribution-prompt__btn attribution-prompt__btn--primary" id="attribution-adopt-all">adopt all</button>
                    <button type="button" class="attribution-prompt__btn" id="attribution-choose">choose individually</button>
                    <button type="button" class="attribution-prompt__btn" id="attribution-decline-all">leave as external</button>
                </div>
                <div class="attribution-prompt__actions attribution-prompt__actions--picked" hidden>
                    <button type="button" class="attribution-prompt__btn attribution-prompt__btn--primary" id="attribution-adopt-picked">adopt the ticked ones</button>
                    <button type="button" class="attribution-prompt__btn" id="attribution-decline-picked">leave the ticked ones external</button>
                </div>
                <p class="attribution-prompt__footnote">closing this without answering brings it back next time. leaving them external is remembered.</p>
            </section>`;
        this._bindAttributionPrompt();
    }

    /**
     * Wire the attribution prompt's four real actions.
     *
     * "choose individually" reveals the per session tick boxes and the
     * second action row; it is not itself an answer.
     *
     * @returns {void}
     */
    _bindAttributionPrompt() {
        const slot = document.getElementById('attribution-prompt');
        if (!slot) return;
        const names = () => Array.from(
            slot.querySelectorAll('.attribution-prompt__row')
        ).map((el) => el.getAttribute('data-tmux-name'));
        const picked = () => Array.from(
            slot.querySelectorAll('.attribution-prompt__check')
        ).filter((el) => el.checked).map((el) => el.getAttribute('data-tmux-name'));

        const close = slot.querySelector('#attribution-prompt-close');
        if (close) close.addEventListener('click', () => {
            this.attributionPromptClosed = true;
            this.renderAttributionPrompt();
        });

        const choose = slot.querySelector('#attribution-choose');
        if (choose) choose.addEventListener('click', () => {
            slot.querySelector('.attribution-prompt')?.classList.add('attribution-prompt--picking');
            const all = slot.querySelector('.attribution-prompt__actions--all');
            const one = slot.querySelector('.attribution-prompt__actions--picked');
            if (all) all.hidden = true;
            if (one) one.hidden = false;
        });

        const adoptAll = slot.querySelector('#attribution-adopt-all');
        if (adoptAll) adoptAll.addEventListener('click', () => this._adoptAttributed(names()));
        const adoptPicked = slot.querySelector('#attribution-adopt-picked');
        if (adoptPicked) adoptPicked.addEventListener('click', () => this._adoptAttributed(picked()));

        const declineAll = slot.querySelector('#attribution-decline-all');
        if (declineAll) declineAll.addEventListener('click', () => this._declineAttributed(names()));
        const declinePicked = slot.querySelector('#attribution-decline-picked');
        if (declinePicked) declinePicked.addEventListener('click', () => this._declineAttributed(picked()));
    }

    /**
     * Adopt the named sessions through the EXISTING adopt path.
     *
     * Description: this records origin 'adopted', not 'created', and that
     * distinction is deliberate. We did not create them as far as we can
     * prove; we claimed them. An adopted session badges as ours for good,
     * so the badge is right and no fact is invented.
     *
     * @param {string[]} tmuxNames - sessions to adopt.
     * @returns {Promise<void>}
     */
    async _adoptAttributed(tmuxNames) {
        if (!tmuxNames || tmuxNames.length === 0) return;
        const failed = [];
        for (const name of tmuxNames) {
            try {
                await window.API.adoptSession(name);
            } catch (error) {
                failed.push(name);
                console.error('Launchpad: adopt failed for', name, error);
            }
        }
        if (failed.length) this.showError('could not adopt: ' + failed.join(', '));
        await this.loadAttributionPrompt();
        this.loadRunningSessions();
    }

    /**
     * Record "leave these as external", durably.
     *
     * @param {string[]} tmuxNames - sessions the user left external.
     * @returns {Promise<void>}
     */
    async _declineAttributed(tmuxNames) {
        if (!tmuxNames || tmuxNames.length === 0) return;
        try {
            const out = await window.API.declineSessionAttribution(tmuxNames);
            const stuck = [...(out.not_eligible || []), ...(out.unknown || [])];
            if (stuck.length) this.showError('not recorded for: ' + stuck.join(', '));
        } catch (error) {
            console.error('Launchpad: decline failed:', error);
            this.showError('that answer was NOT recorded: ' + error.message);
        }
        await this.loadAttributionPrompt();
    }

    /**
     * Fetch live filesystem presence for every DB-tracked project and
     * index it by raw config path for renderProjectList() to consult.
     *
     * Non-fatal: a failed fetch (server datastore unreachable, network
     * hiccup) clears the map rather than throwing, so every project
     * renders as 'unchecked' (normal, actions allowed) rather than the
     * whole launchpad erroring out over a presence sidecar. This is a
     * deliberate choice not to invent a worse verdict than "could not
     * ask" - the same three-outcome discipline the server side applies,
     * mirrored here: failing to LOAD presence is not evidence anything
     * is missing or unreachable.
     */
    /**
     * Fetch which source the project list came from, and any DB/config
     * disagreement, for the banner renderProjectList() draws.
     *
     * feat/db-is-authoritative. Non-fatal, and its failure is its OWN
     * state rather than an assumption of health: a failed fetch sets
     * `projectAuthority` to null, which the banner renders as "could not
     * determine which source is authoritative" - never as the healthy
     * `db` mode. Assuming health here would reintroduce the exact false
     * green the authority endpoint exists to expose.
     *
     * @returns {Promise<void>}
     */
    async loadProjectAuthority() {
        try {
            this.projectAuthority = await window.API.getProjectsAuthority();
        } catch (error) {
            console.warn('Launchpad: failed to load project authority:', error);
            this.projectAuthority = null;
        }
    }

    /**
     * Build the ONE banner that names where the project list came from.
     *
     * There is exactly one banner, and that is the fix. This method used
     * to draw up to two at once from two independently-formed opinions -
     * a "degraded mode" banner saying config.json's projects were being
     * shown, and a "sources disagree" banner saying the database was
     * authoritative and was what you were seeing. Both rendered together,
     * and they contradicted each other about the one thing a provenance
     * banner exists to state. Worse, the disagreement was computed by
     * comparing a live database read against a CACHED config read, so it
     * announced conflicts that did not exist on disk.
     *
     * Projects are DB-only now, so there is one source, one opinion and
     * one banner. Three cases, and the healthy one draws nothing:
     *
     *   - `db`: the steady state. No badge; an empty list here is a real
     *     measured empty list and the empty-state copy already says so.
     *   - `db_unreadable`: the datastore did not answer. The server's own
     *     message says the empty list means "could not read", not "you
     *     have none", and that writes are refused.
     *   - authority unknown (the fetch itself failed): says so, and
     *     claims nothing in either direction.
     *
     * @returns {string} - HTML, empty string when there is nothing to say.
     */
    _renderProjectAuthorityBannerHtml() {
        const a = this.projectAuthority;
        if (a === null || a === undefined) {
            return `<div class="project-authority-banner project-authority-banner-unknown" data-authority-state="unknown">CANNOT DETERMINE which source these projects came from - the authority check did not answer. This is not a claim that anything is wrong, and not a claim that it is fine.</div>`;
        }

        if (!a.degraded) {
            return '';
        }

        return `<div class="project-authority-banner project-authority-banner-unreadable" data-authority-state="${this._escapeHtml(a.mode)}" data-writable="${a.writable ? 'true' : 'false'}">${this._escapeHtml(a.message || a.mode)}</div>`;
    }

    async loadProjectPresence() {
        this.projectPresence = new Map();
        try {
            const result = await window.API.getProjectsPresence();
            if (result && result.status === 'ok' && Array.isArray(result.projects)) {
                for (const row of result.projects) {
                    this.projectPresence.set(row.raw_path, row);
                    // feat/db-is-authoritative - also index by normalised
                    // root, which is what GET /projects now returns as a
                    // project's identity. Indexing by raw_path alone made
                    // the badge miss whenever the two spellings differed.
                    if (row.root) {
                        this.projectPresence.set(row.root, row);
                    }
                }
            }
        } catch (error) {
            console.warn('Launchpad: failed to load project presence:', error);
        }
    }

    /**
     * Fetch the unified "running sessions" list and repaint the section.
     *
     * Combines two server endpoints:
     *   - ``GET /sessions/attachable`` - external tmux sessions on the
     *     cloude socket, plus cloude-owned sessions NOT currently bound
     *     to an active backend (detached-but-alive).
     *   - ``GET /sessions`` - the currently-active backend, if any. The
     *     server's /attachable filter drops this row to prevent a
     *     self-adopt footgun, so we refetch and merge it in here.
     *
     * Each merged row gains an ``is_active`` flag and the list is sorted:
     * active first, then owned (cloude-created), then external; within
     * each bucket, newest first by ``created_at_epoch``.
     */
    async loadRunningSessions() {
        // Reset the verdict for this poll tick. It is set to "not ok" by
        // either fetch below and consumed by renderRunningSessions().
        this.runningSessionsListing = { ok: true, reason: null, detail: null, sources: [] };
        try {
            const list = await window.API.listAttachableSessions();
            if (Array.isArray(list)) {
                this.runningSessions = list;
            } else {
                // A 200 whose body is not an array is not an empty list,
                // it is an unparseable one. Saying zero here would be
                // the same invented verdict as the catch below.
                this.runningSessions = [];
                this._noteListingUnknown('attachable', 'malformed_response',
                    'the server did not return a session array');
            }
        } catch (err) {
            // THIS IS THE THIRD OUTCOME, NOT A LOG LINE. The previous
            // version of this catch logged loudly and then fell back to
            // `[]`, which is worse than a silent catch: the console tells
            // the truth while the screen renders a dead tmux server as a
            // healthy machine with zero sessions, and the loud log made
            // the problem LOOK solved. A failed probe is the absence of
            // an answer, so the row set stays empty AND the section is
            // marked not-evaluated so the user sees "cannot determine".
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
            // - `auth-required` remains the primary signal.
            if (status === 401) {
                try {
                    window.dispatchEvent(new CustomEvent('cloude:reauth-needed', {
                        detail: { source: 'launchpad.loadRunningSessions' }
                    }));
                } catch (_) { /* non-fatal */ }
            }
            this.runningSessions = [];
            this._noteListingUnknown('attachable',
                this._listingReasonFromError(err, status),
                this._listingDetailFromError(err, status));
        }
        // Augment with EVERY currently-live session, which the server
        // filters out of /sessions/attachable to prevent self-adopt.
        // Multi-session: two tabs can each be on a different session, so
        // we pull the full list via GET /sessions/list (oldest first) and
        // merge each one in, tagging is_active + carrying its session id.
        try {
            let liveSessions = [];
            if (typeof window.API.listSessions === 'function') {
                liveSessions = await window.API.listSessions();
            }
            if (!Array.isArray(liveSessions) || liveSessions.length === 0) {
                // Back-compat fallback: single-session server.
                const current = await window.API.getCurrentSession();
                liveSessions = current ? [current] : [];
            }
            for (const live of liveSessions) {
                const tmuxName = live && live.tmux_session;
                if (!tmuxName) continue;
                // activity_status comes from the server's bulk tmux pane
                // query (src/core/session_status.py) - 'running' | 'idle' |
                // 'dead' | 'unknown'. Never fabricated client-side.
                const liveStatus = (live && live.activity_status) || 'unknown';
                const liveUnread = !!(live && live.unread);
                const liveId = (live.session && live.session.id) || live.id || null;
                const existing = this.runningSessions.find(s => s.name === tmuxName);
                if (existing) {
                    existing.is_active = true;
                    existing.session_id = liveId || existing.session_id;
                    existing.status = liveStatus;
                    existing.unread = liveUnread;
                    existing.created_by_cloude = !!live.created_by_cloude;
                    // feat/agent-family-pills - THREE-OUTCOME family
                    // display. ``agent_family`` is null (not a string)
                    // whenever the server could not determine it -
                    // overwritten unconditionally (never `||`'d against
                    // the previous value) so a session whose wrapper was
                    // deleted mid-session correctly flips to unknown
                    // instead of keeping a stale guess.
                    existing.agent_family = live.agent_family !== undefined ? live.agent_family : null;
                    existing.agent_family_source = live.agent_family_source !== undefined ? live.agent_family_source : null;
                    if (live.pinned_theme) existing.pinned_theme = live.pinned_theme;
                    // The durable row id, so an OPEN session shows the same
                    // "#7" a detached one does. Without this the id appeared
                    // and vanished depending on whether you happened to have
                    // the session open, which reads as a glitch rather than
                    // as two code paths.
                    existing.session_row_id = live.session_row_id !== undefined ? live.session_row_id : null;
                    existing.parent_session_id = live.parent_session_id !== undefined ? live.parent_session_id : null;
                    // The user's chosen name. This merge builds its row
                    // field by field, so anything not copied here simply
                    // does not exist on it - and the renderer then falls
                    // back to the tmux handle. A rename that persisted,
                    // resolved and was served correctly still vanished on
                    // screen for want of this line.
                    if (live.label !== undefined) existing.label = live.label;
                } else {
                    this.runningSessions.unshift({
                        name: tmuxName,
                        // The badge means: did THIS APP CREATE this tmux
                        // session, or did it merely ADOPT one started
                        // outside the app? That is a fact about origin, so
                        // it must not flip when the session is opened or
                        // closed, and it must survive a server restart.
                        //
                        // Never derived here. The server answers it from
                        // its persisted `owned_tmux_sessions` set and ships
                        // it on SessionInfo.created_by_cloude - the same
                        // source AttachableSession uses, so a row merged
                        // from either endpoint agrees. Two previous local
                        // derivations were both wrong: a hardcoded `true`
                        // badged every OPEN session TMUX (open sessions
                        // reach us only here, because /sessions/attachable
                        // filters them out), and an `adopted:`-id-prefix
                        // test badged nearly everything EXTERNAL, because a
                        // server restart re-attaches to still-running tmux
                        // sessions through the adopt path and mints
                        // `adopted:` ids for sessions the server still
                        // owns. The id is not durable; the NAME is.
                        created_by_cloude: !!live.created_by_cloude,
                        created_at_epoch: live.created_at_epoch || 0,
                        window_count: 1,
                        is_active: true,
                        session_id: liveId,
                        status: liveStatus,
                        unread: liveUnread,
                        pinned_theme: live.pinned_theme || null,
                        // feat/agent-family-pills - see the `existing`
                        // branch above for why this is never defaulted
                        // to a guessed string.
                        agent_family: live.agent_family !== undefined ? live.agent_family : null,
                        agent_family_source: live.agent_family_source !== undefined ? live.agent_family_source : null,
                        // See the `existing` branch above.
                        session_row_id: live.session_row_id !== undefined ? live.session_row_id : null,
                        parent_session_id: live.parent_session_id !== undefined ? live.parent_session_id : null,
                        // See the `existing` branch above.
                        label: live.label !== undefined ? live.label : null,
                    });
                }
            }
        } catch (err) {
            // A 404 from GET /sessions IS an answer: there is no active
            // session. Anything else is not - it is a merge that did not
            // run, so the row set below is INCOMPLETE and may be missing
            // every currently-open session. The old bare
            // `// 404 = no active session, fine` treated both the same
            // and let a failed merge render as "these are all your
            // sessions".
            const status = (err && typeof err.status === 'number') ? err.status : null;
            if (status !== 404) {
                console.error('[launchpad] live-session merge failed:',
                    status !== null ? `status=${status}` : '(no status)', err);
                this._noteListingUnknown('live',
                    this._listingReasonFromError(err, status),
                    this._listingDetailFromError(err, status));
            }
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
        // feat/project-session-tree (S8) - the tree needs to know which
        // project (if any) each of the rows just sorted above belongs
        // to, so re-fetch attribution and repaint the tree every time
        // the running-session set changes (poller tick included). Both
        // calls are non-fatal; a failure here degrades the tree to NEEDS
        // ATTENTION, it never throws out of loadRunningSessions().
        await this.loadSessionAttribution();
        this.renderProjectList();
    }

    /**
     * Fetch per-session project attribution (S8) from the datastore and
     * index it for _buildProjectSessionGroups() to consult.
     *
     * Description: THREE-OUTCOME, latched on
     *   ``this.sessionAttributionListingOk``. A successful fetch
     *   populates ``sessionAttribution`` / ``sessionAttributionByInstance``
     *   / ``sessionAttributionAmbiguous`` via ``_resolveSessionAttribution``
     *   and every resolved row's ``project_attribution`` is trusted as
     *   read (including the literal strings ``'none'`` and ``'unknown'``,
     *   which mean different things - see ``_buildProjectSessionGroups``).
     *   A failed fetch clears all three AND sets the flag false, which
     *   is what forces every running session into NEEDS ATTENTION rather
     *   than rendering "no project" for a question that was never asked.
     * Inputs: none.
     * Output: Promise<void>. Mutates ``this.sessionAttribution``,
     *   ``this.sessionAttributionByInstance``,
     *   ``this.sessionAttributionAmbiguous``,
     *   ``this.sessionAttributionListingOk``,
     *   ``this.sessionAttributionListingDetail``.
     */
    async loadSessionAttribution() {
        try {
            const rows = await window.API.listSessionRecords();
            if (!Array.isArray(rows)) {
                this.sessionAttribution = new Map();
                this.sessionAttributionByInstance = new Map();
                this.sessionAttributionAmbiguous = new Set();
                this.sessionRecords = [];
                this.sessionAttributionListingOk = false;
                this.sessionAttributionListingDetail =
                    'the server did not return a session record array';
                return;
            }
            const resolved = this._resolveSessionAttribution(rows);
            this.sessionAttribution = resolved.byName;
            this.sessionAttributionByInstance = resolved.byInstance;
            this.sessionAttributionAmbiguous = resolved.ambiguous;
            this.sessionRecords = rows;
            this.sessionAttributionListingOk = true;
            this.sessionAttributionListingDetail = null;
        } catch (error) {
            console.warn('Launchpad: failed to load session attribution:', error);
            this.sessionAttribution = new Map();
            this.sessionAttributionByInstance = new Map();
            this.sessionAttributionAmbiguous = new Set();
            this.sessionRecords = [];
            this.sessionAttributionListingOk = false;
            this.sessionAttributionListingDetail =
                (error && error.message) || 'the server could not be reached';
        }
    }

    /**
     * Resolve the tmux-identity -> stored-row join used for project
     * attribution, from a raw GET /sessions/records payload.
     *
     * Description: A tmux_name is NOT a stable identity - two rows can
     *   legitimately share one, because a session recreated after its
     *   pane died reuses the name with a new ``tmux_created_epoch``. The
     *   old code built a single ``Map<tmux_name, row>`` over a
     *   newest-first, archived-rows-included list with plain
     *   ``map.set()``, so the OLDEST row in the list won (last write
     *   wins) and an archived row was never excluded - which is how a
     *   stopped-then-archived row shadowed a running session of the same
     *   name and made it vanish from its project entirely (measured live:
     *   row id 3, archived, epoch 1787686975 beat row id 4, running,
     *   epoch 1788016091).
     *
     *   Builds three structures instead of one map:
     *     - ``byInstance``: keyed on the composite instance identity
     *       ``${tmux_name}\u0000${tmux_created_epoch}`` (NUL-separated,
     *       so no tmux name can collide with the key of another). INCLUDES
     *       archived rows on purpose - a caller holding a live session's
     *       own creation epoch (both ``SessionInfo`` and
     *       ``AttachableSession`` carry it, as ``created_at_epoch``; see
     *       the merge in ``loadRunningSessions()``) can resolve its EXACT
     *       row this way, including the case where the user archived
     *       that exact instance's record - the row still comes back and
     *       the caller drops it on ``archived_at``, which is what keeps
     *       "delete this session's record" working without letting an
     *       unrelated archived row from an OLDER instance of the same
     *       name shadow a live one.
     *     - ``byName``: the weaker name-only fallback, for a caller that
     *       cannot supply an epoch (or when the exact instance key is not
     *       found - e.g. legacy rows with no recorded epoch). NEVER
     *       contains an archived row. Among the remaining non-archived
     *       candidates for a name, the row with the newest
     *       ``tmux_created_epoch`` wins - resolved by scanning every
     *       candidate for the true maximum, so the winner does not depend
     *       on the order rows arrived in.
     *     - ``ambiguous``: names where two or more non-archived
     *       candidates could not be ranked (a real tie on the newest
     *       epoch, or a candidate with no epoch recorded at all). Such a
     *       name is deliberately ABSENT from ``byName`` - this is a
     *       COULD-NOT-EVALUATE, and the caller must report it as one
     *       rather than silently picking a row.
     *
     *   A genuine collision inside ``byInstance`` itself (two distinct
     *   rows claiming the identical ``tmux_name`` + ``tmux_created_epoch``
     *   pair, which should not happen but is not assumed impossible) is
     *   treated the same way: the key is dropped from ``byInstance`` so a
     *   lookup reports "not found" rather than an arbitrary pick, and the
     *   caller falls through to ``byName`` / ``ambiguous``.
     * Inputs: rows (SessionRecord[]) - as returned by
     *   GET /sessions/records: newest first, archived rows included.
     * Output: {byName: Map<string, object>, byInstance: Map<string, object>,
     *   ambiguous: Set<string>}
     * Example:
     *   const { byName } = lp._resolveSessionAttribution(rows);
     *   byName.get('cloude_a')  // the SessionRecord for the live instance
     */
    _resolveSessionAttribution(rows) {
        const byInstance = new Map();
        const instanceCollisions = new Set();
        const candidatesByName = new Map();

        for (const row of rows) {
            if (!row || !row.tmux_name) continue;
            const name = row.tmux_name;
            const epoch = (row.tmux_created_epoch === null || row.tmux_created_epoch === undefined)
                ? null
                : row.tmux_created_epoch;

            if (epoch !== null) {
                const instanceKey = name + '\u0000' + epoch;
                if (byInstance.has(instanceKey)) {
                    instanceCollisions.add(instanceKey);
                } else {
                    byInstance.set(instanceKey, row);
                }
            }

            // Archived rows are never a candidate for the name-only
            // fallback - see the docstring above.
            if (row.archived_at) continue;

            if (!candidatesByName.has(name)) candidatesByName.set(name, []);
            candidatesByName.get(name).push({ row, epoch });
        }

        for (const key of instanceCollisions) byInstance.delete(key);

        const byName = new Map();
        const ambiguous = new Set();
        for (const [name, candidates] of candidatesByName) {
            if (candidates.length === 1) {
                byName.set(name, candidates[0].row);
                continue;
            }
            // Two or more non-archived rows share this name. A row with
            // no recorded epoch can never be ranked against another, so
            // its mere presence makes the whole name ambiguous.
            if (candidates.some((c) => c.epoch === null)) {
                ambiguous.add(name);
                continue;
            }
            const maxEpoch = Math.max(...candidates.map((c) => c.epoch));
            const winners = candidates.filter((c) => c.epoch === maxEpoch);
            if (winners.length > 1) {
                ambiguous.add(name);
                continue;
            }
            byName.set(name, winners[0].row);
        }

        return { byName, byInstance, ambiguous };
    }

    /**
     * Record that one of the two session probes did not produce an answer.
     *
     * Description: Latches ``runningSessionsListing.ok`` to false for this
     *   poll tick. Once false it never flips back within the tick - a
     *   second probe succeeding does not un-break the first, because the
     *   row set is still incomplete.
     * Inputs: source (string) - 'attachable' | 'live', which fetch failed.
     *   reason (string) - short machine token, mirrors the server's
     *   TmuxListing reason vocabulary where one is available.
     *   detail (string|null) - human text for the row's second line.
     * Output: undefined. Mutates ``this.runningSessionsListing``.
     * Example: this._noteListingUnknown('attachable', 'timeout', '...');
     */
    _noteListingUnknown(source, reason, detail) {
        if (!this.runningSessionsListing) {
            this.runningSessionsListing = { ok: true, reason: null, detail: null, sources: [] };
        }
        const st = this.runningSessionsListing;
        st.ok = false;
        if (!st.reason) st.reason = reason || 'probe_error';
        if (!st.detail) st.detail = detail || null;
        if (st.sources.indexOf(source) === -1) st.sources.push(source);
    }

    /**
     * Derive a machine-readable reason token from a rejected API call.
     *
     * Description: Prefers the server's own ``listing_reason`` (shipped in
     *   the structured 503 detail from GET /sessions/attachable, preserved
     *   on ``err.detail`` by api.js) so the client repeats the server's
     *   verdict rather than inventing a parallel one. Falls back to the
     *   transport-level facts we do have.
     * Inputs: err (Error) - the rejection. status (number|null) - HTTP
     *   status already parsed by the caller.
     * Output: string - e.g. 'tmux_missing', 'timeout', 'exit_2',
     *   'unauthorized', 'http_500', 'network_error'.
     * Example: this._listingReasonFromError(err, 503) // 'timeout'
     */
    _listingReasonFromError(err, status) {
        const d = err && err.detail;
        if (d && typeof d === 'object' && typeof d.listing_reason === 'string' && d.listing_reason) {
            return d.listing_reason;
        }
        if (status === 401) return 'unauthorized';
        if (typeof status === 'number' && status > 0) return `http_${status}`;
        return 'network_error';
    }

    /**
     * Derive the human explanation shown under a CANNOT DETERMINE row.
     *
     * Description: Same precedence as ``_listingReasonFromError`` - the
     *   server's own ``listing_detail`` or ``message`` wins, because it
     *   knows things the browser cannot (which tmux command failed, what
     *   stderr said). Never returns an empty string; a blank cell is not
     *   an explanation.
     * Inputs: err (Error) - the rejection. status (number|null) - HTTP
     *   status already parsed by the caller.
     * Output: string - one short sentence.
     * Example: this._listingDetailFromError(err, 0) // 'the server could not be reached'
     */
    _listingDetailFromError(err, status) {
        const d = err && err.detail;
        if (d && typeof d === 'object') {
            if (typeof d.listing_detail === 'string' && d.listing_detail) return d.listing_detail;
            if (typeof d.message === 'string' && d.message) return d.message;
        }
        if (status === 401) return 'sign in again to see your sessions';
        if (status === 503) return 'the server could not read the tmux session list';
        if (typeof status === 'number' && status > 0) return `the server answered HTTP ${status}`;
        if (err && typeof err.message === 'string' && err.message) return err.message;
        return 'the server could not be reached';
    }

    /**
     * Build the NEEDS ATTENTION block shown when a probe did not answer.
     *
     * Description: The visible half of the three-outcome rule. It is a
     *   plain informational row and deliberately carries NO action
     *   controls - no close, no remove, no restart. An action against a
     *   session whose existence we cannot confirm either does nothing or
     *   does something to the wrong thing, and offering it would tell the
     *   user we know more than we do.
     * Inputs: none (reads ``this.runningSessionsListing``).
     * Output: string - HTML, or '' when the listing is fine.
     * Example: lp._renderListingAttentionHtml()
     */
    _renderListingAttentionHtml() {
        const st = this.runningSessionsListing;
        if (!st || st.ok) return '';
        const reason = this._escapeHtml(st.reason || 'probe_error');
        const detail = this._escapeHtml(
            st.detail || 'the server could not be reached');
        const which = st.sources && st.sources.length
            ? this._escapeHtml(st.sources.join(' + '))
            : 'session';
        return `
                <div class="running-sessions-attention" role="status" data-listing-ok="0" data-listing-reason="${reason}">
                  <div class="running-sessions-attention__head">NEEDS ATTENTION</div>
                  <div class="running-sessions-attention__title">CANNOT DETERMINE which sessions are running</div>
                  <div class="running-sessions-attention__detail">${detail} (${which} probe, ${reason})</div>
                  <div class="running-sessions-attention__note">any sessions listed below may be incomplete, and none are shown as stopped</div>
                </div>
            `;
    }

    /**
     * Paint the count line beside the "running sessions" heading.
     *
     * Description: The heading must never assert a number the app did not
     *   measure. When a probe failed it says so in words instead, which
     *   is the difference between "you have no sessions" and "I could not
     *   find out".
     * Inputs: none (reads ``this.runningSessions`` and
     *   ``this.runningSessionsListing``).
     * Output: undefined. Writes ``#running-sessions-count`` textContent.
     * Example: lp._updateRunningSessionsCount()
     */
    _updateRunningSessionsCount() {
        const el = document.getElementById('running-sessions-count');
        if (!el) return;
        const st = this.runningSessionsListing;
        if (st && !st.ok) {
            el.textContent = 'count could not be determined';
            el.setAttribute('data-listing-ok', '0');
            return;
        }
        const n = (this.runningSessions || []).length;
        el.textContent = n === 1 ? '1 running' : `${n} running`;
        el.setAttribute('data-listing-ok', '1');
    }

    /**
     * Paint (or hide) the Running Sessions section. Hides via display:none
     * when empty - opacity:0 would still capture clicks, which we don't want.
     *
     * Click handlers (row → return/adopt, X → kill) land in Task 10; this
     * pass only builds the DOM. ``data-name`` / ``data-active`` attributes
     * are the hooks event delegation will use.
     */
    /**
     * The durable row id, bottom-right of a session box, with its parent
     * when the session is a fork.
     *
     * Description: renders ``#7`` normally and ``#7 ← #3`` for a fork, so
     *   a fork tree can be followed by eye across screens. The id is
     *   ``sessions.id``, which is what ``parent_session_id`` points at -
     *   showing anything else here (the uuid, the tmux name) would give
     *   the user a number that does not match the link it is meant to
     *   help them follow.
     *
     *   RENDERS NOTHING when there is no id, and that is deliberate. An
     *   EXTERNAL tmux session the app never created genuinely has no row.
     *   Printing "#?" or "#0" there would be inventing an identity for a
     *   session we have no record of, which is worse than a blank corner.
     * Inputs: s (object) - a running-session row from /sessions/attachable.
     * Output: string - HTML, possibly empty.
     * Example: lp._renderSessionIdHtml({session_row_id: 7, parent_session_id: 3})
     */
    _renderSessionIdHtml(s) {
        const id = s && s.session_row_id;
        if (id === null || id === undefined || id === '') return '';
        const parent = s.parent_session_id;
        const forked = (parent !== null && parent !== undefined && parent !== '')
            ? `<span class="running-session-fork-of" title="forked from session #${this._escapeHtml(String(parent))}">← #${this._escapeHtml(String(parent))}</span>`
            : '';
        return `<span class="running-session-id" title="session id ${this._escapeHtml(String(id))}">#${this._escapeHtml(String(id))}${forked}</span>`;
    }

    renderRunningSessions() {
        const container = document.getElementById('running-sessions-list');
        if (!container) return;
        const section = document.getElementById('running-sessions-section');
        const listing = this.runningSessionsListing || { ok: true, reason: null };
        const attentionHtml = this._renderListingAttentionHtml();
        if (!this.runningSessions || this.runningSessions.length === 0) {
            // ZERO ROWS IS TWO DIFFERENT SITUATIONS. With a listing that
            // ran, it means the user has no sessions and the section
            // hides, as it always has. With a listing that did NOT run it
            // means we do not know, and hiding the section would render
            // "cannot determine" as "nothing to see" - the exact false
            // green this whole change exists to remove. So the unknown
            // case SHOWS the section carrying only the attention block.
            if (!listing.ok) {
                const unknownSig = `unknown:${listing.reason || ''}:${listing.detail || ''}`;
                if (this._lastRunningSig !== unknownSig) {
                    this._lastRunningSig = unknownSig;
                    if (section) section.style.display = '';
                    container.innerHTML = attentionHtml;
                }
                this._updateRunningSessionsCount();
                return;
            }
            // Only rewrite the DOM when transitioning into the empty state -
            // repeated renders while already empty would thrash the
            // section's display flip for no reason.
            if (this._lastRunningSig !== 'empty') {
                this._lastRunningSig = 'empty';
                if (section) section.style.display = 'none';
                container.innerHTML = '';
            }
            this._updateRunningSessionsCount();
            return;
        }
        // Signature-diff: skip the innerHTML rewrite when the set of rows
        // (name + ownership + active flag) hasn't changed. Previously the
        // 5s poller was restarting the `.running-session-row` pulse-glow
        // CSS animations every tick, which visibly flickered. Age labels
        // still need updating each tick, so we punt those through a
        // cheap text-only DOM update instead.
        const sig = JSON.stringify({
            listing: [listing.ok, listing.reason || '', listing.detail || ''],
            rows: this.runningSessions.map(s => ({
                name: s.name,
                label: s.label || null,
                owned: !!s.created_by_cloude,
                active: !!s.is_active,
                sid: s.session_id || null,
                status: s.status || 'unknown',
                unread: !!s.unread,
                fam: s.agent_family || null,
                famSrc: s.agent_family_source || null,
            })),
        });
        if (sig === this._lastRunningSig) {
            this._updateRunningSessionAges();
            this._updateRunningSessionsCount();
            return;
        }
        this._lastRunningSig = sig;
        if (section) section.style.display = '';
        container.innerHTML = attentionHtml + this.runningSessions.map(s => {
            const owned = !!s.created_by_cloude;
            const displayName = this._sessionDisplayLabel(s);
            const ageStr = s.created_at_epoch ? this._formatRelativeTime(s.created_at_epoch) : '';
            const escapedName = this._escapeHtml(s.name);
            const escapedDisplay = this._escapeHtml(displayName);
            const sidAttr = s.session_id ? ` data-session-id="${this._escapeHtml(s.session_id)}"` : '';
            // Pencil rename button, in one of three states - never absent.
            // See _renderRenamePencilHtml for why omitting it was the bug.
            const renamePencil = this._renderRenamePencilHtml(s, escapedName);
            // Fork is offered on OWNED sessions only. An external tmux
            // session has no row of ours and therefore no recorded Claude
            // conversation to resume, so the server would refuse it with
            // a 409 - better not to paint a control that cannot work.
            const forkBtn = owned
                ? `<button type="button" class="running-session-fork" data-fork-name="${escapedName}" title="copy this conversation into a new session and open it - this session is not changed. Note: Claude Code&#39;s own /fork runs the copy in the BACKGROUND and leaves you here; this button behaves like its /branch." aria-label="fork this session into a new one">fork</button>`
                : '';
            // Status dot: real activity status (running/idle/dead/unknown)
            // via the shared SessionStatusUI helper (client/js/session-status-ui.js),
            // NOT the old ownership-colored placeholder. title + aria-label
            // on the dot itself so the state is never color-only.
            const statusDot = window.SessionStatusUI
                ? window.SessionStatusUI.dotHtml(s.status)
                : '';
            const markUnread = window.SessionStatusUI
                ? window.SessionStatusUI.markUnreadHtml(s.name, !!s.unread)
                : '';
            // X (close) on a running row, trash (remove) on a stopped one,
            // never both - built by the shared SessionRowActions module so
            // the launcher, the conversation sidebar, and any future
            // session surface draw the same glyph with the same tooltip
            // for the same meaning. See client/js/session-row-actions.js.
            const rowAction = window.SessionRowActions
                ? window.SessionRowActions.html(s.status, s.name, 'running-session-kill')
                : '';
            // Empty string for a session with no theme, an unknown theme,
            // or a registry that has not loaded yet - all three render as
            // the row always has. See client/js/session-theme-tint.js.
            const themeAttrs = window.SessionThemeTint
                ? window.SessionThemeTint.attrs(s.pinned_theme)
                : '';
            // The session-theme cue. The SAME element the sidebar row
            // renders, in the same place relative to the name, because
            // one rule used to paint both surfaces and moving the cue on
            // one alone would relocate the collision rather than remove
            // it. See client/js/session-theme-tint.js.
            const themeSwatch = window.SessionThemeTint
                ? window.SessionThemeTint.swatchHtml(s.pinned_theme)
                : '';
            return `
                <div class="running-session-row ${owned ? 'owned' : 'external'}" data-name="${escapedName}" data-active="${s.is_active ? '1' : '0'}"${sidAttr}${themeAttrs}>
                  <div class="running-session-top">
                    ${statusDot}
                    <span class="running-session-name">${escapedDisplay}</span>
                    ${themeSwatch}
                    ${renamePencil}
                    ${forkBtn}
                    ${markUnread}
                    ${rowAction}
                  </div>
                  <div class="running-session-badges">
                    <span class="badge ${owned ? 'badge-tmux' : 'badge-external'}">${owned ? 'TMUX' : 'EXTERNAL'}</span>
                    ${this._renderFamilyPillHtml(s.agent_family, s.agent_family_source)}
                    ${ageStr ? `<span class="running-session-age">${this._escapeHtml(ageStr)}</span>` : ''}
                    ${this._renderSessionIdHtml(s)}
                  </div>
                </div>
            `;
        }).join('');
        this._updateRunningSessionsCount();
        // Idempotent - re-calling after subsequent renders is a no-op
        // because the listener is bound to the (stable) container element,
        // not the (re-painted) row children, and the flag gates re-bind.
        this._bindRunningSessionClicks();
    }

    /**
     * Fetch the RECENT group (S9) from ``GET /sessions/recent`` and render it.
     *
     * Description: datastore-backed, NOT a live tmux probe - this is the
     *   first launcher surface that reads stored history rather than
     *   re-asking tmux. Failure is non-fatal: logged and rendered as the
     *   'probe_unavailable'-shaped attention block via
     *   ``renderRecentSessions()``, never silently dropped.
     * Inputs: none.
     * Output: Promise<void>. Sets ``this.recentSessionsState`` /
     *   ``this.recentSessions`` and calls ``renderRecentSessions()``.
     */
    async loadRecentSessions() {
        try {
            const payload = await window.API.listRecentSessions();
            this.recentSessionsState = payload && payload.state || 'probe_unavailable';
            this.recentSessions = (payload && payload.sessions) || [];
            this.recentSessionsNotice = (payload && payload.notice) || null;
        } catch (error) {
            console.warn('Launchpad: failed to load recent sessions:', error);
            this.recentSessionsState = 'probe_unavailable';
            this.recentSessions = [];
            this.recentSessionsNotice = 'recent sessions could not be loaded: '
                + (error && error.message ? error.message : 'the server could not be reached');
        }
        this.renderRecentSessions();
    }

    /**
     * Build one RECENT row's HTML.
     *
     * THREE-OUTCOME RESTART GATE, enforced HERE, not only by the server
     * query that populated ``row``. ``GET /sessions/recent`` only ever
     * returns ``lifecycle='stopped'`` rows, but this function checks
     * ``row.lifecycle`` itself and refuses to emit a RESTART control for
     * anything else - a row whose lifecycle is 'unknown' (or any value
     * other than 'stopped') must never offer to restart it: restarting a
     * session whose state could not be confirmed is how you get two of
     * the same session running at once. Belt-and-suspenders on purpose -
     * see this file's CLAUDE.md "assert every guarantee at the layer
     * that enforces it".
     * Inputs: row (object) - one ``SessionRecord`` from the wire.
     * Output: string - HTML for one ``.recent-session-row``.
     */
    _renderRecentSessionRowHtml(row) {
        const uuid = this._escapeHtml(row.session_uuid || '');
        // ``title`` IS THE LABEL NOW, so it leads. It used to sit third
        // in this chain as a last-ditch fallback, which was correct while
        // it was a lineage-seeded string and is exactly backwards once it
        // carries what the user called the session.
        const displayName = this._escapeHtml(
            (row.title && String(row.title).trim())
                || (row.tmux_name && this._deriveRunningSessionDisplayName(row.tmux_name))
                || row.working_dir
                || 'session'
        );
        const lifecycle = row.lifecycle || 'unknown';
        const canRestart = lifecycle === 'stopped';
        const restartBtn = canRestart
            ? `<button type="button" class="recent-session-restart" data-uuid="${uuid}" data-working-dir="${this._escapeHtml(row.working_dir || '')}" data-agent-type="${this._escapeHtml(row.agent_type || '')}">restart</button>`
            : '';
        const lifecycleLabel = canRestart ? 'ENDED' : this._escapeHtml(lifecycle);
        // THE SAME ENDED SIGNAL THE TREE USES, not a second vocabulary.
        // This row already said "stopped" in words; the shared dot is
        // added so a session looks the same whichever surface drew it,
        // which is the whole complaint this change answers. The dot is
        // only shown for a row we can actually call ended - a lifecycle
        // we could not evaluate keeps its own word and gets no dot
        // asserting a state nobody measured.
        const statusDot = (canRestart && window.SessionStatusUI)
            ? window.SessionStatusUI.dotHtml('stopped')
            : '';
        // Delete is offered on EVERY row here, including one whose
        // lifecycle is unknown: hiding a row from your own list is safe
        // whatever state it is in, unlike restart, which is gated above.
        const deleteBtn = `<button type="button" class="ended-session-delete" data-uuid="${uuid}" title="delete this session from your lists (the record is kept)" aria-label="delete this session from your lists">delete</button>`;
        return `
                <div class="recent-session-row" data-uuid="${uuid}" data-lifecycle="${this._escapeHtml(lifecycle)}">
                  ${statusDot}
                  <span class="recent-session-name">${displayName}</span>
                  <span class="recent-session-lifecycle">${lifecycleLabel}</span>
                  ${restartBtn}
                  ${deleteBtn}
                </div>
            `;
    }

    /**
     * Paint (or hide) the RECENT section.
     *
     * THREE-OUTCOME RULE applied to the whole group. ``state !== 'ok'``
     * (probe never ran, or the last one failed) renders an explicit
     * "cannot determine" block and ZERO rows - never the stored rows
     * shown as if they were freshly confirmed, and never a silent empty
     * section indistinguishable from "no history". ``state === 'ok'``
     * with zero rows is the ordinary "nothing stopped" case and hides
     * the section, matching the running-sessions convention.
     * Inputs: none (reads ``this.recentSessionsState`` /
     *   ``this.recentSessions`` / ``this.recentSessionsNotice``).
     * Output: undefined. Writes ``#recent-sessions-list`` innerHTML.
     */
    renderRecentSessions() {
        const container = document.getElementById('recent-sessions-list');
        if (!container) return;
        const section = document.getElementById('recent-sessions-section');
        const countEl = document.getElementById('recent-sessions-count');
        const state = this.recentSessionsState || 'never_probed';
        const rows = this.recentSessions || [];

        if (state !== 'ok') {
            const notice = this._escapeHtml(
                this.recentSessionsNotice || 'recent sessions CANNOT BE DETERMINED'
            );
            if (section) section.style.display = '';
            if (countEl) {
                countEl.textContent = 'cannot determine';
                countEl.setAttribute('data-state', state);
            }
            container.innerHTML = `
                <div class="recent-sessions-attention" role="status" data-state="${this._escapeHtml(state)}">
                  <div class="recent-sessions-attention__title">CANNOT DETERMINE recent sessions</div>
                  <div class="recent-sessions-attention__detail">${notice}</div>
                </div>
            `;
            return;
        }

        if (countEl) {
            countEl.textContent = rows.length === 1 ? '1 recent' : `${rows.length} recent`;
            countEl.setAttribute('data-state', 'ok');
        }
        if (rows.length === 0) {
            if (section) section.style.display = 'none';
            container.innerHTML = '';
            return;
        }
        if (section) section.style.display = '';
        container.innerHTML = rows.map(row => this._renderRecentSessionRowHtml(row)).join('');
        this._bindRecentSessionClicks();
    }

    /**
     * Wire RESTART clicks on RECENT rows, via event delegation on the
     * (stable) list container - same pattern as running-sessions clicks.
     * Guarded by a flag so repeated renders don't stack listeners.
     */
    _bindRecentSessionClicks() {
        const container = document.getElementById('recent-sessions-list');
        if (!container || container.dataset.recentClickBound === '1') return;
        container.dataset.recentClickBound = '1';
        container.addEventListener('click', (ev) => {
            const del = ev.target.closest && ev.target.closest('.ended-session-delete');
            if (del) {
                this._deleteSessionRecord(del.getAttribute('data-uuid'));
                return;
            }
            const btn = ev.target.closest && ev.target.closest('.recent-session-restart');
            if (!btn) return;
            this._restartRecentSession(
                btn.getAttribute('data-working-dir'),
                btn.getAttribute('data-agent-type')
            );
        });
    }

    /**
     * DELETE one stored session from every listing. Keep the record.
     *
     * Description: the ONE handler behind every delete control on this
     *   screen - RECENT rows and ended tree rows both route here, so the
     *   two cannot drift into meaning different things, which is the
     *   class of bug this whole change is repairing.
     *
     *   IT DOES NOT KILL ANYTHING. The X/close control on a RUNNING row
     *   is a different verb: it stops the process and removes the
     *   session's uploads bucket from the project folder. This only
     *   stamps ``archived_at`` server-side. There is deliberately no
     *   confirm dialog: nothing is destroyed, the row is retained, and
     *   the app has a standing rule that confirm copy must name real
     *   consequences - a dialog warning about nothing teaches people to
     *   click through the ones that matter.
     *
     *   REFRESHES BOTH SURFACES, not just the one clicked. Deleting from
     *   RECENT while the tree still showed the row would recreate the
     *   exact contradiction this change removes.
     * Inputs: sessionUuid (string) - the stored row's ``session_uuid``.
     * Output: Promise<void>.
     * Example: await lp._deleteSessionRecord('a1b2-c3');
     */
    /**
     * FORK a running session into a new one that branches its Claude
     * conversation.
     *
     * Description: the parent is NOT changed by this. It keeps running,
     *   stays listed, stays resumable and can be forked again - there is
     *   no "was forked from" state anywhere, because the process was
     *   never touched. The new session is labelled with "(fork)" appended
     *   and renaming it afterwards is the user's job.
     *
     *   THREE OUTCOMES ARE SURFACED, not two. A 409 means the session has
     *   no recorded Claude conversation to resume, which is a REFUSAL and
     *   is reported as one - forking anyway would start a brand new
     *   conversation wearing a fork label and the user would believe they
     *   had branched their work. And a fork that succeeds while its parent
     *   link fails to land says so, rather than claiming a clean success:
     *   the session works, it is simply not linked in the tree.
     * Inputs: sessionName (string) - the PARENT's tmux session name.
     * Output: Promise<void>.
     * Example: await lp._forkSession('cloude_work');
     */
    async _forkSession(sessionName) {
        if (!sessionName) {
            this.showError('cannot fork: this row carries no session name');
            return;
        }
        let result;
        try {
            result = await window.API.forkSession(sessionName);
        } catch (error) {
            const status = error && error.status;
            if (status === 409) {
                this.showError(
                    'cannot fork: this session has no Claude conversation yet, '
                    + 'so there is nothing to branch from'
                );
            } else {
                console.error('Launchpad: fork failed:', error);
                this.showError(
                    'failed to fork session: '
                    + ((error && error.message) || 'the server could not be reached')
                );
            }
            return;
        }
        if (result && result.lineage_recorded === false) {
            // Not an error and not a clean success. Say exactly what is
            // true: the fork exists and works, the link did not land.
            this.showError(
                result.detail
                || 'forked, but the link back to the parent was not recorded'
            );
        }
        await this.loadSessionAttribution();
        await this.loadRecentSessions();
        this.renderProjectList();
    }

    async _deleteSessionRecord(sessionUuid) {
        if (!sessionUuid) {
            // No id means we do not know WHICH row was asked for, and a
            // delete aimed at nothing must say so rather than quietly
            // doing nothing and looking like it worked.
            this.showError('cannot delete: this row carries no session id');
            return;
        }
        try {
            await window.API.deleteSessionRecord(sessionUuid);
        } catch (error) {
            console.error('Launchpad: delete of session record failed:', error);
            this.showError(
                'failed to delete session: '
                + ((error && error.message) || 'the server could not be reached')
            );
            return;
        }
        await this.loadSessionAttribution();
        await this.loadRecentSessions();
        this.renderProjectList();
    }

    /**
     * RESTART a stopped RECENT session: launch a fresh session in the
     * same working directory (and agent type, when known). This is
     * deliberately NOT a resurrection of the dead tmux instance (that
     * process and its pane are gone) - it is a new session, the same
     * action the "new console" FAB performs, seeded from the stopped
     * row's own metadata.
     * Inputs: workingDir (string), agentType (string|'').
     * Output: Promise<void>.
     */
    async _restartRecentSession(workingDir, agentType) {
        try {
            const payload = { working_dir: workingDir || undefined };
            if (agentType) payload.agent_type = agentType;
            await window.API.createSession(payload);
            await this.loadRunningSessions();
            await this.loadRecentSessions();
        } catch (error) {
            console.error('Launchpad: restart of recent session failed:', error);
            this.showError('failed to restart session: ' + (error && error.message ? error.message : 'unknown error'));
        }
    }

    /**
     * Text-only age refresh - walks existing rows and rewrites just the
     * ``.running-session-age`` textContent. Used on poll ticks when the
     * row set is unchanged so we avoid the innerHTML rewrite that would
     * restart the pulse-glow CSS animations.
     *
     * Guarded for all the obvious missing-data cases: row without a
     * data-name, session no longer in the list, session without an
     * epoch, row without an age element. Any miss is a silent skip -
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
     * Description: render the rename pencil for one running-session row in
     *   one of THREE states, and never omit it.
     *
     *   THE BUG THIS FIXES. The pencil used to be gated on
     *   ``s.session_id`` alone, and a row without one simply had no
     *   pencil. But the badge in the same row is gated on
     *   ``created_by_cloude``, and those two fields answer DIFFERENT
     *   questions: ``created_by_cloude`` is about ORIGIN (did this app
     *   create or adopt this session - durable, name-keyed, survives a
     *   restart), while ``session_id`` is only populated by the
     *   ``/sessions/list`` merge and therefore really means "is this
     *   session currently open right now". A session this app owns but
     *   which is not currently open is badged TMUX and had no pencil,
     *   which is exactly what the user reported: a row that says TMUX,
     *   says it is his, and offers no way to rename it.
     *
     *   Silently removing a control is the worst available answer,
     *   because an absent affordance is indistinguishable from a broken
     *   one - the user cannot tell "you may not do this" from "this app
     *   forgot to draw the button". So the control is always drawn, and
     *   when it cannot act it says why:
     *
     *     1. RENAMEABLE  - ``session_id`` known. Live pencil, unchanged
     *        behaviour, carries ``data-rename-sid`` and is the only state
     *        the click handler's ``.running-session-rename`` selector can
     *        match.
     *     2. UNAVAILABLE - no ``session_id``, but ownership IS known. We
     *        can state the precondition precisely, because the rename
     *        endpoint is keyed on a session id we do not currently hold:
     *        open it (ours) or adopt it (external) and rename becomes
     *        available. Drawn dimmed with the reason in ``title`` and in
     *        ``aria-label``, so the reason is available to a screen
     *        reader and not only to a hovering mouse.
     *     3. CANNOT DETERMINE - no ``session_id`` AND ownership is null.
     *        ``created_by_cloude`` is genuinely nullable: server_status.py
     *        fills it with ``ownership_by_name.get(name)``, which yields
     *        None for a name the ownership map never answered for. That is
     *        not "external", and this state does not pretend it is.
     *
     *   States 2 and 3 use a DIFFERENT class from state 1
     *   (``running-session-rename-unavailable``), which is what keeps them
     *   out of the live click path: the delegated handler selects on
     *   ``.running-session-rename`` and a class token is matched whole, so
     *   an unavailable pencil can never reach the rename call no matter
     *   what data attributes it carries. It carries none.
     *
     * Inputs:
     *   s (object) - the row's session record. Reads ``session_id``
     *     (string|null) and ``created_by_cloude`` (boolean|null).
     *   escapedName (string) - the row's tmux name, ALREADY HTML-escaped
     *     by the caller. Not re-escaped here.
     * Output: string - exactly one ``<span>`` element. Never empty.
     * Example: this._renderRenamePencilHtml({session_id: null,
     *   created_by_cloude: true}, 'cloude_fs2')
     *   -> '<span class="running-session-rename-unavailable" ...>'
     */
    _renderRenamePencilHtml(s, escapedName) {
        const pencil = window.SessionStatusUI ? window.SessionStatusUI.pencilIconSvg() : '';
        // A TMUX NAME IS ENOUGH NOW. This required `s.session_id` - an
        // app session id the manager only holds for an OPEN session - so
        // every row the app had not adopted showed a dead pencil,
        // including a user's own running sessions, all of them at once
        // after a restart. "Open it first, then you may rename it" is not
        // a rule anybody would choose; it was the rename endpoint's shape
        // leaking into the UI.
        //
        // That shape was real for the ORIGINAL design, where renaming
        // meant typing into the pane: you cannot type into a backend you
        // do not hold. Rename is out-of-band now - it keys the row by
        // tmux name and addresses the conversation by claude uuid, and
        // needs neither the session open nor the pane touched. So the
        // precondition is gone and the pencil follows the name.
        const renameKey = s.session_id || s.tmux_session || s.name;
        if (renameKey) {
            return `<span class="running-session-rename" role="button" aria-label="rename session"`
                + ` data-rename-sid="${this._escapeHtml(renameKey)}"`
                + ` data-rename-name="${escapedName}" title="rename session">${pencil}</span>`;
        }
        // Ownership is a THREE-valued field here. `== null` catches both
        // null and undefined and nothing else, deliberately: `!s.x` would
        // fold the genuine unknown into "external" and invent an answer.
        const reason = s.created_by_cloude == null
            ? 'rename unavailable: CANNOT DETERMINE whether this session is yours,'
                + ' so whether it can be renamed is unknown'
            : (s.created_by_cloude
                ? 'rename unavailable until this session is open - click the row to open it'
                : 'rename unavailable until this session is adopted - click the row to adopt it');
        return `<span class="running-session-rename-unavailable" aria-disabled="true"`
            + ` aria-label="${this._escapeHtml(reason)}"`
            + ` title="${this._escapeHtml(reason)}">${pencil}</span>`;
    }

    /**
     * Render the agent-family pill for a running-session row.
     *
     * feat/agent-family-pills - THREE-OUTCOME RULE applied to the family
     * badge (see repo CLAUDE.md). ``agentFamily`` is the resolved family
     * name (e.g. "codex") or null/undefined when
     * ``resolve_family_for_display`` (src/core/agent_families.py) could
     * not determine it - NEVER a collapsed guess of "claude". A null
     * family renders literally as "unknown family", never as any family
     * name, and never silently as nothing.
     *
     * A GUESS AND A FACT MUST NOT LOOK IDENTICAL. ``agentFamilySource``
     * of "fingerprint" or "derived_deepest" means the value was reached
     * by inference (scrollback heuristic, or an extra hop past a wrapper
     * with no recorded family) rather than read directly off a stored
     * choice ("wrapper" / "reserved_name") - those two render with the
     * ``family-pill--guess`` class (dashed border, see styles.css)
     * instead of ``family-pill--fact`` (solid), so the two are visually
     * distinguishable at a glance, not just in a title attribute a user
     * has to hover to find.
     *
     * Inputs:
     *   agentFamily (string|null|undefined) - resolved family name.
     *   agentFamilySource (string|null|undefined) - one of 'wrapper' |
     *     'reserved_name' | 'fingerprint' | 'derived_deepest' | 'unknown'.
     * Output: string - one ``<span class="family-pill ...">`` element.
     * Example: this._renderFamilyPillHtml('codex', 'wrapper')
     *   -> '<span class="family-pill family-pill--fact" ...>codex</span>'
     */

    _renderFamilyPillHtml(agentFamily, agentFamilySource) {
        const source = agentFamilySource || 'unknown';
        const isGuess = source === 'fingerprint' || source === 'derived_deepest';
        const known = !!agentFamily && source !== 'unknown';
        const label = known ? agentFamily : 'unknown family';
        const kindClass = !known
            ? 'family-pill--unknown'
            : (isGuess ? 'family-pill--guess' : 'family-pill--fact');
        const title = known
            ? (isGuess
                ? `guessed from session output (${source})`
                : `agent family: ${agentFamily}`)
            : 'could not determine which agent this session is running';
        return `<span class="family-pill ${kindClass}" data-family-source="${this._escapeHtml(source)}" title="${this._escapeHtml(title)}">${this._escapeHtml(label)}</span>`;
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
     * The string a HUMAN should see for one session row.
     *
     * A session's NAME IS A LABEL. It is free-form - spaces,
     * punctuation, mixed case - and it lives on the row as
     * ``sessions.title``, arriving here as ``label``. The tmux session
     * name is an internal handle derived from that label once, at
     * creation, and never moved again. Keeping them separate is what
     * stops a rename from moving the field session identity is keyed
     * on, which used to split one session into two rows.
     *
     * FALLBACK IS THE OLD BEHAVIOUR, EXACTLY. A row with no label - any
     * session created before labels existed, and any external session
     * this app never made - renders the ``cloude_``-stripped tmux name
     * just as it always did. An empty-string label counts as no label:
     * rendering blank would be worse than rendering the handle.
     *
     * Inputs: row (object) - a running-session row; reads ``label`` and
     *   ``name``.
     * Output: string - the display name, never empty when a name exists.
     * Example: this._sessionDisplayLabel({name: 'cloude_Media',
     *            label: 'Media Compression'})  -> 'Media Compression'
     */
    _sessionDisplayLabel(row) {
        // DELEGATED, NOT DUPLICATED. This method used to hold its own
        // copy of the fallback chain, which was fine while the launchpad
        // was the only surface rendering a session name. It is not: the
        // tab title, the in-page header, a toast card and the attribution
        // prompt all ask the same question, and four separate answers to
        // it would drift silently. The rule lives in
        // client/js/session-label.js now and this is a thin caller.
        //
        // A row with neither a label nor a name cannot be named at all,
        // which is a thing to SAY rather than a blank cell to render.
        if (window.SessionLabel) {
            return window.SessionLabel.resolve(row) || window.SessionLabel.UNKNOWN;
        }
        // session-label.js is loaded before this file by client/index.html.
        // If it somehow is not, fall back to the old inline chain rather
        // than rendering nothing - a missing script must not blank every
        // session name on the home screen.
        const label = row && typeof row.label === 'string'
            ? row.label.trim()
            : '';
        if (label) return label;
        return this._deriveRunningSessionDisplayName(row && row.name);
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
     * technically user-controlled - any embedded `<`, `>`, `"`, `'`, `&`
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
     * Jump straight into an already-active running session's terminal.
     *
     * Description: extracted from the running-sessions row click handler
     *   (Task 5 / deep-link fix) so the SAME pre-fit + scrollback-capture
     *   dance is reachable from both a mouse click and a resolved
     *   `/session/<name>` deep link - the two paths must not drift apart.
     *   Pre-shows the terminal screen, measures THIS client's true
     *   cols/rows via a fit BEFORE the capture (a mismatch garbles
     *   older scrollback on a differently-sized client), then fetches
     *   the session with scrollback and hands off to
     *   `App.returnToExistingTerminal()`.
     * Inputs:
     *   rowSessionId (string|null) - server-side session id of the
     *     already-active backend to jump to.
     * Output: Promise<void>. Shows a launchpad error on failure.
     */
    async _returnToActiveRunningSession(rowSessionId) {
        try {
            // 1. Pre-show terminal screen so xterm can measure layout.
            //    hideAllScreens lives on window.App; the optional
            //    chain guards against an early-boot race where App
            //    isn't fully constructed yet (shouldn't happen on a
            //    user-triggered click, but it's free defense).
            window.App && window.App.hideAllScreens
                ? window.App.hideAllScreens()
                : document.querySelectorAll('.screen').forEach((s) =>
                      s.classList.remove('active')
                  );
            const termScreen = document.getElementById('terminal-screen');
            if (termScreen) termScreen.classList.add('active');
            // 2. First-time init of xterm if the user never visited
            //    the terminal this page load (e.g. refresh on launchpad).
            if (window.TerminalController && !window.TerminalController.term) {
                await window.TerminalController.init();
            }
            // 3. Yield two animation frames so the layout actually
            //    flushes before fitAddon measures the container.
            await new Promise((r) =>
                requestAnimationFrame(() => requestAnimationFrame(r))
            );
            // 4. Fit + read measured geometry. Wrap in try/catch -
            //    fit can throw if the container isn't laid out yet;
            //    we tolerate and fall back to 0 (server treats 0 as
            //    "skip pre-resize").
            let cols = 0;
            let rows = 0;
            try {
                if (
                    window.TerminalController &&
                    window.TerminalController.fitAddon &&
                    typeof window.TerminalController.fitAddon.fit === 'function'
                ) {
                    window.TerminalController.fitAddon.fit();
                }
                cols =
                    (window.TerminalController &&
                        window.TerminalController.term &&
                        window.TerminalController.term.cols) ||
                    0;
                rows =
                    (window.TerminalController &&
                        window.TerminalController.term &&
                        window.TerminalController.term.rows) ||
                    0;
            } catch (fitErr) {
                // Tolerated - fall through with 0/0; server skips
                // the pre-resize and behavior is identical to the
                // pre-fix path for THIS request (same-width clients
                // are unaffected anyway).
                console.warn('rejoin pre-fit failed', fitErr);
            }

            const info = await window.API.getSession(rowSessionId, {
                includeScrollback: true,
                cols,
                rows,
            });
            if (info) {
                window.App.returnToExistingTerminal(info);
            }
        } catch (err) {
            this.showError('failed to return to terminal: ' + (err.message || err));
        }
    }

    /**
     * Bind a single delegated click listener on #running-sessions-list.
     *
     * Event delegation over per-row listeners: avoids re-binding on every
     * render and survives DOM swaps. The `__boundRunningClicks` flag is
     * a one-shot idempotence guard - re-calling from renderRunningSessions
     * is a no-op after the first paint.
     *
     * Click target disambiguation:
     *   - the row's action button (or its SVG child) → close/remove flow
     *   - anywhere else on `.running-session-row`    → return/swap flow
     *
     * The action selector is built from SessionRowActions.ATTR_ACTION,
     * never from a literal: the same module builds the markup, so reading
     * the name from it is what makes the two sides unable to drift. There
     * is deliberately no literal fallback for a missing module, because
     * SessionRowActions.html() emits nothing in that case and there would
     * be no such element to match.
     *
     * stopPropagation on the action branch is the important bit: without
     * it the row handler would also fire and we'd race a swap against a
     * destroy.
     */
    _bindRunningSessionClicks() {
        const container = document.getElementById('running-sessions-list');
        if (!container || container.__boundRunningClicks) return;
        container.addEventListener('click', async (e) => {
            const actions = window.SessionRowActions;
            const rowActionEl = actions
                ? e.target.closest(`[${actions.ATTR_ACTION}]`)
                : null;
            const forkEl = e.target.closest('.running-session-fork');
            const renameEl = e.target.closest('.running-session-rename');
            const markUnreadEl = e.target.closest('[data-mark-unread]');
            const rowEl = e.target.closest('.running-session-row');
            if (!rowEl) return;

            // Envelope icon path: manual mark/clear unread. Stop
            // propagation so the row click handler (return/adopt) never
            // also fires - this is a status toggle, not a navigation.
            if (markUnreadEl) {
                e.stopPropagation();
                await this._handleMarkUnread(markUnreadEl);
                return;
            }

            // Fork path: spawn a NEW session branching this one. Stops
            // propagation so the row click (return/adopt) does not also
            // fire - forking is not navigation.
            if (forkEl) {
                e.stopPropagation();
                await this._forkSession(forkEl.getAttribute('data-fork-name'));
                return;
            }

            // X (close) / trash (remove) path: explicit destroy. Which
            // one this row painted is read back off the button rather
            // than re-derived, so the confirm copy always matches the
            // control the user actually clicked.
            if (rowActionEl) {
                e.stopPropagation();
                // `actions` is non-null here by construction: rowActionEl
                // can only be non-null when the selector above was built,
                // which requires the module.
                const name = rowActionEl.getAttribute(actions.ATTR_NAME);
                const action = rowActionEl.getAttribute(actions.ATTR_ACTION);
                const sid = rowEl.dataset.sessionId || null;
                await this._handleSessionRowAction(name, sid, action);
                return;
            }

            // Pencil icon path, UNAVAILABLE state. A row whose session id
            // we do not hold now draws a dimmed pencil that explains why
            // instead of drawing nothing (see _renderRenamePencilHtml).
            // It carries a different class so it can never match the live
            // selector below, but it still has to swallow its own click:
            // without this, clicking a control the UI just told you is
            // unavailable would fall through to the row and open or adopt
            // the session, which is a surprising side effect from a
            // disabled affordance. The reason is already on the element
            // as title + aria-label, so there is nothing further to say.
            if (e.target.closest('.running-session-rename-unavailable')) {
                e.stopPropagation();
                return;
            }

            // Pencil icon path: inline-edit the session name. Stop
            // propagation so the row click handler (return/adopt) doesn't
            // also fire and race the edit. Only pencil buttons with a
            // ``data-rename-sid`` appear in rows with a known session_id.
            if (renameEl) {
                e.stopPropagation();
                const sid = renameEl.dataset.renameSid;
                if (sid) {
                    // No name is passed. The editor reads its seed off
                    // the row's own name span, so what it opens on and
                    // what the row displays cannot disagree. Handing it
                    // `data-rename-name` (the tmux handle) is what made
                    // a plain Enter overwrite the user's label.
                    this._handleRenameRunningSession(rowEl, sid);
                }
                return;
            }

            // Row click - return or attach
            const name = rowEl.dataset.name;
            const isActive = rowEl.dataset.active === '1';
            const rowSessionId = rowEl.dataset.sessionId || null;
            if (isActive) {
                await this._returnToActiveRunningSession(rowSessionId);
                return;
            }
            // Not yet attached → adopt it as a (new, concurrent) session
            await this._handleAttachRunningSession(name);
        });
        // Keyboard activation (Enter/Space) for the mark-unread toggle -
        // it's a `role="button"` span, not a real <button>, so it needs
        // explicit key handling to be operable without a mouse.
        container.addEventListener('keydown', async (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const markUnreadEl = e.target.closest('[data-mark-unread]');
            if (!markUnreadEl) return;
            e.preventDefault();
            e.stopPropagation();
            await this._handleMarkUnread(markUnreadEl);
        });
        container.__boundRunningClicks = true;
    }

    /**
     * Toggle the manual unread flag for one running-session row.
     *
     * Description: Optimistic-ish - awaits the PATCH, then forces a
     *   re-render by invalidating the signature cache and re-fetching, so
     *   the toggle's visual state (and the finished_unread dot it may
     *   flip on/off) updates immediately rather than waiting for the next
     *   5s poll tick.
     * Inputs:
     *   toggleEl (Element) - the `[data-mark-unread]` span clicked,
     *     carrying the tmux name + current state as data-* attributes.
     * Output: Promise<void>.
     */
    async _handleMarkUnread(toggleEl) {
        const tmuxName = toggleEl.dataset.markUnread;
        if (!tmuxName) return;
        const next = toggleEl.dataset.unreadCurrent !== 'true';
        try {
            await window.API.setSessionUnread(tmuxName, next);
            this._lastRunningSig = null; // force a repaint past the sig-diff guard
            await this.loadRunningSessions();
        } catch (err) {
            console.error('[launchpad] mark-unread failed:', err);
        }
    }

    /**
     * Run the destructive row action (close a running session, or remove
     * a stopped one from the list). Both end at the same server call: a
     * stopped row has no process left to terminate, so clearing its
     * leftover tmux husk is exactly what "remove" means. The confirm copy
     * differs and is honest about that difference - see
     * client/js/session-row-actions.js.
     *
     * Inputs:
     *   tmuxName (string) - literal tmux session name.
     *   sessionId (string|null) - server session id when already known.
     *   action (string|null) - SessionRowActions.ACTION_CLOSE or
     *     ACTION_REMOVE; defaults to close.
     * Output: Promise<void>. No-op if the user cancels.
     *
     * Two server paths:
     *   1. Target IS the currently-active backend → DELETE /sessions
     *      (full destroy: tears down backend, idle watcher, tunnels,
     *      metadata; then kills tmux).
     *   2. Target is a different session → DELETE /sessions/external/{name}
     *      (direct `tmux kill-session`, no adoption). This used to be
     *      adopt-then-destroy, but adoption refuses dead panes (raised
     *      `RuntimeError("pane already dead")` in tmux_backend.attach_existing,
     *      surfacing as HTTP 500 from POST /sessions/adopt) - leaving any
     *      session whose foreground process exited permanently un-killable
     *      from the UI. The dedicated external-destroy endpoint sidesteps
     *      adoption entirely and is also idempotent if the session is
     *      already gone server-side.
     */
    /**
     * v0.7.1 - Inline-edit a running session's name from the launchpad row.
     *
     * Replaces the row's name span with a text input pre-filled with the
     * name the row is DISPLAYING. Enter saves (calls PATCH
     * /sessions/{id}/name); Esc cancels (restores the span). On success
     * the server broadcasts ``session.renamed`` over WS to every attached
     * browser; we ALSO force-refresh the launchpad list immediately so the
     * new name shows without waiting on the 5s poller.
     *
     * THIS EDITS THE LABEL. ``sessions.title`` is free-form text a human
     * typed; the tmux name is an internal handle derived from it once, at
     * creation, and never moved again. This control used to seed itself
     * from that handle and enforce the handle's own
     * ``^[A-Za-z0-9_-]{1,64}$`` against it, which did two things: it
     * refused "Media Compression" in the browser against a server that
     * accepts it happily, and it showed a user who HAD a label the handle
     * instead, so a plain Enter stored a handle-derived string over their
     * own label. Silent data loss.
     *
     * THE SEED IS THE RENDERED TEXT, read off the name span rather than
     * re-derived. That is what makes display and seed agree by
     * construction rather than by two functions happening to match: if
     * the row's display half ever regresses to the handle, the seed
     * regresses with it and one test catches both.
     *
     * @param {HTMLElement} rowEl - The ``.running-session-row`` host.
     * @param {string} sessionId - Server-side session id (not tmux name).
     */
    _handleRenameRunningSession(rowEl, sessionId) {
        const nameEl = rowEl.querySelector('.running-session-name');
        if (!nameEl) return;
        // Idempotent - bail if an input is already showing in this row.
        if (rowEl.querySelector('.running-session-rename-input')) return;

        // THE SEED IS WHAT THE ROW IS SHOWING. Read BEFORE the span is
        // hidden below - `display: none` does not affect textContent, but
        // the ordering is worth not depending on.
        const seed = (nameEl.textContent || '').trim();

        // Build the input + inline error label.
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'running-session-rename-input';
        input.value = seed;
        // The LABEL's limit, from the one module that mirrors the server.
        // It used to be a hardcoded 64 - the old tmux-name limit - which
        // silently truncated any longer label at the maxlength with no
        // error anywhere, because a maxlength does not report anything.
        input.setAttribute(
            'maxlength',
            String((window.SessionLabel && window.SessionLabel.LABEL_MAX_CHARS) || 200)
        );
        input.spellcheck = false;
        input.autocomplete = 'off';
        input.setAttribute('aria-label', 'New session label');

        const err = document.createElement('span');
        err.className = 'running-session-rename-error';
        err.style.display = 'none';

        // Hide the name span while editing.
        nameEl.style.display = 'none';

        nameEl.insertAdjacentElement('afterend', input);
        input.insertAdjacentElement('afterend', err);

        let settled = false;
        const cleanup = () => {
            try { if (input.parentNode) input.parentNode.removeChild(input); } catch (_) { /* */ }
            try { if (err.parentNode) err.parentNode.removeChild(err); } catch (_) { /* */ }
            nameEl.style.display = '';
        };
        const cancel = () => {
            if (settled) return;
            settled = true;
            cleanup();
        };
        const save = async () => {
            if (settled) return;
            const raw = (input.value || '').trim();
            // UNCHANGED IS A NO-OP, AND THE BASIS IS THE SEED. This guard
            // existed before; it compared against the tmux HANDLE, a
            // value a label-editing user never types, so it could
            // essentially never fire. The basis has to move with the
            // seed - a seed of the label against a comparison on the
            // handle would make every dismissal a write.
            //
            // It is also what makes seeding a session with NO label safe.
            // That session is seeded with the stripped handle, because an
            // empty box for a row that visibly has a name is worse to use
            // and no safer; unchanged writing nothing is what stops that
            // handle being promoted into a stored label nobody typed.
            if (!raw || raw === seed) {
                cancel();
                return;
            }
            // The label rule, from the one module that holds it. NOT a
            // local regex - a second copy is how the client and server
            // drift apart without either one looking wrong.
            const verdict = (window.SessionLabel && window.SessionLabel.validate)
                ? window.SessionLabel.validate(raw)
                : { ok: false, reason: 'the label rule is unavailable' };
            if (!verdict.ok) {
                err.textContent = verdict.reason;
                err.style.display = '';
                input.focus();
                input.select();
                return;
            }
            settled = true;
            // Stopping the row's swallow-click handlers is already done by
            // the caller (stopPropagation on the renameEl branch).
            try {
                await window.API.renameSession(sessionId, verdict.value);
                cleanup();
                // Immediate refresh so the row paints the new name without
                // waiting on the 5s poller. The WS broadcast (received by
                // the terminal controller) ALSO triggers a refresh -
                // duplicate calls are idempotent; the load itself is
                // signature-diffed inside renderRunningSessions.
                await this.loadRunningSessions();
            } catch (e) {
                settled = false;
                let msg = (e && e.message) ? e.message : 'Rename failed';
                if (/409/.test(msg) || /already in use/i.test(msg)) {
                    msg = 'Name already in use';
                } else if (/400/.test(msg) || /Invalid session name/i.test(msg)) {
                    msg = 'Invalid name';
                } else if (/404/.test(msg)) {
                    msg = 'Session not found';
                }
                err.textContent = msg;
                err.style.display = '';
                input.focus();
                input.select();
            }
        };

        input.addEventListener('keydown', (e) => {
            // Don't let row-level keyboard nav reach the container.
            e.stopPropagation();
            if (e.key === 'Enter') {
                e.preventDefault();
                save();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                cancel();
            }
        });
        input.addEventListener('click', (e) => {
            // Clicks inside the input must not bubble to the row's
            // return/adopt handler.
            e.stopPropagation();
        });
        input.addEventListener('blur', () => { save(); });

        setTimeout(() => { input.focus(); input.select(); }, 0);
    }

    /**
     * Restart the agent in a session whose process exited, in place.
     *
     * Description: one POST, then a list refresh. The row keeps its
     *   identity, so the refreshed list shows the SAME row alive again
     *   rather than a new one appearing beside a corpse.
     *
     *   THE SERVER'S ``ok`` IS THE VERDICT, NOT THE HTTP STATUS. A 200
     *   carrying ``ok:false`` is the normal shape for "the pane could not
     *   be read" and for "it started and exited again", and both of those
     *   are things the user needs told. Treating a 200 as success would
     *   leave him staring at a row that did not come back with no
     *   explanation - the exact failure this feature exists to end, one
     *   layer up. The server's ``detail`` sentence is shown verbatim
     *   rather than replaced with our own wording, because it is the only
     *   thing that knows WHICH of those happened.
     *
     *   The list is reloaded on every path including failure, so a row
     *   whose real state changed under us repaints either way.
     * Inputs: tmuxName (string) - literal tmux session name.
     *   display (string) - name as shown in the row, for the message.
     * Output: Promise<void>.
     */
    async _handleRestartSession(tmuxName, display) {
        try {
            const result = await window.API.respawnSession(tmuxName);
            if (!result || result.ok !== true) {
                const why = (result && result.detail) || 'no reason given';
                this.showError(`could not restart "${display}": ${why}`);
            }
        } catch (error) {
            this.showError(
                `could not restart "${display}": `
                + (error && error.message ? error.message : 'unknown error')
            );
        }
        await this.loadRunningSessions();
    }

    async _handleSessionRowAction(tmuxName, sessionId = null, action = null) {
        if (!tmuxName) return;
        if (!window.SessionRowActions) {
            // Load-order bug, not a user-facing state: index.html loads
            // session-row-actions.js before this file. Refuse to run a
            // destructive action without its confirm rather than fall
            // back to a second, unreviewed confirmation path.
            console.error('[launchpad] SessionRowActions missing, refusing to act');
            return;
        }
        // NAME IT THE WAY THE USER KNOWS IT. This derived the display
        // straight off the tmux handle, so a session labelled "Media
        // Compression" produced a DESTRUCTIVE confirmation reading
        // "cloude_Media_Compression" - asking someone to confirm
        // destroying something under a name they never chose and may not
        // recognise, which is the worst dialog in the app to get wrong.
        //
        // The row is looked up by handle, which is the identity and the
        // only thing this handler is given, and then resolved through the
        // same `_sessionDisplayLabel` the row itself rendered with, so the
        // dialog and the row cannot disagree. A handle with no row falls
        // back to the old derivation rather than refusing: a confirm that
        // cannot name its target is still better than no confirm.
        const actionRow = (this.runningSessions || []).find((s) => s && s.name === tmuxName);
        const display = actionRow
            ? this._sessionDisplayLabel(actionRow)
            : this._deriveRunningSessionDisplayName(tmuxName);
        const resolved = action || window.SessionRowActions.ACTION_CLOSE;

        // RESTART branches out before the confirm, because it is the one
        // action here that destroys nothing - see
        // SessionRowActions.requiresConfirm for the policy and why a
        // dialog on a harmless action is a cost, not a safeguard.
        //
        // NOT the same thing as _restartRecentSession() further up this
        // file. That one creates a BRAND NEW session for a row whose tmux
        // instance is gone entirely. This one revives a session that still
        // exists with a dead pane, keeping its name, scrollback, theme and
        // database row. Same word, two different operations; do not merge
        // them.
        if (resolved === window.SessionRowActions.ACTION_RESTART) {
            await this._handleRestartSession(tmuxName, display);
            return;
        }

        // Confirm copy comes from the shared SessionRowActions module so
        // the launcher and the conversation sidebar say the same true
        // thing about the same operation. showConfirmModal escapes its
        // own arguments, so don't pre-escape `display` or it double-escapes.
        const confirmed = await window.SessionRowActions.confirm(resolved, display);
        if (!confirmed) return;
        try {
            // Resolve the session id for this tmux name if not supplied:
            // a live session bound to it must go through DELETE /sessions
            // (full teardown). External/detached → direct kill-session.
            let sid = sessionId;
            if (!sid && typeof window.API.listSessions === 'function') {
                const live = await window.API.listSessions().catch(() => []);
                const match = Array.isArray(live)
                    ? live.find(x => x && x.tmux_session === tmuxName)
                    : null;
                sid = match ? ((match.session && match.session.id) || match.id || null) : null;
            }
            if (sid) {
                await window.API.destroySession(sid);
            } else {
                await window.API.destroyExternalSession(tmuxName);
            }
        } catch (err) {
            this.showError(`${resolved} failed: ${err.message || err}`);
        }
        await this.loadRunningSessions();
    }

    /**
     * Adopt flow for a not-yet-attached running tmux session (row click).
     *
     * Multi-session: this is purely additive - adopting this tmux session
     * does NOT detach or kill any other session. On adopt success, dispatch
     * `session-created` with the adopt-specific detail payload
     * (initialScrollbackB64, fifoStartOffset, adopted:true) so
     * App.showTerminal() can plumb scrollback into the terminal controller.
     */
    async _handleAttachRunningSession(tmuxName) {
        try {
            const response = await window.API.adoptSession(tmuxName, true);
            const session = response.session || response;
            // THE ADOPT RESPONSE HAS NO LABEL, and the header and tab
            // title are both resolved from one.
            //
            // Session (src/models.py) carries the tmux handle, not the
            // user's chosen name, so attaching this way titled the tab
            // "ScratchLab-4_fork" while entering the SAME session from the
            // sidebar - which passes the listing row - titled it "Refactor
            // spike (round 2)". Same session, two names, depending on how
            // you got there.
            //
            // The label is already in hand: the listing row was what we
            // matched to decide to attach at all. Carry it across rather
            // than widening the Session model, and never overwrite a label
            // the response did supply.
            if (session && !session.label) {
                const known = (this.runningSessions || []).find(
                    r => r.name === tmuxName
                );
                if (known && known.label) session.label = known.label;
            }
            const initialScrollbackB64 = response.initial_scrollback_b64 || '';
            const fifoStartOffset = typeof response.fifo_start_offset === 'number'
                ? response.fifo_start_offset
                : null;

            // Auto-add adopted session to Recent Projects so the user can
            // relaunch it after tmux dies. Mirrors the create-flow pattern
            // (lines 1164-1177). Skip if working_dir is missing (shouldn't
            // happen for adopted sessions but defensive).
            if (session && session.working_dir) {
                try {
                    // Strip the `cloude_` tmux-namespace prefix that
                    // session_manager.py adds when minting the tmux name -
                    // otherwise Recent Projects ends up storing
                    // `cloude_<name>`, and the next launch double-prefixes
                    // it to `cloude_cloude_<name>`. The display name in
                    // Recent Projects should be the bare project name.
                    const rawName = session.tmux_session || tmuxName;
                    const cleanName = rawName.replace(/^cloude_/, '');
                    await window.API.createProject({
                        name: cleanName,
                        path: session.working_dir,
                        description: ''
                    });
                } catch (error) {
                    // If project already exists, that's ok - continue anyway
                    if (!error.message.includes('already exists')) {
                        console.error('Launchpad: Failed to save adopted project:', error);
                    }
                }
            }

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
        // TWO CHILDREN, ONE OF WHICH SCROLLS. #launchpad-screen is a flex
        // column that does not scroll; .launchpad-scroll takes the
        // leftover height and scrolls, and .home-bar sits below it and
        // does not shrink. That split is what makes the bar a real bottom
        // bar rather than the last thing in the scrolled content, and it
        // is why the bar can never overlap a project row or the "+" FAB's
        // fan-out menu: they are in different boxes. See
        // client/css/home-bar.css.
        this.launchpadScreen.innerHTML = `
            <div class="launchpad-scroll">
            <div class="launchpad-container">
                <!-- HOME-HEADER-CONSOLIDATION: the "Cloude Code Launcher"
                     title + "select a project or create a new project"
                     prompt used to render here as their own block
                     (.launchpad-header / .launchpad-prompt). They now live
                     in the top header itself (App.showLaunchpad() ->
                     setHeaderIdentity(), client/js/app.js), centred, with
                     the prompt as the header's second row. Do not re-add
                     them here - that would restore the standalone block's
                     vertical cost this change removed. -->

                <!-- LAUNCHPAD HELP. Lives at the TOP of the pane, under the
                     launcher title and its subtitle - NOT in the running-sessions
                     heading row where the adopt-only version of this used to sit.
                     Two reasons it moved there originally: (a) the running-sessions
                     section is display:none until a session exists, so the one
                     explanation of how to adopt a session you started yourself was
                     hidden from exactly the user who had not started one yet;
                     (b) as a bordered text "?" pill it was the only non-SVG glyph
                     on the pane and read as a different weight from every icon
                     around it. It has since grown from "how to adopt" into the
                     app's one general help surface (adopting, wrappers, slash
                     commands) because there is nowhere else on this screen a
                     stuck user would look - see docs/help-content-audit.md for
                     what was wrong with the old copy and why each section reads
                     the way it does now.
                     The marker is an inline SVG in the same family as the
                     .new-fab__icon set: viewBox "0 0 24 24" with stroke-width
                     1.8 as a PRESENTATION ATTRIBUTE on the svg, inherited by
                     the paths. Do not move stroke-width into a CSS svg rule -
                     a presentation attribute on a child path beats it, which
                     has silently defeated stroke restyles here twice.
                     It stays a native details/summary, never a button: the
                     bare "button { width: 36px; height: 36px }" reset in
                     styles.css would force a 36px box on it (40px under the
                     480px media query) and a class only overrides the properties
                     it actually declares.
                     NOTE FOR ANY FUTURE EDIT OF THIS BLOCK: no backticks in
                     here. The whole return value is a template literal, so a
                     backtick in a comment ends the string and takes the module
                     out with it. Also: no em dashes or en dashes in the rendered
                     copy itself (project style rule) - use a period or a colon. -->
                <!-- STAGE C: the session-attribution prompt. Rendered
                     into by renderAttributionPrompt(), which is called
                     from loadProjects(). It sits ABOVE the help
                     disclosure and above the project list because it is
                     a question, not a status line, and a question below
                     the fold is a question nobody answers. The container
                     is always present and always EMPTY when there is
                     nothing to ask, so an empty prompt costs no layout. -->
                <div id="attribution-prompt" class="attribution-prompt-slot"></div>

                <details class="adopt-disclosure">
                    <summary aria-label="help: adopting sessions, wrappers, and slash commands" title="help">
                        <svg class="adopt-disclosure__icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                            <circle cx="12" cy="12" r="10"/>
                            <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4"/>
                            <line x1="12" y1="17.5" x2="12" y2="17.5"/>
                        </svg>
                    </summary>
                    <div class="adopt-disclosure-body">
                        <p><strong>adopting a session you started yourself</strong></p>
                        <p>you don't have to launch through cloude. <em>any</em> tmux session on the <code>cloude</code> socket with <code>claude</code> running inside it shows up here, adoptable. start one yourself in any terminal:</p>
                        <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork; claude</code></pre>
                        <p>it shows up in this list tagged <code>EXTERNAL</code>. click it to adopt. that tag is worked out fresh each time this list loads by checking which tmux session names cloude itself created, not stored on the session, so give it a few seconds after adopting elsewhere before you trust it. note the <code>-L cloude</code> flag: a plain <code>tmux new -s mywork</code> lives on the default socket and never appears here.</p>
                        <p>to launch claude in one line so the pane survives claude exiting:</p>
                        <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "claude --dangerously-skip-permissions; exec \$SHELL"</code></pre>
                        <p>the <code>exec \$SHELL</code> part keeps the pane alive with a shell prompt after claude exits.</p>
                        <p>if you already have a launcher function (e.g. <code>cld</code>) defined in your <code>~/.zshrc</code> or <code>~/.bashrc</code>, run it through an interactive shell so it resolves:</p>
                        <pre class="adopt-disclosure-code"><code>tmux -L cloude new -s mywork "\$SHELL -ic 'cld; exec \$SHELL'"</code></pre>
                        <p>full <code>cld</code> setup in the <a href="https://github.com/Adoom666/CloudeCode#before-you-start-three-things-that-will-bite-you" target="_blank" rel="noopener">README</a>.</p>

                        <p><strong>wrappers and launch wrappers are the same thing</strong></p>
                        <p>settings names the tab <code>wrappers</code>; the panel inside it titles the same section <code>launch wrappers</code>. both mean one object: a named shell command tied to one agent family (claude, codex, hermes, openclaw, or shell) that runs when a session launches. there is no second, different kind of wrapper hiding anywhere.</p>
                        <p>configure them under settings, wrappers tab. pick one per family as the default, or choose a different one at launch time from the new-session picker. a family with no wrappers falls back to its static legacy command, shown collapsed under "advanced: legacy &lt;family&gt; command" inside that family's group.</p>

                        <p><strong>slash commands</strong></p>
                        <p>open the slash command list from the <code>/</code> control next to the terminal input (or the d-pad). the row above the terminal shows your starred favorites as tappable chips. star a command in the list to add it there; until you star anything, the row shows a small built-in default set, not your own picks.</p>
                    </div>
                </details>

                <!-- CREATE CONTROL. It lives HERE, in its own always-present
                     row, and NOT inside a section title, because it is a
                     GLOBAL action whose lifetime must not depend on any one
                     list's contents.

                     It used to be a child of #running-sessions-section's
                     title row. That section is display:none while the user
                     has zero sessions, so on a fresh install the only
                     control that creates a project or a session measured
                     0x0 and a brand-new user could not create anything at
                     all - the button that makes your first session only
                     existed once you already had one. The button was in the
                     DOM the whole time with visibility:visible, so every
                     markup assertion passed against the broken build; only
                     getBoundingClientRect() and a walk up the ancestor
                     chain could see it. See scripts/verify_fresh_install.py,
                     which measures exactly that and ships with a --legacy
                     positive control that re-parents it back here to prove
                     the check can fail.

                     Do NOT move this back inside a section. If it needs to
                     sit visually beside a heading, style this row - do not
                     re-parent the control. -->
                <div class="launchpad-actions" id="launchpad-actions">
                    <div class="new-fab" id="new-fab">
                            <button class="new-fab__trigger" id="new-fab-trigger" type="button" aria-label="New" title="New" aria-haspopup="menu" aria-expanded="false">
                                <svg class="new-fab__plus" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
                                    <line x1="12" y1="5" x2="12" y2="19"/>
                                    <line x1="5" y1="12" x2="19" y2="12"/>
                                </svg>
                            </button>
                            <div class="new-fab__menu" role="menu" aria-label="New session actions">
                                <!-- TOP ITEM. "create new project" was the
                                     claude entrypoint without ever saying so.
                                     It is named for what it makes, and it
                                     carries the app's OWN icon file
                                     (client/assets/icons/header-icon.png, the
                                     same asset the header uses) rather than a
                                     glyph traced by hand. Never redraw a mark
                                     here; if an asset you need does not exist,
                                     say so instead of approximating one. -->
                                <button class="new-fab__item" type="button" role="menuitem" data-action="new-claude-project" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <img class="new-fab__icon-img" src="/static/assets/icons/header-icon.png" srcset="/static/assets/icons/header-icon.png 1x, /static/assets/icons/header-icon@2x.png 2x" alt="" />
                                    </span>
                                    <span class="new-fab__label">new claude project</span>
                                </button>
                                <!-- SECOND ITEM. Adds a session to a project
                                     that already exists. It never creates a
                                     project, and with no projects to choose
                                     from it says so rather than opening an
                                     empty picker. -->
                                <button class="new-fab__item" type="button" role="menuitem" data-action="new-session" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <rect x="3" y="4" width="18" height="16" rx="2"/>
                                            <line x1="12" y1="9" x2="12" y2="15"/>
                                            <line x1="9" y1="12" x2="15" y2="12"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">new session</span>
                                </button>
                                <!-- NO "open from folder" ITEM HERE ANY MORE.
                                     Opening a folder already on disk is one
                                     of the three ways to start a claude
                                     project, not a peer of starting one, so
                                     it is now the third choice inside "new
                                     claude project" above (see
                                     startNewClaudeProject). Do not restore
                                     it here: two entry points to the same
                                     flow is what this removed. -->
                                <button class="new-fab__item" type="button" role="menuitem" data-action="connect-openclaw" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <path d="M6 3v6a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v4"/>
                                            <path d="M6 3l-2 2"/>
                                            <path d="M6 3l2 2"/>
                                            <path d="M18 21l-2-2"/>
                                            <path d="M18 21l2-2"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">connect to openclaw</span>
                                </button>
                                <button class="new-fab__item" type="button" role="menuitem" data-action="connect-hermes" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <path d="M13 2L4 14h7l-2 8 9-12h-7l2-8z"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">connect to hermes</span>
                                </button>
                                <button class="new-fab__item" type="button" role="menuitem" data-action="new-console" tabindex="-1">
                                    <span class="new-fab__icon" aria-hidden="true">
                                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                                            <polyline points="4 7 9 12 4 17"/>
                                            <line x1="12" y1="18" x2="20" y2="18"/>
                                        </svg>
                                    </span>
                                    <span class="new-fab__label">new console</span>
                                </button>
                            </div>
                    </div>
                </div>

                <div id="running-sessions-section" class="launchpad-section running-sessions-section" style="display:none;">
                    <div class="launchpad-section-title launchpad-section-title--row">
                        <button type="button" class="launchpad-section-toggle" id="running-sessions-toggle" aria-expanded="true" aria-controls="running-sessions-list">
                            <span class="launchpad-section-chevron" aria-hidden="true">►</span>
                            <span class="launchpad-section-title__text">running sessions</span>
                            <span class="launchpad-section-count" id="running-sessions-count" data-listing-ok="1"></span>
                        </button>
                    </div>
                    <div id="running-sessions-list"></div>
                </div>

                <!-- RECENT (S9) - datastore-backed, NOT a live tmux probe.
                     Every row here is lifecycle='stopped' read straight
                     from the sessions table. See Launchpad.loadRecentSessions /
                     renderRecentSessions in launchpad.js. Hidden via
                     display:none when empty, same convention as the running
                     sessions section above. -->
                <div id="recent-sessions-section" class="launchpad-section recent-sessions-section" style="display:none;">
                    <div class="launchpad-section-title">
                        <button type="button" class="launchpad-section-toggle" id="recent-sessions-toggle" aria-expanded="true" aria-controls="recent-sessions-list">
                            <span class="launchpad-section-chevron" aria-hidden="true">►</span>
                            <span class="launchpad-section-title__text">recent</span>
                            <span class="launchpad-section-count" id="recent-sessions-count" data-state="ok"></span>
                        </button>
                    </div>
                    <div id="recent-sessions-list"></div>
                </div>

                <!-- "new project" actions live in the inline speed-dial FAB
                     to the right of the "running sessions" heading. Wired in setupNewFab(). -->

                <div class="launchpad-section" id="projects-section">
                    <div class="launchpad-section-title">
                        <button type="button" class="launchpad-section-toggle" id="projects-section-toggle" aria-expanded="true" aria-controls="project-list">
                            <span class="launchpad-section-chevron" aria-hidden="true">►</span>
                            projects
                        </button>
                    </div>
                    <div id="project-list" class="project-list">
                        <div class="launchpad-empty">loading projects...</div>
                    </div>
                </div>

                <!-- NO "SERVER MANAGEMENT" SECTION HERE ANY MORE. Its one
                     control, "reset server", is now the "restart server"
                     row of the home bar's server-controls menu below
                     (client/js/server-controls-menu.js). It is in ONE
                     place, not two. A collapsible section plus a
                     full-width button was a lot of the home screen's
                     vertical budget for a control pressed roughly never. -->
                </div>
            </div>

            <!-- THE HOME BAR. A flex row whose direct children are its
                 items: everything before .home-bar__spacer hugs the left
                 edge, everything after it the right. Adding an item later
                 is adding one child on the side it belongs to - there is
                 no slot table and no layout to rewrite.

                 HOME SCREEN ONLY. This markup is rendered into
                 #launchpad-screen and nowhere else, so it cannot appear on
                 the terminal screen, which spends its vertical pixels on
                 the terminal. -->
            <div class="home-bar" role="toolbar" aria-label="home bar">
                <button type="button" id="server-controls-btn" class="home-bar__btn"
                        aria-haspopup="menu" aria-expanded="false"
                        aria-label="server controls" title="server controls">
                    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <path d="M8 10.25a2.25 2.25 0 1 0 0-4.5 2.25 2.25 0 0 0 0 4.5Z" stroke="currentColor" stroke-width="1.5"/>
                        <path d="M13 8c0-.38-.04-.75-.12-1.1l1.34-.98-1.5-2.6-1.55.62a5.05 5.05 0 0 0-1.9-1.1L9.05 1h-3l-.22 1.84c-.7.24-1.35.62-1.9 1.1l-1.55-.62-1.5 2.6 1.34.98a5.1 5.1 0 0 0 0 2.2l-1.34.98 1.5 2.6 1.55-.62c.55.48 1.2.86 1.9 1.1L6.05 15h3l.22-1.84c.7-.24 1.35-.62 1.9-1.1l1.55.62 1.5-2.6-1.34-.98c.08-.35.12-.72.12-1.1Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
                    </svg>
                </button>
                <!-- Connection light, LEFT group. The dot itself is NOT in
                     this markup on purpose: this is a MOUNT POINT, not a
                     copy. The one #statusText node lives in the header on
                     the auth and terminal screens and is moved in here by
                     App._placeStatusLight() while the home screen is up,
                     the same node-moving rule header-menu.js follows,
                     because it is addressed by id by app.js and
                     terminal.js. The label is written from that node's
                     data-status by App._observeStatusText(), so the string
                     still has exactly one author. -->
                <span class="home-bar__status" id="home-bar-status">
                    <span class="home-bar__status-text" id="home-bar-status-text"></span>
                </span>
                <span class="home-bar__spacer" aria-hidden="true"></span>
                <span class="version home-bar__version" id="home-bar-version"></span>
                <a class="home-bar__link" href="https://nyedis.ai" target="_blank" rel="noopener noreferrer"
                   aria-label="nyedis.ai" title="nyedis.ai">
                        <svg version="1.1" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 986 937" role="img" aria-label="Black bird silhouette">
                            <path d="M 409.0 883.5 L 408.5 882.0 L 458.5 804.0 L 489.5 748.0 L 488.0 747.5 L 453.0 783.5 L 437.0 797.5 L 403.0 823.5 L 377.0 839.5 L 376.5 838.0 L 398.5 816.0 L 438.5 771.0 L 469.5 732.0 L 478.5 718.0 L 474.0 719.5 L 436.0 750.5 L 388.0 785.5 L 394.5 766.0 L 409.5 739.0 L 408.0 738.5 L 386.0 753.5 L 377.0 758.5 L 375.5 758.0 L 382.5 743.0 L 394.5 725.0 L 410.5 705.0 L 410.5 703.0 L 374.0 704.5 L 361.0 702.5 L 360.5 701.0 L 409.0 681.5 L 481.0 647.5 L 520.0 625.5 L 546.0 607.5 L 565.5 589.0 L 570.5 580.0 L 570.5 576.0 L 561.0 575.5 L 542.0 580.5 L 545.5 574.0 L 560.5 556.0 L 594.0 522.5 L 632.5 489.0 L 630.0 487.5 L 588.0 488.5 L 551.0 493.5 L 516.0 500.5 L 529.5 487.0 L 532.5 480.0 L 532.0 473.5 L 515.0 472.5 L 491.0 468.5 L 451.0 455.5 L 435.5 448.0 L 452.0 439.5 L 456.5 435.0 L 456.0 433.5 L 420.0 426.5 L 402.0 420.5 L 394.5 416.0 L 427.0 414.5 L 442.0 411.5 L 445.0 410.5 L 445.0 408.5 L 399.0 408.5 L 375.0 406.5 L 333.0 400.5 L 305.5 393.0 L 306.0 391.5 L 309.0 391.5 L 344.0 394.5 L 429.0 395.5 L 461.0 394.5 L 461.0 392.5 L 426.0 390.5 L 378.0 384.5 L 302.0 370.5 L 249.0 358.5 L 180.0 339.5 L 138.0 331.5 L 75.0 314.5 L 34.0 299.5 L 19.0 291.5 L 15.5 287.0 L 18.0 285.5 L 173.5 287.0 L 173.0 285.5 L 125.0 275.5 L 91.0 264.5 L 68.0 252.5 L 59.5 244.0 L 59.0 238.5 L 134.0 251.5 L 227.5 271.0 L 225.5 266.0 L 218.0 259.5 L 181.5 238.0 L 185.0 237.5 L 297.0 264.5 L 434.0 294.5 L 546.0 316.5 L 613.0 326.5 L 613.5 325.0 L 607.0 320.5 L 591.0 312.5 L 561.0 301.5 L 509.0 287.5 L 450.0 276.5 L 449.5 275.0 L 483.0 262.5 L 505.0 257.5 L 534.0 253.5 L 600.0 252.5 L 625.0 255.5 L 632.5 255.0 L 622.0 245.5 L 609.0 239.5 L 588.0 233.5 L 551.5 228.0 L 568.0 220.5 L 585.0 217.5 L 612.0 217.5 L 644.0 221.5 L 692.0 232.5 L 737.0 247.5 L 741.0 247.5 L 747.0 241.5 L 754.0 237.5 L 771.0 233.5 L 797.0 235.5 L 814.0 240.5 L 827.0 246.5 L 841.5 259.0 L 844.5 265.0 L 845.5 281.0 L 843.5 288.0 L 836.5 301.0 L 824.5 316.0 L 805.0 334.5 L 782.0 351.5 L 769.5 365.0 L 761.5 379.0 L 761.5 390.0 L 765.0 393.5 L 767.0 393.5 L 778.0 388.5 L 795.0 383.5 L 807.0 381.5 L 827.0 381.5 L 852.0 387.5 L 865.0 393.5 L 879.0 402.5 L 904.5 426.0 L 925.5 454.0 L 946.5 492.0 L 957.5 518.0 L 961.5 532.0 L 944.0 513.5 L 932.0 503.5 L 923.0 497.5 L 903.0 488.5 L 889.0 485.5 L 873.0 485.5 L 853.0 490.5 L 839.0 497.5 L 829.0 504.5 L 814.5 519.0 L 783.5 561.0 L 754.5 604.0 L 737.5 635.0 L 737.5 659.0 L 740.0 661.5 L 765.0 671.5 L 816.0 696.5 L 826.0 699.5 L 843.0 709.5 L 857.0 720.5 L 879.5 743.0 L 894.5 762.0 L 895.5 768.0 L 881.0 780.5 L 879.5 771.0 L 875.5 764.0 L 870.0 758.5 L 856.5 750.0 L 847.5 731.0 L 834.0 716.5 L 821.0 708.5 L 808.0 704.5 L 799.0 704.5 L 800.0 700.5 L 780.0 689.5 L 722.0 666.5 L 716.5 662.0 L 715.5 650.0 L 710.0 643.5 L 705.0 641.5 L 688.0 641.5 L 683.5 644.0 L 682.5 651.0 L 689.0 666.5 L 752.0 692.5 L 793.0 711.5 L 808.0 722.5 L 824.5 738.0 L 834.5 751.0 L 840.5 762.0 L 838.5 766.0 L 828.0 772.5 L 816.0 774.5 L 815.5 762.0 L 811.5 753.0 L 805.5 744.0 L 794.0 732.5 L 786.0 729.5 L 777.0 728.5 L 765.0 729.5 L 764.5 728.0 L 768.0 724.5 L 774.0 722.5 L 774.5 721.0 L 767.0 719.5 L 734.0 699.5 L 701.0 684.5 L 681.0 679.5 L 670.0 679.5 L 668.5 671.0 L 664.0 665.5 L 657.0 663.5 L 651.0 664.5 L 646.5 669.0 L 643.5 677.0 L 645.5 705.0 L 644.5 753.0 L 643.5 756.0 L 641.0 756.5 L 637.5 748.0 L 633.0 742.5 L 627.0 739.5 L 620.5 740.0 L 623.5 754.0 L 623.5 772.0 L 620.5 790.0 L 616.0 803.5 L 614.5 795.0 L 611.0 789.5 L 603.0 783.5 L 595.0 782.5 L 593.5 798.0 L 589.5 812.0 L 581.5 826.0 L 567.0 840.5 L 564.5 841.0 L 566.5 830.0 L 566.5 816.0 L 565.5 807.0 L 564.0 806.5 L 549.5 828.0 L 532.0 845.5 L 514.0 858.5 L 512.5 858.0 L 519.5 849.0 L 523.5 840.0 L 526.5 828.0 L 526.0 823.5 L 503.0 844.5 L 479.0 860.5 L 487.5 844.0 L 495.5 819.0 L 501.5 788.0 L 500.0 786.5 L 461.5 835.0 L 427.0 869.5 L 409.0 883.5 Z" fill="currentColor"/>
                        </svg>
                    </a>
                </div>
            `;

        // Event listeners
        // Note: the 6 speed-dial actions (new claude project / new
        // session / open-folder / openclaw / hermes / console) are wired in
        // setupNewFab() - the inline speed-dial sits to the right of the
        // "running sessions" section heading on the launchpad screen.

        this.renderHomeBarVersion();
        this.wireServerControls();

        this.initSectionDisclosures();

        // Note: loadProjects() will be called by App.showLaunchpad().
        // Running-sessions row/X click handlers land in Task 10 via event
        // delegation on #running-sessions-list.
    }

    /**
     * Stamp the app version into the home bar's chip.
     *
     * The version is resolved server-side and stamped into the
     * `cloude-app-version` meta tag by src/main.py; this markup is built
     * at runtime and so has no server-rendered token of its own. An
     * absent or empty meta leaves the chip empty, which
     * `.home-bar__version:empty` then removes from the layout - a blank
     * gap beside the bird would read as a broken control.
     *
     * @returns {void}
     */
    renderHomeBarVersion() {
        const chip = document.getElementById('home-bar-version');
        if (!chip) return;
        const meta = document.querySelector('meta[name="cloude-app-version"]');
        const version = meta ? (meta.getAttribute('content') || '').trim() : '';
        chip.textContent = version;
    }

    /**
     * Wire the home bar's server-controls trigger to its menu.
     *
     * The menu itself (rows, icons, what each row does) lives in
     * client/js/server-controls-menu.js and rides the shared FabMenu
     * plumbing. Wiring is idempotent, so a re-render that mints a fresh
     * button cannot double-bind the click.
     *
     * @returns {void}
     */
    wireServerControls() {
        const btn = document.getElementById('server-controls-btn');
        if (!btn) return;
        if (window.ServerControlsMenu && typeof window.ServerControlsMenu.wire === 'function') {
            window.ServerControlsMenu.wire(btn);
            return;
        }
        // The module is a plain script with no load guarantee relative to
        // this render. Say so rather than leaving a dead button: a
        // control that does nothing when pressed is the worse failure.
        btn.disabled = true;
        btn.setAttribute('title', 'server controls unavailable');
        console.warn('Launchpad: ServerControlsMenu not loaded; server controls disabled');
    }

    /**
     * Wire up the two launchpad section headings ("running sessions" and
     * "projects") as real collapsible disclosures. Collapsed state
     * persists per-section in localStorage under
     * `cloude.launchpad.collapsed`, following the same convention as
     * `cloude.theme` / `cloude.audio.volume`.
     *
     * There used to be a third, "server management". Its one control now
     * lives in the home bar's server-controls menu.
     */
    initSectionDisclosures() {
        const collapsedState = this.getLaunchpadCollapsedState();
        const sections = [
            { id: 'running-sessions', toggleId: 'running-sessions-toggle', contentId: 'running-sessions-list' },
            { id: 'recent-projects', toggleId: 'projects-section-toggle', contentId: 'project-list' },
        ];

        sections.forEach(({ id, toggleId, contentId }) => {
            const toggle = document.getElementById(toggleId);
            const content = document.getElementById(contentId);
            if (!toggle || !content) return;

            this.setSectionExpanded(toggle, content, !collapsedState[id]);

            toggle.addEventListener('click', () => {
                const nowExpanded = toggle.getAttribute('aria-expanded') !== 'true';
                this.setSectionExpanded(toggle, content, nowExpanded);
                this.setLaunchpadSectionCollapsed(id, !nowExpanded);
            });
        });
    }

    /**
     * Apply expanded/collapsed visual + a11y state to one disclosure toggle
     * and its content region.
     *
     * Uses `style.display` rather than the `hidden` attribute: `.project-list`
     * sets `display: flex` in the stylesheet, which (author origin) would
     * win the cascade over the UA `[hidden] { display: none }` rule and
     * silently no-op the collapse for that section.
     *
     * @param {HTMLElement} toggle - the <button> heading control
     * @param {HTMLElement} content - the region it shows/hides
     * @param {boolean} expanded - true to show content, false to collapse
     */
    setSectionExpanded(toggle, content, expanded) {
        toggle.setAttribute('aria-expanded', String(expanded));
        content.style.display = expanded ? '' : 'none';
    }

    /**
     * Read the persisted collapsed-state map for launchpad sections.
     *
     * @returns {Object<string, boolean>} section id -> collapsed
     */
    getLaunchpadCollapsedState() {
        try {
            const raw = localStorage.getItem('cloude.launchpad.collapsed');
            return raw ? JSON.parse(raw) : {};
        } catch (err) {
            console.warn('Launchpad: failed to read collapsed-section state:', err);
            return {};
        }
    }

    /**
     * Persist one section's collapsed flag into the shared state map.
     *
     * @param {string} sectionId - e.g. "running-sessions"
     * @param {boolean} collapsed
     */
    setLaunchpadSectionCollapsed(sectionId, collapsed) {
        const state = this.getLaunchpadCollapsedState();
        state[sectionId] = collapsed;
        try {
            localStorage.setItem('cloude.launchpad.collapsed', JSON.stringify(state));
        } catch (err) {
            console.warn('Launchpad: failed to persist collapsed-section state:', err);
        }
    }

    /**
     * Group running sessions by project for the home-screen tree (S8).
     *
     * Description: cross-references ``this.runningSessions`` (live tmux
     *   probe) against ``this.sessionAttribution`` (datastore-backed
     *   ``project_id`` / ``project_attribution`` per tmux name, from
     *   GET /sessions/records) to decide which project owns each running
     *   session. THREE-OUTCOME RULE: a session with a resolved
     *   ``project_id`` becomes that project's child; ``project_attribution
     *   === 'none'`` (working directory WAS read and sits inside no known
     *   project - a complete, actionable answer) becomes a child of the
     *   synthetic "no project" group; ``project_attribution === 'unknown'``
     *   (the working directory could not be read - NOT an answer), a
     *   session absent from the attribution map, or a wholesale fetch
     *   failure (``sessionAttributionListingOk === false``) all land in
     *   ``needsAttention`` instead - excluded from every project's
     *   children and from "no project", because an unproven answer must
     *   never render as if it were measured.
     * Inputs: none (reads ``this.runningSessions``,
     *   ``this.sessionAttribution``, ``this.sessionAttributionListingOk``).
     * Output: {byProjectId: Map<number, object[]>, noProject: object[],
     *   needsAttention: Array<{session: object, reason: string}>}
     * Example: const {byProjectId} = lp._buildProjectSessionGroups();
     */
    _buildProjectSessionGroups() {
        const byProjectId = new Map();
        const noProject = [];
        const needsAttention = [];
        const sessions = this.runningSessions || [];
        for (const s of sessions) {
            if (!this.sessionAttributionListingOk) {
                needsAttention.push({
                    session: s,
                    reason: this.sessionAttributionListingDetail
                        || 'project attribution could not be read',
                });
                continue;
            }
            // INSTANCE-EXACT FIRST. When this running session carries its
            // own tmux creation epoch (every row on ``runningSessions``
            // does - see ``created_at_epoch`` in loadRunningSessions()),
            // resolve its EXACT stored row via the instance key rather
            // than a name-only guess - this is what stops an archived
            // OLDER instance of the same tmux_name from ever being
            // considered for a currently-live session.
            let rec = null;
            if (s.created_at_epoch) {
                const instanceKey = s.name + '\u0000' + s.created_at_epoch;
                if (this.sessionAttributionByInstance.has(instanceKey)) {
                    rec = this.sessionAttributionByInstance.get(instanceKey);
                }
            }
            // FALLBACK: name-only join, only reached when the exact
            // instance was not found (epoch missing on either side, or a
            // legacy row with no recorded epoch). Never resolves to an
            // archived row - see _resolveSessionAttribution().
            if (!rec) {
                if (this.sessionAttributionAmbiguous.has(s.name)) {
                    needsAttention.push({
                        session: s,
                        reason: 'two stored session records for this name '
                            + 'could not be told apart',
                    });
                    continue;
                }
                rec = this.sessionAttribution.get(s.name) || null;
            }
            if (!rec) {
                needsAttention.push({
                    session: s,
                    reason: 'no stored attribution for this session',
                });
                continue;
            }
            // DELETED WINS OVER LIVE. Deleting is a decision about the
            // user's list, not about the process, so a row he deleted
            // stays off the tree even if its tmux session is somehow
            // still up. Without this the rule would silently read
            // "deleted, unless it happens to still be running", which is
            // the kind of exception nobody can predict from the button's
            // label. The session keeps running and is still reachable
            // from the live sidebar; only this listing forgets it.
            // Reached only via the instance-exact path now (the name-only
            // fallback above never yields an archived row), so this is
            // specifically "the live session's OWN record was deleted",
            // not "some unrelated older instance of this name was".
            if (rec.archived_at) continue;
            const attribution = rec.project_attribution;
            if (attribution === 'unknown') {
                needsAttention.push({
                    session: s,
                    reason: 'working directory could not be read',
                });
            } else if (attribution === 'none') {
                noProject.push(s);
            } else if (rec.project_id !== null && rec.project_id !== undefined) {
                const list = byProjectId.get(rec.project_id) || [];
                list.push(s);
                byProjectId.set(rec.project_id, list);
            } else {
                // Defensive: an attribution string that isn't 'none' or
                // 'unknown' but carries no id is not a shape this build
                // should trust - never guess which project it meant.
                needsAttention.push({
                    session: s,
                    reason: 'project attribution missing an id',
                });
            }
        }
        // ENDED SESSIONS, APPENDED AFTER THE LIVE PASS. This loop is the
        // whole fix for "recent shows two, the tree shows one": the loop
        // above can only ever describe what the LIVE tmux probe named, so
        // a session whose instance is gone was invisible to the tree by
        // construction - not filtered out, unable to appear at all.
        //
        // APPENDED, NOT MERGED IN, and the order is the point: a dead row
        // sitting above the session he is actually working in is
        // technically correct and practically wrong. Live first, ended
        // underneath, in every group.
        for (const s of this._endedSessionsForTree()) {
            if (s.project_attribution === 'unknown') {
                needsAttention.push({
                    session: s,
                    reason: 'ended; working directory could not be read',
                });
            } else if (s.project_attribution === 'none') {
                noProject.push(s);
            } else if (s.project_id !== null && s.project_id !== undefined) {
                const list = byProjectId.get(s.project_id) || [];
                list.push(s);
                byProjectId.set(s.project_id, list);
            } else {
                needsAttention.push({
                    session: s,
                    reason: 'ended; project attribution missing an id',
                });
            }
        }
        return { byProjectId, noProject, needsAttention };
    }

    /**
     * The stored sessions that ENDED, shaped like running-session rows.
     *
     * Description: reads ``this.sessionRecords`` (GET /sessions/records)
     *   and returns the rows the tree must show but no live probe can
     *   name. Three filters, each load-bearing:
     *
     *     archived_at        DELETED. The user pressed delete; it is off
     *                        his screens. This is the ONE thing that
     *                        hides a row, and it hides it whatever its
     *                        lifecycle - including a row that is somehow
     *                        still running, or "delete" would quietly
     *                        mean "delete unless it is busy".
     *     lifecycle          only 'stopped' / 'dead' are ENDED. A
     *                        'running' row is the live probe's business
     *                        and is already in the list above; an
     *                        'unknown' row is a CANNOT DETERMINE and is
     *                        NOT quietly promoted to ended here - the
     *                        live pass already routes it to NEEDS
     *                        ATTENTION, which is where an unevaluable
     *                        row belongs.
     *     live name          a name the probe DID list is a live session,
     *                        so the stored row is merely stale. The live
     *                        answer wins; never render both.
     *
     *   THE THREE-OUTCOME GATE. When ``sessionAttributionListingOk`` is
     *   false the records fetch did not answer, so this returns NOTHING.
     *   Inventing ended rows out of a failed read would be the exact
     *   false green this screen keeps removing - and the live pass
     *   already routes every row to NEEDS ATTENTION in that case, which
     *   is the honest report.
     * Inputs: none (reads ``this.sessionRecords``, ``this.runningSessions``
     *   and the listing latch).
     * Output: object[] - rows shaped like ``this.runningSessions`` entries
     *   plus ``ended: true``, ``session_uuid`` and the record's project
     *   attribution. Empty when the fetch did not answer.
     * Example: lp._endedSessionsForTree()  // [{name:'cloude_a', ended:true, ...}]
     */
    _endedSessionsForTree() {
        if (!this.sessionAttributionListingOk) return [];
        const liveNames = new Set((this.runningSessions || []).map(s => s.name));
        const out = [];
        for (const rec of (this.sessionRecords || [])) {
            if (!rec || rec.archived_at) continue;
            if (rec.lifecycle !== 'stopped' && rec.lifecycle !== 'dead') continue;
            if (rec.tmux_name && liveNames.has(rec.tmux_name)) continue;
            out.push({
                name: rec.tmux_name || '',
                label: rec.title || null,
                session_uuid: rec.session_uuid,
                ended: true,
                status: 'stopped',
                is_active: false,
                created_by_cloude: !!rec.owned,
                created_at_epoch: rec.tmux_created_epoch || 0,
                working_dir: rec.working_dir || '',
                agent_type: rec.agent_type || '',
                agent_family: rec.agent_family || null,
                agent_family_source: rec.agent_family_source || null,
                project_id: rec.project_id,
                project_attribution: rec.project_attribution,
            });
        }
        return out;
    }

    /**
     * Build one child session row for the project tree (S8).
     *
     * Description: a read-only summary row (status dot, name, EXTERNAL/
     *   TMUX ownership badge, agent-family pill) reusing the exact same
     *   per-session helpers the flat "running sessions" list uses
     *   (``_renderFamilyPillHtml``, ``SessionStatusUI.dotHtml``), so a
     *   session never looks different depending on which surface drew
     *   it. Clicking the row opens/adopts the session via
     *   ``_bindProjectSessionRowClicks`` - kill/rename/mark-unread stay
     *   exclusively on the flat list above, this row does not duplicate
     *   those controls.
     * Inputs: s (object) - one running-session row (same shape as
     *   ``this.runningSessions`` entries).
     * Output: string - HTML for one ``.project-session-row``.
     */
    _renderTreeSessionRowHtml(s) {
        if (s.ended) return this._renderEndedTreeSessionRowHtml(s);
        const owned = !!s.created_by_cloude;
        const displayName = this._sessionDisplayLabel(s);
        const escapedName = this._escapeHtml(s.name);
        const escapedDisplay = this._escapeHtml(displayName);
        const statusDot = window.SessionStatusUI
            ? window.SessionStatusUI.dotHtml(s.status)
            : '';
        return `
                <div class="project-session-row" data-name="${escapedName}" data-active="${s.is_active ? '1' : '0'}" role="button" tabindex="0">
                  ${statusDot}
                  <span class="project-session-row__name">${escapedDisplay}</span>
                  <span class="badge ${owned ? 'badge-tmux' : 'badge-external'}">${owned ? 'TMUX' : 'EXTERNAL'}</span>
                  ${this._renderFamilyPillHtml(s.agent_family, s.agent_family_source)}
                </div>
            `;
    }

    /**
     * Build one ENDED child row for the project tree.
     *
     * Description: the same shape as a live row so the tree does not
     *   visually fragment, differing in exactly the ways a human must be
     *   able to see WITHOUT hovering anything:
     *
     *     - the word ENDED, as text. Colour alone never carries meaning
     *       here, and a dot is a 9px hint at best.
     *     - the `stopped` status dot from the shared vocabulary, not a
     *       second signal invented for this surface.
     *     - NO `role="button"` and NO `tabindex`. This is the important
     *       one. A dead row that looks identical to a live one is worse
     *       than a hidden one, because he would click it and try to
     *       attach to a tmux session that does not exist.
     *     - restart and delete instead, which are the only two things
     *       that CAN be done to a record with no process behind it.
     *       Restart is the same action RECENT already offers: a NEW
     *       session in the same working directory, never a resurrection.
     *
     *   `data-uuid` carries ``session_uuid`` because delete is keyed on
     *   it, never on the tmux name - tmux reuses names, and two rows can
     *   differ only by creation epoch.
     * Inputs: s (object) - one row from ``_endedSessionsForTree``.
     * Output: string - HTML for one ``.project-session-row--ended``.
     */
    _renderEndedTreeSessionRowHtml(s) {
        const owned = !!s.created_by_cloude;
        const escapedName = this._escapeHtml(s.name);
        const escapedDisplay = this._escapeHtml(this._sessionDisplayLabel(s));
        const uuid = this._escapeHtml(s.session_uuid || '');
        const statusDot = window.SessionStatusUI
            ? window.SessionStatusUI.dotHtml('stopped')
            : '';
        return `
                <div class="project-session-row project-session-row--ended" data-name="${escapedName}" data-ended="1" data-lifecycle="stopped" data-uuid="${uuid}">
                  ${statusDot}
                  <span class="project-session-row__name">${escapedDisplay}</span>
                  <span class="badge badge-ended">ENDED</span>
                  <span class="badge ${owned ? 'badge-tmux' : 'badge-external'}">${owned ? 'TMUX' : 'EXTERNAL'}</span>
                  ${this._renderFamilyPillHtml(s.agent_family, s.agent_family_source)}
                  <button type="button" class="ended-session-restart" data-uuid="${uuid}" data-working-dir="${this._escapeHtml(s.working_dir || '')}" data-agent-type="${this._escapeHtml(s.agent_type || '')}">restart</button>
                  <button type="button" class="ended-session-delete" data-uuid="${uuid}" title="delete this session from your lists (the record is kept)" aria-label="delete this session from your lists">delete</button>
                </div>
            `;
    }

    /**
     * Build the synthetic "no project" group (S8).
     *
     * Description: the home for every RUNNING session whose working
     *   directory WAS read and sits inside no known project
     *   (``project_attribution === 'none'``). It is a real, measured
     *   answer - distinct from NEEDS ATTENTION, which is reserved for
     *   sessions that could NOT be evaluated - so it renders as an
     *   ordinary (collapsible) group, never as a warning. Omitted
     *   entirely when empty, matching every other optional group on this
     *   screen.
     * Inputs: sessions (object[]) - rows attributed 'none'.
     * Output: string - HTML for one ``.project-node--virtual``, or ''.
     */
    _renderNoProjectGroupHtml(sessions) {
        if (!sessions || sessions.length === 0) return '';
        const nodeKey = '__no_project__';
        const collapsed = this._collapsedProjectNodes.has(nodeKey);
        const rows = sessions.map(s => this._renderTreeSessionRowHtml(s)).join('');
        return `
                <div class="project-node project-node--virtual" data-project-node="no-project">
                  <button type="button" class="project-node__header project-node__toggle" data-node-key="${nodeKey}" aria-expanded="${!collapsed}" aria-controls="project-node-sessions-${nodeKey}">
                    <span class="project-node__chevron" aria-hidden="true">►</span>
                    <span class="project-node__title">no project</span>
                    <span class="project-node__count">${sessions.length} session${sessions.length === 1 ? '' : 's'}</span>
                  </button>
                  <div class="project-node__sessions" id="project-node-sessions-${nodeKey}" style="${collapsed ? 'display:none;' : ''}">${rows}</div>
                </div>
            `;
    }

    /**
     * Build the NEEDS ATTENTION group for un-attributable running
     * sessions (S8) - the session-level counterpart to S3's project
     * presence badges and this file's ``_renderListingAttentionHtml``
     * for the running-sessions probe. Reuses the same "NEEDS ATTENTION"
     * visual language (see client/css/styles.css) rather than inventing
     * a new one.
     *
     * Description: never collapsible and never offers any action - the
     *   row exists so an unattributed session is visible and named, not
     *   silently dropped from the tree and not guessed into a project.
     * Inputs: items (Array<{session: object, reason: string}>).
     * Output: string - HTML, or '' when there is nothing to report.
     */
    _renderProjectAttentionGroupHtml(items) {
        if (!items || items.length === 0) return '';
        const rows = items.map(({ session, reason }) => {
            const displayName = this._escapeHtml(
                this._sessionDisplayLabel(session)
            );
            return `
                <div class="project-session-row project-session-row--attention" data-name="${this._escapeHtml(session.name)}">
                  <span class="project-session-row__name">${displayName}</span>
                  <span class="project-session-row__attention-reason">${this._escapeHtml(reason)}</span>
                </div>
            `;
        }).join('');
        return `
                <div class="project-node project-node--attention" data-project-node="needs-attention" role="status">
                  <div class="project-node__header project-node__header--attention">
                    <span class="project-node__attention-head">NEEDS ATTENTION</span>
                    <span class="project-node__title">${items.length} session${items.length === 1 ? '' : 's'} could not be attributed to a project</span>
                  </div>
                  <div class="project-node__sessions">${rows}</div>
                </div>
            `;
    }

    /**
     * Wire click-to-expand/collapse on every ``.project-node__toggle`` in
     * the tree, via event delegation on the (stable) ``#project-list``
     * container so re-renders never need to re-bind. State is kept in
     * ``this._collapsedProjectNodes`` (a Set of node keys) so it survives
     * the next ``renderProjectList()`` call - e.g. the 5s running-
     * sessions poller repainting the tree does not snap a collapsed
     * project back open.
     */
    _bindProjectNodeToggles() {
        const container = document.getElementById('project-list');
        if (!container || container.__boundNodeToggles) return;
        container.__boundNodeToggles = true;
        container.addEventListener('click', (e) => {
            const toggle = e.target.closest('.project-node__toggle');
            if (!toggle) return;
            e.stopPropagation();
            const key = toggle.getAttribute('data-node-key');
            if (!key) return;
            const nowExpanded = toggle.getAttribute('aria-expanded') !== 'true';
            this._applyProjectNodeCollapsed(toggle, !nowExpanded);
            if (nowExpanded) {
                this._collapsedProjectNodes.delete(key);
            } else {
                this._collapsedProjectNodes.add(key);
            }
        });
    }

    /**
     * Show or hide one tree node's foldable parts, addressed from the
     * node ROOT rather than by walking siblings off the toggle button.
     *
     * Description: this is the fix for the fold that did nothing. A
     *   PROJECT node nests its toggle inside ``.project-node__row``, so
     *   ``toggle.nextElementSibling`` was the ``.project-item`` card, not
     *   the ``.project-node__sessions`` container one level up. The old
     *   code guarded on the class, found the wrong element, and silently
     *   changed nothing while still flipping ``aria-expanded`` and
     *   recording the new state - so the sessions only appeared to fold
     *   later, when the 5s poller happened to re-render. The synthetic
     *   "no project" node DID fold, because there the toggle is the
     *   header and the container really is its next sibling, which is
     *   why the bug read as "sometimes works". Resolving from
     *   ``closest('.project-node')`` makes both shapes take the same
     *   path and removes the dependence on sibling order entirely.
     *   Both foldable parts move together: the child session rows and
     *   the project description.
     * Inputs: toggle (HTMLElement) - the ``.project-node__toggle``
     *   clicked or being re-applied; collapsed (boolean) - true to hide.
     * Output: boolean - true when a ``.project-node`` root was found and
     *   updated, false when the toggle sits outside one (nothing was
     *   changed, and nothing is claimed to have been).
     * Example: lp._applyProjectNodeCollapsed(btn, true);
     */
    _applyProjectNodeCollapsed(toggle, collapsed) {
        toggle.setAttribute('aria-expanded', String(!collapsed));
        const node = toggle.closest('.project-node');
        if (!node) return false;
        const display = collapsed ? 'none' : '';
        node.querySelectorAll('.project-node__sessions').forEach((el) => {
            el.style.display = display;
        });
        node.querySelectorAll('.project-description').forEach((el) => {
            el.style.display = display;
        });
        return true;
    }

    /**
     * Wire click-to-open on every child ``.project-session-row`` in the
     * tree (excluding the inert ``--attention`` variant, which carries
     * no ``data-active`` and offers no action). Delegated the same way
     * as ``_bindProjectNodeToggles``. Routes into the exact same
     * open/adopt methods the flat running-sessions list uses, so a
     * session behaves identically whichever surface it was clicked from.
     */
    _bindProjectSessionRowClicks() {
        const container = document.getElementById('project-list');
        if (!container || container.__boundSessionRowClicks) return;
        container.__boundSessionRowClicks = true;
        container.addEventListener('click', async (e) => {
            const row = e.target.closest('.project-session-row');
            if (!row || row.classList.contains('project-session-row--attention')) return;
            e.stopPropagation();
            // AN ENDED ROW MUST NOT REACH THE ATTACH PATH. Dropping
            // role="button" off the markup stops it LOOKING clickable,
            // but this listener is delegated on the container and matches
            // the row class, not the role - so without this guard the
            // whole row would still attach on click and fail against a
            // tmux session that no longer exists. Its two real actions
            // are handled below and return before this point.
            if (row.dataset.ended === '1') {
                const restart = e.target.closest('.ended-session-restart');
                if (restart) {
                    await this._restartRecentSession(
                        restart.getAttribute('data-working-dir'),
                        restart.getAttribute('data-agent-type')
                    );
                    return;
                }
                const del = e.target.closest('.ended-session-delete');
                if (del) await this._deleteSessionRecord(del.getAttribute('data-uuid'));
                return;
            }
            const name = row.dataset.name;
            const isActive = row.dataset.active === '1';
            if (isActive) {
                const live = (this.runningSessions || []).find(s => s.name === name);
                await this._returnToActiveRunningSession(live ? live.session_id : null);
                return;
            }
            await this._handleAttachRunningSession(name);
        });
    }

    /**
     * Render the project list as a two-level project-to-session tree
     * (S8, design section 4.2). Projects are the parents; their RUNNING
     * sessions (from ``this.runningSessions``, matched via
     * ``this.sessionAttribution``) are the children, via
     * ``_buildProjectSessionGroups``. A project whose ``presence`` is
     * ``missing`` or ``unreachable`` still renders here with S3's badge
     * and every action on its row refused - it is never dropped from
     * the list, matching the three-outcome rule this whole screen
     * follows.
     */
    renderProjectList() {
        const projectListEl = document.getElementById('project-list');
        if (!projectListEl) return;

        // feat/db-is-authoritative - the provenance banner is drawn in
        // BOTH the empty and populated cases. An empty list is exactly
        // when the user most needs to know whether the datastore
        // answered, because "no projects" and "could not read your
        // projects" look identical without it.
        const authorityHtml = this._renderProjectAuthorityBannerHtml();

        if (this.projects.length === 0) {
            projectListEl.innerHTML = authorityHtml + `
                <div class="launchpad-empty">
                    no projects yet<br>
                    <small style="color: #666;">use + new to add one</small>
                </div>
            `;
            return;
        }

        const groups = this._buildProjectSessionGroups();

        // Render projects
        const projectNodesHtml = this.projects.map((project, index) => {
            // SLIM ROW. A project with no description used to render the
            // literal filler "no description": a full line of type on
            // every row that says nothing. All 9 projects in the live
            // datastore have an empty description, so on the real screen
            // the filler was the single largest avoidable cost. No
            // description now means no element at all. The text is also
            // escaped here - it was interpolated raw, and a description
            // is user-supplied text that reaches this template.
            const rawDescription = (project.description || '').trim();
            const hasDescription = rawDescription.length > 0;
            const description = this._escapeHtml(rawDescription);
            // feat/projects-table (S3) - presence badge. `presenceRow` is
            // undefined for a project the DB import has not seen yet
            // (created via config write after the one-time boot import);
            // that renders exactly like 'unchecked' - normal, no badge,
            // every action allowed, because "not yet probed" is not
            // evidence of anything wrong. Only 'missing' and
            // 'unreachable' change the row: they get a visibly distinct
            // badge (different label AND different color, see
            // client/css/styles.css) and every action on the row -
            // opening it, editing it, removing it - is refused, matching
            // design section 4.1's "every action refused" for both
            // states. The two states are never rendered the same way:
            // collapsing "your project is gone" and "I could not check"
            // into one look is the exact bug this table exists to kill.
            // Presence is indexed by BOTH raw path and normalised root
            // (see loadProjectPresence). Root is tried first because the
            // authoritative project list is keyed by root, and two
            // spellings of the same folder must resolve to one badge.
            const presenceRow = (project.root && this.projectPresence.get(project.root))
                || this.projectPresence.get(project.path);
            const presenceState = presenceRow ? presenceRow.presence : 'unchecked';
            const isDisabled = presenceState === 'missing' || presenceState === 'unreachable';
            let presenceBadge = '';
            if (presenceState === 'missing') {
                presenceBadge = `<div class="project-presence-badge project-presence-badge-missing">MISSING - folder not found</div>`;
            } else if (presenceState === 'unreachable') {
                const detail = presenceRow && presenceRow.presence_detail
                    ? this._escapeHtml(presenceRow.presence_detail)
                    : 'reason unknown';
                presenceBadge = `<div class="project-presence-badge project-presence-badge-unreachable">CANNOT DETERMINE - ${detail}</div>`;
            }
            const itemClasses = isDisabled
                ? `project-item project-presence-disabled project-presence-${presenceState}`
                : 'project-item';

            // S8, revised by feat/db-is-authoritative - a project's row
            // id now arrives ON THE PROJECT ITSELF, from GET /projects,
            // which reads the authoritative `projects` table.
            //
            // It used to be looked up here in the PRESENCE map, keyed by
            // raw config path. That is what produced the triplication:
            // config.json carried three entries ("test pause",
            // "ses_ec5bf2a3", "qqwe") all pointing at
            // /Users/jsugamele/Development/ses_ec5bf2a3, all three found
            // the SAME presence row, and all three therefore drew the
            // same two child sessions. The list is now one entry per
            // unique root by construction, so that cannot recur.
            //
            // `project.id` is null in the degraded config.json fallback -
            // a config entry has no row - and null means "no children we
            // can prove", never row 0. The presence map is still
            // consulted as a fallback so a project the boot import has
            // not reached yet still resolves.
            const projectId = (project.id !== null && project.id !== undefined)
                ? project.id
                : (presenceRow ? presenceRow.id : null);
            const children = (projectId !== null && projectId !== undefined)
                ? (groups.byProjectId.get(projectId) || [])
                : [];
            const nodeKey = `project:${project.name}`;
            const collapsed = this._collapsedProjectNodes.has(nodeKey);
            const hasChildren = children.length > 0;
            // The node is foldable when it has something to fold: child
            // sessions, a description, or both. The count chip is drawn
            // only when there ARE children, because a bare "0" would be
            // a claim about sessions that the fold is not making.
            const foldable = hasChildren || hasDescription;
            const countHtml = hasChildren
                ? `<span class="project-node__count">${children.length}</span>`
                : '';
            const controlsAttr = hasChildren
                ? ` aria-controls="project-node-sessions-${this._escapeHtml(nodeKey)}"`
                : '';
            const chevronHtml = foldable
                ? `<button type="button" class="project-node__toggle" data-node-key="${this._escapeHtml(nodeKey)}" aria-expanded="${!collapsed}" aria-label="toggle details for ${this._escapeHtml(project.name)}"${controlsAttr}><span class="project-node__chevron" aria-hidden="true">►</span>${countHtml}</button>`
                : '';
            const sessionsHtml = hasChildren
                ? `<div class="project-node__sessions" id="project-node-sessions-${this._escapeHtml(nodeKey)}" style="${collapsed ? 'display:none;' : ''}">${children.map(s => this._renderTreeSessionRowHtml(s)).join('')}</div>`
                : '';
            // Item 43: the description is the part of the row that a
            // collapsed node sheds. Rendered with the collapse already
            // applied so a re-render (the 5s poller) repaints the same
            // state the user last chose, exactly as sessionsHtml does.
            const descriptionHtml = hasDescription
                ? `<div class="project-description"${collapsed ? ' style="display:none;"' : ''}>${description}</div>`
                : '';

            return `
                <div class="project-node" data-project-node="project" data-project-name="${this._escapeHtml(project.name)}">
                  <div class="project-node__row">
                    ${chevronHtml}
                    <div class="${itemClasses}" data-index="${index}" data-name="${project.name}"${isDisabled ? ' aria-disabled="true"' : ''}>
                        <button class="project-edit-btn" data-name="${project.name}" title="edit project" aria-label="edit project"${isDisabled ? ' disabled' : ''}>${window.SessionStatusUI ? window.SessionStatusUI.pencilIconSvg() : ''}</button>
                        <button class="project-delete-btn" data-name="${project.name}" title="remove project from the launcher" aria-label="remove project from the launcher"${isDisabled ? ' disabled' : ''}>${window.SessionStatusUI ? window.SessionStatusUI.trashIconSvg() : '&times;'}</button>
                        <div class="project-name">» ${project.name}</div>
                        <div class="project-path">${project.path}</div>
                        ${descriptionHtml}
                        ${presenceBadge}
                    </div>
                  </div>
                  ${sessionsHtml}
                </div>
            `;
        }).join('');

        const noProjectHtml = this._renderNoProjectGroupHtml(groups.noProject);
        const attentionHtml = this._renderProjectAttentionGroupHtml(groups.needsAttention);

        projectListEl.innerHTML = authorityHtml + projectNodesHtml + noProjectHtml + attentionHtml;

        this._bindProjectNodeToggles();
        this._bindProjectSessionRowClicks();

        // Add click handlers for project selection
        const projectItems = projectListEl.querySelectorAll('.project-item');
        projectItems.forEach(item => {
            item.addEventListener('click', (e) => {
                // Don't open project if clicking an inline action button
                if (e.target.classList.contains('project-delete-btn') ||
                    e.target.classList.contains('project-edit-btn')) {
                    return;
                }
                // MISSING and CANNOT DETERMINE rows refuse every action -
                // design section 4.1. The row still exists so it stays
                // visible and can never be silently opened into a stale
                // or unreachable directory.
                //
                // REFUSING IS NOT THE SAME AS DOING NOTHING. This used to be
                // a bare `return`: the click was swallowed with no message,
                // no log line and no request, so the row presented to the
                // user as a button that does nothing. Refusal has to SAY it
                // refused and name the path, or the user cannot tell a
                // deliberate refusal from a broken app - and on a fresh
                // install every seeded row was in this state, so the whole
                // first screen was dead clicks.
                if (item.classList.contains('project-presence-disabled')) {
                    const idx = parseInt(item.dataset.index);
                    const p = this.projects[idx];
                    this._explainRefusedProject(p, item);
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
            // Trash means the same thing on a project row as on a session
            // row: forget the entry, touch nothing on disk. The copy says
            // so plainly rather than borrowing a "cannot be undone"
            // warning this action does not earn.
            const confirmed = await this.showConfirmModal(
                'remove project',
                `remove "${projectName}" from the launcher?`,
                'this only removes it from the launcher. the folder and its files on disk are not touched.',
                'remove',
                'cancel'
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
     * Display name only - the folder on disk is never touched.
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
     * with an error whose ``message`` contains "already exists" - handled
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
                                the folder on disk is never renamed - only the launcher label changes.
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

    // THE SERVER-RESTART CONTROL WAS REMOVED HERE, deliberately.
    //
    // restartServer() called API.resetServer() -> POST /api/v1/server/reset
    // -> reset.sh from the server's own root. reset.sh has never shipped in
    // macOS/package.json's build.extraResources, so on a packaged install
    // that endpoint returned a 500 naming the missing file, every time. The
    // control existed only to teach the user the app was broken.
    //
    // Shipping the script would not have fixed it: a process restart belongs
    // to whatever SUPERVISES the process, and the python server never
    // supervises itself. The full argument, and where each install shape's
    // real restart lives, is at the removal site in src/api/routes.py.
    //
    // If this comes back, it comes back as an action the supervisor performs.

    /**
     * Show confirmation modal.
     *
     * Thin delegate to `App.showConfirmModal()` - that is the ONE
     * confirmation-modal implementation in the app (title/message
     * escaping, Escape/cancel/click-outside handling, focus management
     * all live there). Kept as a same-named method here purely so the
     * launchpad's existing call sites (delete project, kill running
     * session, reset server) don't need to change; do not re-implement
     * the modal here.
     * @param {string} title - Modal title
     * @param {string} message - Main message
     * @param {string} [details] - Additional details (optional)
     * @param {string} [primaryLabel='confirm'] - Label for the primary (destructive / intent) button
     * @param {string} [secondaryLabel='cancel'] - Label for the safe no-op button
     * @returns {Promise<boolean>} - True if confirmed, false if cancelled. Cancel is ALWAYS a no-op - callers must never map cancel to a destructive action.
     */
    showConfirmModal(title, message, details = null, primaryLabel = 'confirm', secondaryLabel = 'cancel') {
        return window.App.showConfirmModal(title, message, details, primaryLabel, secondaryLabel);
    }

    /**
     * Present a one-of-N choice as a modal and resolve the chosen key.
     *
     * Description: the shared list-picker behind "new claude project" and
     *   "new session". Reuses the folder-picker visual language already
     *   in this file (``.folder-picker-list`` / ``.folder-picker-item``)
     *   rather than inventing a second kind of list. A row may be
     *   DISABLED with a stated reason, which is how a project whose
     *   presence is 'missing' or 'unreachable' stays VISIBLE and NAMED
     *   while refusing to be opened, matching the row treatment on the
     *   home screen itself. When there is nothing to choose from, the
     *   caller's own message is shown instead of an empty box, because
     *   "you have none" and "I could not find out" are different answers
     *   and the caller is the only one that knows which it has.
     * Inputs: options ({title: string, hint?: string, items:
     *   Array<{key: string, label: string, sub?: string,
     *   disabled?: boolean, reason?: string}>, emptyMessage?: string,
     *   emptyKind?: string}).
     * Output: Promise<?string> - the chosen item's key, or null when the
     *   user cancelled or there was nothing selectable.
     * Example: const how = await lp._showChoiceModal({title: 'new claude
     *   project', items: [{key: 'empty', label: 'start empty'}]});
     */
    _showChoiceModal(options) {
        const {
            title = 'choose',
            hint = 'up/down to move . enter to choose . esc to cancel',
            items = [],
            emptyMessage = null,
            emptyKind = 'info',
        } = options || {};
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            const rowsHtml = items.length
                ? items.map((it, i) => {
                    const cls = it.disabled
                        ? 'folder-picker-item folder-picker-item-disabled'
                        : 'folder-picker-item';
                    const sub = it.disabled && it.reason
                        ? `<div class="folder-picker-item-sub">${this._escapeHtml(it.reason)}</div>`
                        : (it.sub ? `<div class="folder-picker-item-sub">${this._escapeHtml(it.sub)}</div>` : '');
                    return `<div class="${cls}" data-choice-index="${i}"${it.disabled ? ' aria-disabled="true"' : ''}>`
                        + `<div class="folder-picker-item-label">${this._escapeHtml(it.label)}</div>${sub}</div>`;
                }).join('')
                : `<div class="folder-picker-empty folder-picker-empty--${this._escapeHtml(emptyKind)}">${this._escapeHtml(emptyMessage || 'nothing to choose from')}</div>`;
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">&raquo; ${this._escapeHtml(title)}</div>
                    <div class="modal-body">
                        <div class="folder-picker-list" tabindex="-1">${rowsHtml}</div>
                        <div class="modal-description">${this._escapeHtml(items.length ? hint : 'esc to close')}</div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" data-choice-cancel>${items.length ? 'cancel' : 'ok'}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const rowEls = Array.from(overlay.querySelectorAll('.folder-picker-item'));
            const selectable = rowEls
                .map((el, i) => (items[i] && !items[i].disabled ? i : -1))
                .filter((i) => i >= 0);
            let active = selectable.length ? selectable[0] : -1;

            const paint = () => {
                rowEls.forEach((el, i) => {
                    el.classList.toggle('folder-picker-item-active', i === active);
                });
                if (active >= 0 && rowEls[active]) {
                    rowEls[active].scrollIntoView({ block: 'nearest' });
                }
            };
            const close = (value) => {
                document.removeEventListener('keydown', onKey, true);
                if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
                resolve(value);
            };
            const step = (dir) => {
                if (!selectable.length) return;
                const at = selectable.indexOf(active);
                const next = at < 0 ? 0 : (at + dir + selectable.length) % selectable.length;
                active = selectable[next];
                paint();
            };
            const onKey = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); return; }
                if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); step(1); return; }
                if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); step(-1); return; }
                if (e.key === 'Enter') {
                    e.preventDefault(); e.stopPropagation();
                    if (active >= 0 && items[active] && !items[active].disabled) close(items[active].key);
                }
            };
            document.addEventListener('keydown', onKey, true);
            rowEls.forEach((el, i) => {
                el.addEventListener('click', () => {
                    if (!items[i] || items[i].disabled) return;
                    close(items[i].key);
                });
            });
            overlay.querySelector('[data-choice-cancel]').addEventListener('click', () => close(null));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
            paint();
        });
    }

    /**
     * The "new claude project" entrypoint, with clone-from-github AND
     * open-from-folder folded in as options.
     *
     * Description: "create new project" never said which agent it was
     *   creating for, and cloning a repo sat beside it as a peer even
     *   though a clone IS a new claude project, just one whose contents
     *   arrive from a remote. This asks how the project should start,
     *   then routes into the three existing flows unchanged - no launch
     *   logic is duplicated or reimplemented here.
     *
     *   "open an existing folder" joined this list for the same reason
     *   the clone did: all three make a claude project, and they differ
     *   only in where the folder comes from - made fresh, cloned from a
     *   remote, or already on disk. It used to be a peer of "new claude
     *   project" in the top-level add menu, which put a THIRD entry point
     *   in front of the user for a decision that is really one branch
     *   inside a single flow. Each option routes straight into the method
     *   that already implemented it, so this method holds no launch logic
     *   of its own and there is nothing here to drift.
     *
     * Inputs: none.
     * Output: Promise<void> - resolves once the chosen flow finishes, or
     *   immediately when the user cancels.
     * Example: await lp.startNewClaudeProject();
     */
    async startNewClaudeProject() {
        const how = await this._showChoiceModal({
            title: 'new claude project',
            items: [
                { key: 'empty', label: 'start empty', sub: 'a fresh working folder' },
                { key: 'clone', label: 'clone from github', sub: 'start from an existing repository' },
                { key: 'folder', label: 'open an existing folder', sub: 'a folder already on this machine' },
            ],
        });
        if (how === 'empty') return this.createNewSession();
        if (how === 'clone') return this.showCloneFromGithubModal();
        if (how === 'folder') return this.openProjectFromFolder();
        return undefined;
    }

    /**
     * The "new session" entrypoint: add a session to a project that
     * ALREADY exists, and never create one.
     *
     * Description: three outcomes, kept distinct. If the project list was
     *   never read successfully (``projectsListingOk === false``) it says
     *   CANNOT DETERMINE and refuses, because an empty list after a failed
     *   fetch is not evidence that there are no projects. If the list WAS
     *   read and is genuinely empty, it says so and points at "new claude
     *   project" instead of opening an empty picker. Otherwise it offers
     *   the projects, with 'missing' and 'unreachable' rows visible,
     *   named and refused exactly as they are on the home screen.
     * Inputs: none.
     * Output: Promise<void>.
     * Example: await lp.startSessionInExistingProject();
     */
    async startSessionInExistingProject() {
        if (this.projectsListingOk === false) {
            await this._showChoiceModal({
                title: 'new session',
                items: [],
                emptyMessage: 'CANNOT DETERMINE which projects you have: the project list could not be read. '
                    + 'This is not a claim that you have none.',
                emptyKind: 'unknown',
            });
            return;
        }
        const projects = this.projects || [];
        if (projects.length === 0) {
            await this._showChoiceModal({
                title: 'new session',
                items: [],
                emptyMessage: 'no claude projects yet. use "new claude project" to make one first.',
                emptyKind: 'info',
            });
            return;
        }
        const items = projects.map((p) => {
            const presenceRow = (p.root && this.projectPresence.get(p.root))
                || this.projectPresence.get(p.path);
            const presence = presenceRow ? presenceRow.presence : 'unchecked';
            const reason = presence === 'missing'
                ? 'MISSING - folder not found'
                : (presence === 'unreachable'
                    ? `CANNOT DETERMINE - ${(presenceRow && presenceRow.presence_detail) || 'reason unknown'}`
                    : null);
            return {
                key: p.name,
                label: p.name,
                sub: p.path,
                disabled: presence === 'missing' || presence === 'unreachable',
                reason,
            };
        });
        const chosen = await this._showChoiceModal({
            title: 'new session in which project',
            items,
        });
        if (!chosen) return;
        const project = projects.find((p) => p.name === chosen);
        if (!project) return;
        await this.selectProject(project);
    }

    /**
     * Create new project with auto-generated workspace.
     * Default behavior - server falls back to ProjectConfig.agent_type
     * or "claude". Does NOT send agent_type in the payload.
     */
    async createNewSession() {
        return this._createNewSessionInner(null);
    }

    /**
     * Create new project pinned to a specific agent (openclaw, hermes, codex).
     * Sends agent_type in the createSession payload so the backend spawns
     * the matching CLI (configured in src/config.py AgentsConfig).
     */
    async createNewSessionWithAgent(agentType) {
        return this._createNewSessionInner(agentType);
    }

    /**
     * Create a plain "console" tmux session in ~/ running $SHELL - no
     * Claude/codex/hermes/openclaw. For quick shell work straight from the
     * launchpad.
     *
     * Auto-generates a name (console-<base36 ts>) - no modal prompt, since
     * a bare shell isn't a "project" in the conventional sense. Still
     * registers a Recent Projects entry so a killed pane can be relaunched
     * the same way every other create path works.
     *
     * @param {{terminalCommandId?: string}} [options] - when
     *   terminalCommandId is set (settings > terminal tab, "run"), the
     *   server types that configured command into the new pane once the
     *   shell is up. Only the ID travels: the command text is read from
     *   config.json server-side and never accepted from the client, and
     *   nothing is exec'd outside this visible tmux pane. See
     *   src/core/terminal_commands.py.
     */
    async createConsoleSession(options = {}) {
        const terminalCommandId = options.terminalCommandId || null;
        console.log('Launchpad: Creating new console session', terminalCommandId || '');

        const sessionName = `console-${Date.now().toString(36)}`;

        try {
            this.updateStatus('creating new console...');

            const _dims = this._getTerminalDims();
            const payload = {
                auto_start_claude: true,   // server gates on this to spawn the command
                copy_templates: false,
                project_name: sessionName,
                working_dir: '~',          // server-side os.path.expanduser
                agent_type: 'shell',
                ...(terminalCommandId ? { terminal_command_id: terminalCommandId } : {}),
                ..._dims
            };
            const session = await window.API.createSession(payload);

            console.log('Launchpad: New console created:', session);

            // Auto-add the project entry (mirrors _createNewSessionInner).
            try {
                await window.API.createProject({
                    name: sessionName,
                    path: session.working_dir,
                    description: 'console session',
                });
            } catch (error) {
                if (!error.message.includes('already exists')) {
                    console.error('Launchpad: Failed to save console project:', error);
                }
            }

            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session }
            }));

        } catch (error) {
            console.error('Launchpad: Failed to create console session:', error);
            if (error.message && error.message.includes('already running')) {
                // Reuse the same detach-and-create handoff the agent paths use.
                this.detachAndCreateNew('shell');
            } else {
                this.showError('failed to create console: ' + (error.message || error));
            }
        }
    }

    /**
     * Inner implementation for createNewSession / createNewSessionWithAgent.
     * @param {string|null} agentType - 'openclaw' | 'hermes' | 'codex' | null
     *   When null, agent_type is OMITTED from the payload (preserves server
     *   fallback behavior for the default "+ new project" FAB action).
     */
    async _createNewSessionInner(agentType = null) {
        console.log('Launchpad: Creating new project', agentType ? `(agent: ${agentType})` : '');

        try {
            // Gate: pick claude vs an OpenRouter model BEFORE asking for a
            // project name. Keyboard-first, defaults to the last choice -
            // this is the hot path of every launch so it must be
            // dismissable in one keystroke. null = user cancelled the
            // whole launch.
            const providerChoice = await this.showProviderModal();
            if (!providerChoice) {
                console.log('Launchpad: Provider selection cancelled');
                return;
            }

            // Show modal to get project details. Title reflects the agent
            // so users know which CLI is about to spawn in the new pane.
            const modalTitle = agentType
                ? `name this ${agentType} project`
                : 'name this project';
            const projectDetails = await this.showProjectNameModal({ title: modalTitle });

            if (!projectDetails) {
                console.log('Launchpad: Project creation cancelled');
                return; // User cancelled
            }

            // Show loading state
            this.updateStatus(
                agentType
                    ? `creating new ${agentType} project...`
                    : 'creating new project...'
            );

            // Create session with auto-generated path and template copying.
            // Include current xterm cell grid dims so the tmux pane is
            // birthed at the right size (avoids the 132x40 default → resize
            // flash before the WS handshake reshapes it).
            const _dims = this._getTerminalDims();
            const payload = {
                auto_start_claude: true,
                copy_templates: true,
                project_name: projectDetails.name,
                ..._dims
            };
            // Only include agent_type when explicitly set, so the server's
            // existing fallback chain (ProjectConfig.agent_type → "claude")
            // continues to work for the default "new-project" button.
            // feat/launch-wrappers - a wrapper choice from the provider
            // modal (providerChoice.wrapperId) ONLY applies when no
            // explicit agentType was already forced by the caller (e.g.
            // the openclaw/hermes/codex quick-connect buttons) - those
            // win outright, matching the pre-wrappers precedence for
            // agent_type.
            if (agentType) {
                payload.agent_type = agentType;
            } else if (providerChoice.wrapperId) {
                payload.agent_type = providerChoice.wrapperId;
            } else if (providerChoice.agentType) {
                // A PINNED FAMILY ROW (codex / hermes / openclaw). The
                // family name IS the agent_type, exactly as the reserved
                // quick-connect buttons above have always posted it, so
                // the server resolves it through the same path. Ranked
                // below wrapperId only because the two are mutually
                // exclusive by construction - the picker returns one or
                // the other, never both.
                payload.agent_type = providerChoice.agentType;
            }
            // Omit for claude (server default); set for an OpenRouter model.
            if (providerChoice.model) {
                payload.model = providerChoice.model;
            }
            const session = await window.API.createSession(payload);

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
            // "create a new project" - carry it out immediately. The prior
            // tmux session is detached (not killed) and stays available in
            // the running-sessions list / banner for rejoin.
            if (error.message.includes('already running')) {
                this.detachAndCreateNew(agentType);
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
     * Show the "clone from github" modal - collects URL + parent dir +
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
        // Gate: pick claude vs an OpenRouter model BEFORE showing the clone
        // form. The backend's POST /projects/clone both clones the repo to
        // disk AND persists the project entry in one shot, so that request
        // must never fire before the user has committed to launching -
        // null = cancelled, back to the launchpad, nothing touched.
        const providerChoice = await this.showProviderModal();
        if (!providerChoice) {
            console.log('Launchpad: Provider selection cancelled');
            return;
        }

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
                            paste the full url or use gh shorthand (owner/repo). server runs <code>gh repo clone</code> - gh must be authenticated.
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
                // Success - refresh project list, close modal, open session
                // in the cloned dir. selectProject does the heavy lifting.
                await this.loadProjects();
                closeModal();
                // Provider already chosen above - pass it through so
                // selectProject doesn't prompt a second time.
                await this.selectProject({
                    name: project.name,
                    path: project.path,
                    description: project.description || null,
                }, providerChoice);
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
     * path - prior session lingers and can be re-adopted later.
     */
    async detachAndCreateNew(agentType = null) {
        try {
            this.updateStatus('detaching from current session...');
            await window.API.detachSession();

            // Wait a moment, then create new. Same race-avoidance rationale
            // as ``detachAndOpenProject``. Honor the agentType so the
            // re-create lands on the same CLI the user originally picked.
            setTimeout(() => {
                if (agentType) {
                    this.createNewSessionWithAgent(agentType);
                } else {
                    this.createNewSession();
                }
            }, 500);
        } catch (error) {
            console.error('Launchpad: Failed to detach session:', error);
            this.showError('failed to detach session: ' + error.message);
        }
    }

    /**
     * Canonical tmux-name <-> URL-slug matcher - the ONE place that decides
     * whether a decoded deep-link slug refers to a given running session.
     *
     * Description: used by BOTH directions of the deep-link feature so
     *   they can never drift apart:
     *     - OUTBOUND (build): `App._syncSessionUrl()` in app.js calls
     *       `_deriveRunningSessionDisplayName()` directly to turn a live
     *       `tmux_session` (e.g. `cloude_claude-config-sync-2`) into the
     *       URL slug (`claude-config-sync-2`).
     *     - INBOUND (resolve): this method calls the SAME
     *       `_deriveRunningSessionDisplayName()` on every candidate row
     *       and compares against the decoded slug - exact match first,
     *       then case-insensitive fallback.
     *   Previously these two directions used the same helper already, so
     *   a prefix-stripping mismatch was ruled out as the root cause of
     *   the duplicate-session regression (see openProjectByName()'s
     *   docstring) - but keeping the comparison here, in one function,
     *   means that stays true by construction instead of by coincidence.
     * Inputs:
     *   slug (string) - decoded, regex-validated name from the URL.
     * Output: the matching row from `this.runningSessions`, or
     *   `undefined` if none matches.
     */
    _findRunningSessionBySlug(slug) {
        const rows = this.runningSessions || [];
        return rows.find(s => this._deriveRunningSessionDisplayName(s.name) === slug)
            || rows.find(s => (this._deriveRunningSessionDisplayName(s.name) || '').toLowerCase() === String(slug).toLowerCase());
    }

    /**
     * Open a project OR an adopted session by name (used by the deep-link
     * router, Item 9; extended for adopted sessions as part of the deep-link
     * fix; REORDERED as part of the duplicate-session regression fix below).
     *
     * ROOT CAUSE of the duplicate-session regression: this method used to
     * check `this.projects` (launcher entries) FIRST and, on a match, call
     * `selectProject()` - which unconditionally calls
     * `window.API.createSession()`. `create_session()` server-side
     * (src/core/session_manager.py) deliberately NEVER attaches to an
     * existing tmux session for a project click - "a project click must
     * ALWAYS spawn a NEW session... the user runs multiple concurrent
     * sessions per directory" - so on a name collision it silently mints
     * `<name>-2`, `<name>-3`, etc. and returns THAT. A deep link to a
     * project that already had a live tmux session therefore always
     * created a fresh duplicate rather than reattaching, and the browser
     * ended up on the newly-created session's URL. The name<->slug
     * mapping itself (`_deriveRunningSessionDisplayName`, see
     * `_findRunningSessionBySlug()` above) was already shared correctly
     * between build and resolve - it was never reached, because the
     * launcher-project branch returned first.
     *
     * FIX: live sessions are now resolved FIRST, and a launcher-project
     * match is no longer used to justify creating a session for a deep
     * link at all - see the GUARD note below.
     *
     * Resolution order:
     *   1. `GET /sessions/list` / `GET /sessions/attachable` (via
     *      `loadRunningSessions()`) for a LIVE session whose slug
     *      matches (`_findRunningSessionBySlug()`) - this covers both an
     *      already-adopted session (jump straight to its terminal) and
     *      an un-adopted but running tmux session (adopt it via the same
     *      path as a running-sessions row click). NEITHER branch creates
     *      anything.
     *   2. Nothing matches anywhere → the router's error banner (NOT a
     *      browser `alert()`, which is easy to miss/dismiss unnoticed) via
     *      `Router.rejectTarget()`, which also cleans the URL back to `/`.
     *
     * GUARD: this method deliberately does NOT fall back to
     * `this.projects` / `selectProject()` on a miss, even though a
     * launcher project with that name may exist - doing so would call
     * `createSession()`, which is exactly the regression above. Deep-link
     * resolution must never create a session; a launcher-project name
     * match with no corresponding live tmux session is indistinguishable
     * from "nothing to reattach to" and is reported the same way. As a
     * second line of defense, `selectProject()` itself refuses to run
     * while `this._resolvingDeepLink` is set (see its guard clause), so
     * even a future refactor that re-wires this method into
     * `selectProject()` fails loudly instead of silently regressing.
     * Inputs: name (string) - decoded, regex-validated slug from the URL.
     * Output: Promise<void>.
     */
    async openProjectByName(name) {
        console.log('Launchpad: openProjectByName:', name);

        this._resolvingDeepLink = true;
        try {
            // Refresh the running-sessions list so we aren't racing the 5s
            // poller - a deep link can arrive well before the first poll
            // tick, and can also be a session with no launcher project
            // entry at all (external/adopted).
            // A LISTING THAT DID NOT RUN IS NOT AN EMPTY LISTING, and
            // treating it as one is what made deep links unreliable.
            //
            // On a COLD page load this resolve can fire before the session
            // list is fetchable - auth has just settled, the first poll
            // has not happened. loadRunningSessions() records that as
            // `runningSessionsListing.ok === false`; it does NOT throw. So
            // the old single attempt proceeded with an empty row set,
            // found nothing, and rejected the URL.
            //
            // The symptom was baffling because ONE class of name survived:
            // a slug that is also a launcher PROJECT fell through to the
            // project branch below and opened anyway. So /session/Foo
            // worked when Foo was a project, and every other session -
            // Foo-2, Foo-3, any fork, anything renamed - bounced to `/`
            // with "No active session", which reads like a broken session
            // rather than a lookup that never got to look.
            //
            // Retry only while we CANNOT DETERMINE. A listing that ran and
            // genuinely returned nothing is an answer, and we take it.
            let session = null;
            for (let attempt = 0; attempt < 5; attempt += 1) {
                try {
                    await this.loadRunningSessions();
                } catch (err) {
                    console.warn('Launchpad: loadRunningSessions during deep-link resolve failed:', err);
                }
                session = this._findRunningSessionBySlug(name);
                if (session) break;
                const listing = this.runningSessionsListing || { ok: true };
                if (listing.ok) break;   // it looked, and there is nothing
                console.warn(
                    'Launchpad: session listing unavailable during deep-link resolve '
                    + `(attempt ${attempt + 1}), reason=${listing.reason || 'unknown'}`
                );
                await new Promise(r => setTimeout(r, 300));
            }

            if (session) {
                console.log('Launchpad: deep-link resolved to running session:', session.name);
                if (session.is_active) {
                    await this._returnToActiveRunningSession(session.session_id || null);
                } else {
                    await this._handleAttachRunningSession(session.name);
                }
                return;
            }

            // No live session anywhere - GUARD (see docstring above): do
            // NOT consult this.projects to create one. Show the actual
            // banner, not a silent bounce to `/` and not a browser
            // alert() (Task 5 / deep-link fix).
            console.warn('Launchpad: deep-link target not found among live sessions:', name);
            if (window.Router && typeof window.Router.rejectTarget === 'function') {
                window.Router.rejectTarget(name);
            } else {
                this.showError(`session not found: ${name}`);
            }
        } finally {
            this._resolvingDeepLink = false;
        }
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

            // Gate: pick claude vs an OpenRouter model BEFORE persisting
            // anything. null = user cancelled the whole launch - abort
            // cleanly with no project entry written to config.json (avoids
            // orphaning a project row for a session that was never created).
            const providerChoice = await this.showProviderModal();
            if (!providerChoice) {
                console.log('Launchpad: Provider selection cancelled');
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

            // Open the project (provider already chosen above - pass it
            // through so selectProject doesn't prompt a second time).
            await this.selectProject({
                name: savedName,
                path: selectedPath,
                description: details.description || null,
            }, providerChoice);
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
     *
     * The modal itself lives in client/js/folder-picker-modal.js; this is
     * the delegate. It reads no controller state beyond the escaper it
     * passes in, which is why it was the unit that could come out.
     */
    showFolderPickerModal() {
        return window.FolderPickerModal.open({
            escapeHtml: (s) => this._escapeHtml(s)
        });
    }

    /**
     * Select and open existing project.
     * @param {object} project
     * @param {{model: string|null}|undefined} [providerChoice] - Pass a
     *   already-resolved choice when the caller gated its own pre-session
     *   side effect (e.g. persisting a new project entry) on the provider
     *   modal first - avoids prompting the user twice. Omit to have this
     *   function show the modal itself (existing-project paths).
     */
    async selectProject(project, providerChoice = undefined) {
        console.log('Launchpad: Selecting project:', project.name);

        // GUARD: never create a session while resolving a deep link (see
        // the `_resolvingDeepLink` docstring in the constructor and
        // `openProjectByName()`'s docstring). This is what makes the
        // "deep-link resolution must never create" invariant explicit in
        // code rather than an accident of call order - openProjectByName()
        // no longer calls this method at all, but this clause exists so a
        // future refactor that re-wires them together fails loudly (a
        // thrown error surfaced to the deep-link error banner) instead of
        // silently spawning a duplicate tmux session again.
        if (this._resolvingDeepLink) {
            const err = new Error(
                `refusing to create a session for ${project.name} while resolving a deep link`
            );
            console.error('Launchpad: BLOCKED create-session during deep-link resolution:', err);
            if (window.Router && typeof window.Router.rejectTarget === 'function') {
                window.Router.rejectTarget(project.name);
            }
            throw err;
        }

        try {
            // Gate: pick claude vs an OpenRouter model BEFORE opening the
            // project. null = user cancelled the whole launch - abort
            // cleanly, no session created, no error toast.
            if (providerChoice === undefined) {
                providerChoice = await this.showProviderModal();
                if (!providerChoice) {
                    console.log('Launchpad: Provider selection cancelled');
                    return;
                }
            }

            // Show loading state
            this.updateStatus(`opening ${project.name}...`);

            // Create session with project path (no template copying for existing projects).
            // Include current xterm cell grid dims so the tmux pane is birthed
            // at the right size - see the "new project" path for rationale.
            const _dims = this._getTerminalDims();
            const payload = {
                working_dir: project.path,
                auto_start_claude: true,
                copy_templates: false,
                project_name: project.name,
                ..._dims
            };
            // Omit for claude (server default); set for an OpenRouter model.
            if (providerChoice.model) {
                payload.model = providerChoice.model;
            }
            // feat/launch-wrappers - see _createNewSessionInner's identical
            // comment; this path has no explicit agentType to defer to.
            if (providerChoice.wrapperId) {
                payload.agent_type = providerChoice.wrapperId;
            } else if (providerChoice.agentType) {
                // Pinned family row; see _createNewSessionInner's comment.
                payload.agent_type = providerChoice.agentType;
            }
            const session = await window.API.createSession(payload);

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
            // create-session call lands - avoids a race where we try to
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
            // aria-label mirrors the ::after tooltip text so screen readers
            // get the same live state a sighted hover shows.
            statusEl.setAttribute('aria-label', message);
        }
        console.log('Launchpad:', message);
    }

    /**
     * Show error message
     */
    /**
     * Say out loud why a project row refused to open, and name the path.
     *
     * THREE OUTCOMES, kept distinct on purpose. 'missing' is a measured
     * fact - the folder is not there. 'unreachable' is the third state:
     * the presence probe could not reach the path, which is NOT evidence
     * the project is gone, and telling the user it is missing would invent
     * a verdict nobody measured. Anything else reaching here means the row
     * was disabled for a reason this function does not know about, and it
     * says exactly that rather than guessing.
     *
     * @param {object|undefined} project - The project the row stands for.
     * @param {HTMLElement} item - The row element, used only as a fallback
     *   source for the path when the project object is unavailable.
     * @returns {void}
     */
    _explainRefusedProject(project, item) {
        const path = (project && (project.root || project.path))
            || (item && item.dataset ? item.dataset.path : '')
            || 'an unrecorded path';
        const row = (project && project.root && this.projectPresence.get(project.root))
            || null;
        const state = row ? row.presence : 'unchecked';

        if (state === 'missing') {
            this.showError(
                `"${project && project.name ? project.name : 'this project'}" ` +
                `was not opened: its folder does not exist at ${path}.\n\n` +
                `Nothing was started and nothing was changed. Either restore ` +
                `the folder at that path, edit the project to point at where ` +
                `it lives now, or delete the project.`
            );
            return;
        }
        if (state === 'unreachable') {
            const detail = (row && row.presence_detail) || 'reason unknown';
            this.showError(
                `"${project && project.name ? project.name : 'this project'}" ` +
                `was not opened: CANNOT DETERMINE whether ${path} exists ` +
                `(${detail}).\n\n` +
                `This is NOT a report that the folder is gone - the check ` +
                `could not run. Nothing was started and nothing was changed.`
            );
            return;
        }
        this.showError(
            `"${project && project.name ? project.name : 'this project'}" ` +
            `was not opened, and the reason was not recorded (presence ` +
            `state "${state}" for ${path}).\n\n` +
            `Nothing was started and nothing was changed. This is a bug in ` +
            `the app, not something you did.`
        );
    }

    showError(message) {
        /**
         * Surface an error WITHOUT blocking the page.
         *
         * Description: this used a native ``alert()``, and three other
         *   places in this file already documented that errors should be
         *   inline "(no alert())" while the alert stayed put. It is not a
         *   style problem. A native alert HALTS the page: timers stop, the
         *   poller stops, and nothing else can run until a human dismisses
         *   it. The fork refusal found that the hard way - the server
         *   answered 409 correctly with a clear reason, the client picked
         *   the right branch, and the whole app then froze on the modal
         *   that was supposed to be telling you about it. An error message
         *   that stops the program is worse than the error.
         *
         *   Deliberately NOT routed through ToastManager: that surface is
         *   for server-pushed session toasts with ack semantics, and a
         *   dismiss there syncs to the server. A local client-side error
         *   has nothing to acknowledge and no server record to update.
         * Inputs: message (string).
         * Output: void.
         * Example: lp.showError('cannot fork: no conversation to branch');
         */
        console.error('Launchpad Error:', message);
        try {
            let host = document.getElementById('launchpad-error-stack');
            if (!host) {
                host = document.createElement('div');
                host.id = 'launchpad-error-stack';
                document.body.appendChild(host);
            }
            const card = document.createElement('div');
            card.className = 'launchpad-error-card';
            card.setAttribute('role', 'alert');
            const text = document.createElement('span');
            text.className = 'launchpad-error-text';
            // textContent, never innerHTML: this string can carry a server
            // detail, a filesystem path or an exception message, none of
            // which is ours to trust as markup.
            text.textContent = message;
            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'launchpad-error-dismiss';
            close.setAttribute('aria-label', 'dismiss this error');
            close.textContent = '\u00d7';
            const remove = () => { if (card.parentNode) card.parentNode.removeChild(card); };
            close.addEventListener('click', remove);
            card.appendChild(text);
            card.appendChild(close);
            host.appendChild(card);
            // Auto-dismiss, but generously: an error you cannot re-read is
            // an error you cannot act on.
            setTimeout(remove, 12000);
        } catch (err) {
            // The reporter must never become the fault. If the DOM is not
            // there to write into, the console line above already ran.
            console.error('Launchpad: could not render error banner:', err);
        }
    }
}

// Export singleton instance
window.Launchpad = new Launchpad();
console.log('[Launchpad Module] Exported as window.Launchpad:', window.Launchpad);
