/**
 * Session sidebar FETCH - acquiring the row list, and the verdict about
 * whether it could be acquired at all.
 *
 * Split out of client/js/session-sidebar.js so that file stays under the
 * project's 500-line ceiling once pinning, ordering and density land in
 * it, and along an honest seam: this module knows about endpoints and
 * merging, the controller knows about the panel.
 *
 * TWO ENDPOINTS, ONE LIST. `GET /sessions/attachable` returns every tmux
 * session that can be joined (detached, or started outside this app);
 * `GET /sessions/list` returns the ones with a live backend, which is
 * where `session_id`, the live activity status and the unread flag come
 * from. Same merge the home screen does, trimmed to what the sidebar
 * renders.
 *
 * THE TWO FAILURES ARE NOT THE SAME FAILURE, and this is the bug that was
 * here before:
 *   attachable rejects  -> we do not know what exists. That is a verdict
 *                          of UNAVAILABLE and the caller must render
 *                          CANNOT DETERMINE, never an empty list.
 *   list rejects        -> we know what exists, we just do not know which
 *                          of them this browser has a backend for. The
 *                          rows are still real and still switchable, so
 *                          this is `ok` with less decoration on each row.
 * Folding the second into the first would page the user over nothing;
 * folding the first into "ok" is the false green.
 *
 * Must load AFTER api.js and session-listing-state.js, and BEFORE
 * session-sidebar.js runs.
 */

console.log('[SessionSidebarFetch Module] Loading...');

(function () {
    /**
     * Description: HTTP status carried on an api.js rejection, or 0 when
     *   the request never reached a server at all.
     * Inputs: err (Error). Output: number.
     */
    function statusOf(err) {
        return (err && typeof err.status === 'number') ? err.status : 0;
    }

    /**
     * Description: fetch and merge the sidebar's row list, returning the
     *   rows alongside an explicit verdict about whether the underlying
     *   probe answered.
     * Inputs: activeTmuxName (string|null) - the tmux session this browser
     *   tab is attached to, so the merged rows can be marked.
     * Output: Promise<object> - {rows (Array<object>), listing (object)}
     *   where listing is {ok: true} or {ok: false, reason, detail}.
     * Example: (await SessionSidebarFetch.load(null)).listing.ok // true
     */
    async function load(activeTmuxName) {
        let rows = [];
        let listing = { ok: true, reason: null, detail: null };

        try {
            const attachable = await window.API.listAttachableSessions();
            rows = Array.isArray(attachable) ? attachable.slice() : [];
        } catch (err) {
            console.error('SessionSidebar: listAttachableSessions failed:', err);
            listing = window.SessionListingState
                ? window.SessionListingState.fromError(err, statusOf(err))
                : { ok: false, reason: 'probe_error', detail: 'the server could not be reached' };
            rows = [];
        }

        try {
            const live = typeof window.API.listSessions === 'function'
                ? await window.API.listSessions()
                : [];
            for (const info of (Array.isArray(live) ? live : [])) {
                mergeLiveRow(rows, info);
            }
            // A live backend this tab can see is proof the session exists,
            // so it repairs the verdict for exactly the rows it names -
            // but it cannot vouch for sessions it never listed, so a
            // failed attachable probe stays unavailable when it yields
            // nothing at all.
            if (!listing.ok && rows.length > 0) {
                listing = {
                    ok: false,
                    reason: listing.reason,
                    detail: `${listing.detail}; only sessions with a live backend are listed`,
                };
            }
        } catch (err) {
            // No live backend for this tab is an ordinary state, not a
            // probe failure: the attachable rows above are still the
            // truth about what exists.
            console.warn('SessionSidebar: listSessions unavailable:', err && err.message);
        }

        for (const row of rows) {
            row.is_this_tab = !!activeTmuxName && row.name === activeTmuxName;
        }
        return { rows: defaultSort(rows), listing };
    }

    /**
     * Description: fold one live SessionInfo into the attachable row list,
     *   updating a matching row or prepending a new one.
     *
     *   `created_by_cloude` is taken VERBATIM from the server, which
     *   resolves it from the persisted `owned_tmux_sessions` set. It means
     *   "did this app CREATE this tmux session", which is origin, not
     *   current state - so it must not flip on open/close and must survive
     *   a server restart. Never derive it here; see the same merge in
     *   client/js/launchpad.js for the two local derivations that were
     *   both wrong.
     * Inputs: rows (Array<object>) - mutated in place. info (object).
     * Output: void.
     */
    function mergeLiveRow(rows, info) {
        const tmuxName = info && info.tmux_session;
        if (!tmuxName) return;
        const sessionId = (info.session && info.session.id) || null;
        const status = info.activity_status || 'unknown';
        const unread = !!info.unread;
        const existing = rows.find((r) => r.name === tmuxName);
        if (existing) {
            existing.is_active = true;
            existing.session_id = sessionId;
            existing.status = status;
            existing.unread = unread;
            existing.created_by_cloude = !!info.created_by_cloude;
            // THE LIVE ROW IS THE FRESHER ANSWER ABOUT THE LABEL. It is
            // the payload a rename's own response and the session.renamed
            // repaint come back through, while the attachable probe may
            // still be serving the pre-rename value from this poll tick.
            // `!== undefined` rather than a truthiness test on purpose: a
            // label CLEARED back to null is a real state, and `||` would
            // silently keep showing the old one.
            if (info.label !== undefined) existing.label = info.label;
            if (info.agent_family !== undefined) existing.agent_family = info.agent_family;
            if (info.agent_family_source !== undefined) {
                existing.agent_family_source = info.agent_family_source;
            }
            if (info.pinned_theme) existing.pinned_theme = info.pinned_theme;
            return;
        }
        rows.unshift({
            name: tmuxName,
            // A row the probe never listed still has a name a human gave
            // it. Dropping the label here would make this one row render
            // its tmux handle while every other row rendered its label.
            label: info.label !== undefined ? info.label : null,
            created_by_cloude: !!info.created_by_cloude,
            created_at_epoch: 0,
            is_active: true,
            session_id: sessionId,
            status,
            unread,
            agent_family: info.agent_family !== undefined ? info.agent_family : null,
            agent_family_source: info.agent_family_source !== undefined
                ? info.agent_family_source
                : null,
            pinned_theme: info.pinned_theme || null,
        });
    }

    /**
     * Description: the sidebar's built-in order - this tab, then live,
     *   then newest. It is the fallback for sessions the user has never
     *   arranged, and ONLY that: once an arrangement exists,
     *   client/js/session-sidebar-arrangement.js overrides this for every
     *   name it knows, because a user-defined order that a poll tick can
     *   undo is not an order.
     * Inputs: rows (Array<object>). Output: Array<object> - same array,
     *   sorted in place.
     */
    function defaultSort(rows) {
        return rows.sort((a, b) => {
            if (!!a.is_this_tab !== !!b.is_this_tab) return a.is_this_tab ? -1 : 1;
            if (!!a.is_active !== !!b.is_active) return a.is_active ? -1 : 1;
            return (b.created_at_epoch || 0) - (a.created_at_epoch || 0);
        });
    }

    window.SessionSidebarFetch = { load, mergeLiveRow, defaultSort, statusOf };
    console.log('[SessionSidebarFetch Module] Exported as window.SessionSidebarFetch');
})();
