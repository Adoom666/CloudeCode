/**
 * Session Sidebar Module - the user's working set of SESSIONS, available
 * from every screen.
 *
 * Mounted from a hamburger button at the top-left of the header. It used
 * to appear only on the terminal screen; it is now available on the HOME
 * screen too, and can be pinned there, because switching between the
 * conversations you are working on is not a thing you only want to do
 * once you are already inside one of them. The home screen keeps its own
 * project-to-session tree - this bar is not a project browser and does
 * not try to be one.
 *
 * THE FOUR SIBLING MODULES, and why this file is not all of them:
 *   session-sidebar-fetch.js        two endpoints -> one row list, plus
 *                                   the verdict about whether the probe
 *                                   answered at all
 *   session-sidebar-arrangement.js  which sessions are pinned, and the
 *                                   order the user put them in
 *   session-sidebar-reorder.js      the keyboard and pointer interactions
 *                                   that change that arrangement
 *   session-sidebar-density.js      how much each row says
 *   session-sidebar-rows.js         the row markup
 *   session-sidebar-pin.js          pinning the BAR docked open
 * This file owns the panel: open, close, poll, paint, and route a click.
 *
 * TWO DIFFERENT PINS LIVE HERE. Pinning the BAR (docked, no backdrop) is
 * session-sidebar-pin.js. Pinning a SESSION (sorted to the top band) is
 * session-sidebar-arrangement.js. The user asked for both, in separate
 * sentences, so neither name was free to mean the other.
 */

console.log('[SessionSidebar Module] Loading...');

class SessionSidebarController {
    constructor() {
        this.toggleBtn = null;
        this.panel = null;
        this.backdrop = null;
        this.closeBtn = null;
        this.listEl = null;

        this.isOpen = false;
        this._wired = false;
        this._pollInterval = null;
        this._lastSig = null;

        // Last painted rows + verdict, so a repaint triggered by a local
        // change (pin, move, density) does not have to wait for a fetch.
        this._rows = [];
        this._listing = { ok: true, reason: null, detail: null };
        this._missing = [];

        // True only while a pointer drag is in flight. Drawn state, not
        // bookkeeping - see setDragging().
        this._dragging = false;

        this._activeSessionId = null;
        this._activeTmuxName = null;
    }

    /**
     * Storage key for open/closed persistence - follows the app's existing
     * localStorage convention (cloude.theme, cloude.audio.volume).
     * @type {string}
     */
    static get STORAGE_KEY() { return 'cloude.session.sidebar'; }

    /**
     * Poll cadence while the panel is open. Matches the launchpad's
     * Running Sessions poller (5s) so status freshness is consistent
     * across both surfaces.
     * @type {number}
     */
    static get POLL_MS() { return 5000; }

