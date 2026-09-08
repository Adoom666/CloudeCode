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

        // THE ORDERING KEY. `GET /sessions/records` is the only surface
        // that carries `last_work_at`; the two probes above describe tmux
        // and a live backend, neither of which knows when the
        // conversation last did something. A failure here is ORDINARY and
        // is NOT folded into `listing`: the rows are still real and still
        // switchable, they simply fall back to being ordered by creation
        // with every row labelled unrecorded, which is honest.
        let workByName = new Map();
        try {
            if (typeof window.API.listSessionRecords === 'function') {
                workByName = workStampIndex(await window.API.listSessionRecords());
            }
        } catch (err) {
            console.warn('SessionSidebar: session records unavailable:', err && err.message);
        }

        for (const row of rows) {
            row.is_this_tab = !!activeTmuxName && row.name === activeTmuxName;
            // null, never 0 and never a placeholder date. An unrecorded
            // row is a THIRD OUTCOME the renderer labels, not a row that
            // was worked on at the epoch.
            row.last_work_at = workByName.get(row.name) || null;
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
            // punchlist 19 - overwritten UNCONDITIONALLY, never `||`'d
            // against the previous value. A session that has just
            // answered its trust prompt flips from
            // 'awaiting_startup_prompt' to 'ready', and a `||` would
            // keep the badge on screen after the thing it warns about is
            // over. A field the server did not send lands on undefined
            // and the renderer normalizes it to 'unknown', which paints
            // nothing - the right degradation for an older payload.
            existing.startup_gate = info.startup_gate;
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
            startup_gate: info.startup_gate,
        });
    }

    /**
     * Description: tmux name -> newest `last_work_at` across the stored
     *   rows carrying that name. The MAXIMUM, not the last one scanned: a
     *   name is reusable, so a dead instance can hold an older row under
     *   it, and the newest stamp is the live instance's by construction
     *   (a dead session cannot have worked more recently than the one
     *   that replaced it).
     *
     *   A row with no stamp contributes NOTHING rather than a zero, so an
     *   absent name reads as "no work recorded" and not as "worked on in
     *   1970".
     * Inputs: rows (Array<object>) - GET /sessions/records payload.
     * Output: Map<string, string>.
     */
    function workStampIndex(rows) {
        const index = new Map();
        for (const row of (Array.isArray(rows) ? rows : [])) {
            if (!row || !row.tmux_name || !row.last_work_at) continue;
            const seen = index.get(row.tmux_name);
            if (!seen || row.last_work_at > seen) index.set(row.tmux_name, row.last_work_at);
        }
        return index;
    }

    /**
     * Description: the sidebar's built-in order - MOST RECENTLY WORKED IN
     *   FIRST. It is the fallback for sessions the user has never
     *   arranged, and ONLY that: once an arrangement exists,
     *   client/js/session-sidebar-arrangement.js overrides this for every
     *   name it knows, because a user-defined order that a poll tick can
     *   undo is not an order.
     *
     *   THE ORDER USED TO LEAD WITH `is_this_tab`, THEN `is_active`, AND
     *   THAT WAS THE BUG. Both are facts about what the user is LOOKING
     *   AT, so opening a session hoisted it to the top of the OTHER band.
     *   This list is read as a timeline - he scans down it to recall what
     *   is in flight - and a timeline that reshuffles when you read it
     *   cannot be read. Neither term survives; nothing a click can change
     *   participates in this comparison any more.
     *
     *   `last_work_at` is stamped ONLY from Claude Code hook events that
     *   mean the conversation did something (see
     *   src/core/session_work_stamp.py and claude_hooks.WORK_EVENTS).
     *   Attaching, selecting, deep-linking and a server restart's rebind
     *   all leave it alone.
     *
     *   THREE OUTCOMES. A row with no stamp has NOT been measured
     *   working - true of every session predating this feature and of one
     *   started seconds ago that has not run a turn. It is not treated as
     *   work at the epoch and not as work now: those rows sort BELOW every
     *   measured row, hold their own newest-created-first order among
     *   themselves, and are labelled by the renderer rather than blended
     *   into the tail of the measured ones.
     * Inputs: rows (Array<object>). Output: Array<object> - same array,
     *   sorted in place.
     */
    function defaultSort(rows) {
        return rows.sort((a, b) => {
            const aw = a && a.last_work_at;
            const bw = b && b.last_work_at;
            if (!!aw !== !!bw) return aw ? -1 : 1;
            if (aw && bw && aw !== bw) return aw < bw ? 1 : -1;
            return (b.created_at_epoch || 0) - (a.created_at_epoch || 0);
        });
    }

    /**
     * Description: the row attributes that make an UNRECORDED session
     *   visibly distinct from a merely stale one.
     *
     *   `defaultSort` puts a row with no `last_work_at` at the bottom,
     *   which is exactly where a human reads "this is the stalest thing I
     *   own" - a statement nobody measured. `data-work` carries the
     *   distinction for the stylesheet (which dims the name, see
     *   client/css/session-sidebar.css) and for a test; the title carries
     *   it in words for a person.
     *
     *   IT LIVES HERE, NOT IN session-sidebar-rows.js, AND THE REASON IS
     *   MECHANICAL: that file sits exactly on this repo's 500-line
     *   ceiling, which its own test enforces. This module already owns
     *   the `last_work_at` concept - it fetches the stamps, indexes them
     *   and sorts by them - so it is the right home anyway, and the row
     *   builder reaches it the same guarded way it reaches
     *   SessionRowActions and SessionStatusUI.
     * Inputs: row (object) - one merged sidebar row.
     * Output: string - HTML attributes, trailing space included.
     */
    function workAttr(row) {
        if (row && row.last_work_at) {
            // Whitelisted rather than escaped. The value is an ISO-8601
            // stamp this app wrote, so anything outside that alphabet is
            // not a value to render safely, it is a value that should not
            // be here - and dropping the stray characters is a smaller
            // dependency than pulling a DOM escaper into a module that
            // otherwise touches no DOM.
            const safe = String(row.last_work_at).replace(/[^0-9TZ:.+-]/g, '');
            return `data-work="recorded" data-work-at="${safe}" `;
        }
        return 'data-work="unrecorded" title="no work recorded yet" ';
    }

    window.SessionSidebarFetch = {
        load,
        mergeLiveRow,
        defaultSort,
        statusOf,
        workStampIndex,
        workAttr,
    };
    console.log('[SessionSidebarFetch Module] Exported as window.SessionSidebarFetch');
})();