    /**
     * Description: wire DOM elements + event listeners once, and load the
     *   persisted arrangement and density. Idempotent.
     * Inputs: none.
     * Output: void.
     */
    init() {
        if (this._wired) return;
        this.toggleBtn = document.getElementById('session-sidebar-toggle');
        this.panel = document.getElementById('session-sidebar-panel');
        this.backdrop = document.getElementById('session-sidebar-backdrop');
        this.closeBtn = document.getElementById('session-sidebar-close');
        this.listEl = document.getElementById('session-sidebar-list');

        if (!this.toggleBtn || !this.panel || !this.backdrop || !this.listEl) {
            console.warn('SessionSidebar: required markup missing, skipping wire');
            return;
        }

        this.toggleBtn.addEventListener('click', () => this.toggle());
        if (this.closeBtn) {
            this.closeBtn.addEventListener('click', () => this.close());
        }
        this.backdrop.addEventListener('click', () => this.close());
        document.addEventListener('keydown', (e) => {
            // A PINNED bar is part of the layout, not something overlaid
            // that Escape should sweep away - see session-sidebar-pin.js.
            if (e.key !== 'Escape' || !this.isOpen) return;
            if (window.SessionSidebarPin && window.SessionSidebarPin.isEffectivelyPinned()) return;
            this.close();
        });
        this.listEl.addEventListener('click', (e) => this._onRowClick(e));
        this.listEl.addEventListener('dblclick', (e) => {
            if (window.SessionSidebarRename) window.SessionSidebarRename.onDblClick(e);
        });
        // Keyboard activation (Enter/Space) for the mark-unread toggle -
        // it's a `role="button"` span, not a real <button>, so it needs
        // explicit key handling to be operable without a mouse.
        this.listEl.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const toggleEl = e.target.closest('[data-mark-unread]');
            if (!toggleEl) return;
            e.preventDefault();
            e.stopPropagation();
            this._onMarkUnreadClick(toggleEl);
        });

        this._wired = true;
        if (window.SessionSidebarArrangement) window.SessionSidebarArrangement.load();
        if (window.SessionSidebarPin) window.SessionSidebarPin.init();
        if (window.SessionSidebarDensity) window.SessionSidebarDensity.init();
        if (window.SessionSidebarReorder) window.SessionSidebarReorder.init();
        if (window.SessionSidebarRename) window.SessionSidebarRename.init();
        if (window.SessionSidebarGroupActions) {
            window.SessionSidebarGroupActions.init();
            // FIRST GROUP READ. Until it lands the store reports
            // 'unknown' and the list renders exactly as it did before
            // groups existed - which is the correct thing to draw while
            // the answer is genuinely not known yet.
            window.SessionSidebarGroupActions.refresh();
        }
        console.log('SessionSidebar: wired');
    }

    /**
     * Description: reveal the hamburger toggle and re-open the panel if it
     *   was open on a prior visit. Called on entering the terminal screen
     *   AND the home screen - the bar is available on both, which is why
     *   this takes no screen argument: there is nothing screen-specific
     *   left in it.
     * Inputs: none.
     * Output: void.
     */
    show() {
        this.init();
        if (!this.toggleBtn) return;
        this.toggleBtn.classList.remove('hidden');
        let stored = null;
        try { stored = localStorage.getItem(SessionSidebarController.STORAGE_KEY); } catch (_) { /* ignore */ }
        if (stored === '1' && !this.isOpen) {
            this.open();
        } else if (window.SessionSidebarPin) {
            window.SessionSidebarPin.apply();
        }
    }

    /**
     * Description: hide the hamburger toggle and close the panel, WITHOUT
     *   clearing the persisted open/closed preference. That distinction is
     *   the whole reason `close()` takes an argument: leaving a screen is
     *   not the user closing the bar, and treating it as one meant a
     *   pinned-open bar came back closed after one round trip through the
     *   home screen.
     * Inputs: none.
     * Output: void.
     */
    hide() {
        this.close({ persist: false });
        if (this.toggleBtn) this.toggleBtn.classList.add('hidden');
    }

    /**
     * Description: record which session is currently attached so the row
     *   list can mark it active and the click handler can no-op on a
     *   self-click.
     * Inputs: sessionId (string|null), tmuxName (string|null).
     * Output: void.
     */
    setActiveSession(sessionId, tmuxName) {
        this._activeSessionId = sessionId || null;
        this._activeTmuxName = tmuxName || null;
        if (this.isOpen) this._fetchAndRender();
    }

    /** Description: toggle open/closed. Inputs: none. Output: void. */
    toggle() {
        if (this.isOpen) this.close(); else this.open();
    }

    /**
     * Description: open the panel, start the poller, persist the open
     *   state, and fetch immediately so the list is not stale for up to
     *   POLL_MS.
     * Inputs: none.
     * Output: void.
     */
    open() {
        if (!this.panel) return;
        this.isOpen = true;
        this.panel.classList.add('session-sidebar-panel--open');
        this.panel.setAttribute('aria-hidden', 'false');
        this.backdrop.hidden = false;
        this.toggleBtn.setAttribute('aria-expanded', 'true');
        try { localStorage.setItem(SessionSidebarController.STORAGE_KEY, '1'); } catch (_) { /* ignore */ }
        if (window.SessionSidebarDensity) window.SessionSidebarDensity.apply();
        if (window.SessionSidebarPin) window.SessionSidebarPin.apply();
        this._fetchAndRender();
        this._startPoll();
    }

    /**
     * Description: close the panel and stop polling.
     * Inputs: opts (object) - {persist (boolean)}, default true. Pass
     *   `{persist: false}` when the bar is being taken off screen for a
     *   reason that is not the user closing it.
     * Output: void.
     */
    close(opts) {
        if (!this.panel) return;
        const persist = !opts || opts.persist !== false;
        this.isOpen = false;
        this.panel.classList.remove('session-sidebar-panel--open');
        this.panel.setAttribute('aria-hidden', 'true');
        this.backdrop.hidden = true;
        if (this.toggleBtn) this.toggleBtn.setAttribute('aria-expanded', 'false');
        if (persist) {
            try { localStorage.setItem(SessionSidebarController.STORAGE_KEY, '0'); } catch (_) { /* ignore */ }
        }
        if (window.SessionSidebarPin) window.SessionSidebarPin.apply();
        this._stopPoll();
    }

    /**
     * Description: close the bar after a conversation switch, unless it is
     *   pinned open. Every switch path routes through this rather than
     *   calling close() directly, so "pinned means it stays put" is
     *   decided in exactly one place.
     * Inputs: none. Output: void.
     */
    _closeAfterSwitch() {
        if (window.SessionSidebarPin) window.SessionSidebarPin.closeAfterSwitch();
        else this.close();
    }

    /** Description: start the poll timer. Inputs: none. Output: void. */
    _startPoll() {
        if (this._pollInterval) return;
        this._pollInterval = setInterval(() => {
            this._fetchAndRender().catch((err) => {
                console.warn('SessionSidebar: poll tick failed:', err);
            });
        }, SessionSidebarController.POLL_MS);
    }

    /** Description: stop the poll timer. Inputs: none. Output: void. */
    _stopPoll() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }

    /**
     * Description: fetch the merged session list and repaint. A poll tick
     *   that lands mid-drag is dropped rather than applied: reordering the
     *   list out from under a finger that is holding a row is a data race
     *   the user can see.
     * Inputs: none.
     * Output: Promise<void>.
     */
    async _fetchAndRender() {
        if (window.SessionSidebarReorder && window.SessionSidebarReorder.isDragging()) return;
        const result = await window.SessionSidebarFetch.load(this._activeTmuxName);
        this._listing = result.listing;
        this._rows = result.rows;
        this.repaint();
    }

    /**
     * Description: apply the user's arrangement to the last fetched rows
     *   and paint. Separate from the fetch so a pin, a move or a density
     *   change repaints instantly instead of waiting for the next poll.
     *
     *   The signature diff that skips a no-op DOM rewrite now includes the
     *   arrangement and the density, because both are things the row
     *   SHOWS; leaving them out meant a pin the user just clicked did not
     *   paint until something unrelated happened to change.
     * Inputs: none.
     * Output: void.
     */
    repaint() {
        if (!this.listEl) return;
        const arrangement = window.SessionSidebarArrangement;
        const density = window.SessionSidebarDensity
            ? window.SessionSidebarDensity.currentMode()
            : 'cozy';
        let rows = this._rows;
        let missing = [];
        if (arrangement) {
            const arranged = arrangement.arrange(this._rows);
            rows = arranged.rows;
            missing = arranged.missing;
        }
        this._missing = missing;
        const state = arrangement ? arrangement.current() : null;
        // A DRAG IN FLIGHT CHANGES WHAT IS ON SCREEN, so it is part of
        // the paint, not a flag on the side: an empty pinned group is
        // drawn as a drop target only while a row is being dragged (see
        // client/js/session-sidebar-groups.js). It has to reach the
        // signature too, or the drop target does not appear until some
        // unrelated field happens to differ.
        const groups = {
            collapsed: (state && Array.isArray(state.collapsed)) ? state.collapsed : [],
            dragging: !!this._dragging,
        };
        // THE GROUP MODEL IS PART OF THE SIGNATURE, and it has to be.
        // This method short-circuits when the signature is unchanged, so
        // a group that was created, renamed, reordered or moved into
        // would repaint NOTHING - the rows are the same rows, in the same
        // order, with the same pins. The band each one lands in is the
        // thing that changed, and without this the user would drag a row
        // into a group and watch it not move.
        const G = window.SessionSidebarGroupStore;
        const groupSig = G
            ? `${G.current().status}|${G.bandOrder().join(',')}|`
                + rows.map((r) => `${r.name}:${r.band_key}`).join(',')
            : 'nogroups';
        const sig = window.SessionSidebarRows.signature(
            rows, density, this._listing, missing, groups,
        ) + (state ? `|${state.status}` : '') + `|${groupSig}`;
        if (sig === this._lastSig) return;
        this._lastSig = sig;
        this.listEl.setAttribute('data-listing-ok', this._listing.ok ? '1' : '0');
        this.listEl.setAttribute('data-order-missing', String(missing.length));
        this.listEl.setAttribute('data-arrangement-state', state ? state.status : 'default');
        this.listEl.setAttribute('data-density', density);
        // CANNOT DETERMINE, on the element, so a harness can measure it
        // and a user can be told. 'unknown' and 'unavailable' both render
        // an ungrouped list, so without this the two are indistinguishable
        // from the outside - which is the exact false green this project
        // keeps removing.
        this.listEl.setAttribute(
            'data-groups-state', G ? G.current().status : 'nogroups',
        );
        this.listEl.innerHTML = window.SessionSidebarRows.listHtml(
            rows, density, this._listing, missing, state, groups,
        );
        if (window.SessionSidebarReorder) window.SessionSidebarReorder.afterRender();
        if (window.SessionSidebarRename) window.SessionSidebarRename.afterRender();
    }

    /**
     * Description: turn the drag-in-flight flag on or off and repaint, so
     *   the empty pinned group appears as a drop target for exactly the
     *   duration of a drag. Called by
     *   client/js/session-sidebar-reorder.js, which owns the gesture but
     *   does not own the paint.
     * Inputs: on (boolean).
     * Output: void.
     */
    setDragging(on) {
        const next = !!on;
        if (next === !!this._dragging) return;
        this._dragging = next;
        this.repaint();
    }

    /**
     * Description: legacy entry point kept so any caller that hands rows
     *   in still works; stores them and repaints through the one path.
     * Inputs: rows (Array<object>). Output: void.
     */
    render(rows) {
        this._rows = Array.isArray(rows) ? rows : [];
        this.repaint();
    }

    /**
     * CLICK ROUTING LIVES IN client/js/session-sidebar-clicks.js. These
     * five methods are thin delegations to it, kept on the controller so
     * every existing caller, listener wiring and test keeps the same
     * entry point it always had. See that file's docblock for why the
     * split is free functions taking the controller rather than a mixin.
     */

    /**
     * Description: route a click inside the list.
     * Inputs: e (MouseEvent). Output: Promise<void>.
     */
    async _onRowClick(e) { await window.SessionSidebarClicks.onRowClick(this, e); }

    /**
     * Description: fold or unfold one section, persist it, and repaint.
     * Inputs: btnEl (Element). Output: void.
     */
    _onGroupToggleClick(btnEl) { window.SessionSidebarClicks.onGroupToggleClick(this, btnEl); }

    /**
     * Description: switch to the conversation a row names. Public because
     *   the keyboard path (Enter on a focused row) needs the same entry
     *   point a click takes.
     * Inputs: rowEl (Element). Output: Promise<void>.
     */
    async activateRow(rowEl) { await window.SessionSidebarClicks.activateRow(this, rowEl); }

    /**
     * Description: toggle the manual unread flag for one row.
     * Inputs: toggleEl (Element). Output: Promise<void>.
     */
    async _onMarkUnreadClick(toggleEl) {
        await window.SessionSidebarClicks.onMarkUnreadClick(this, toggleEl);
    }

    /**
     * Description: run a row's destructive action (close or remove).
     * Inputs: btnEl (Element). Output: Promise<void>.
     */
    async _onRowActionClick(btnEl) {
        await window.SessionSidebarClicks.onRowActionClick(this, btnEl);
    }
}

window.SessionSidebar = new SessionSidebarController();
console.log('[SessionSidebar Module] Exported as window.SessionSidebar');
