/**
 * Decide which stored RECENT rows may be painted, given the live sessions.
 *
 * WHY THIS IS ITS OWN MODULE. The rule used to be three inline lines in
 * `Launchpad.renderRecentSessions()`, and it was keyed on the tmux NAME:
 *
 *     const liveNames = new Set(runningSessions.map(s => s.name));
 *     rows = recentAll.filter(r => !liveNames.has(r.tmux_name));
 *
 * A tmux name is not an identity. This repo says so in two places already
 * (`client/js/api.js` on deleteSessionRecord: "Keyed on session_uuid,
 * never the tmux name: tmux reuses names, so two rows can differ only by
 * creation epoch", and CLAUDE.md's instance-triple section), and the
 * launcher's own recent list was the one surface still ignoring it.
 *
 * MEASURED ON THE OWNER'S BOX, 2026-09-08. Six session rows carry
 * `archived_at`. Three reach the wire (the other three are dropped
 * server-side as rows a running session replaced). Of those three, TWO
 * were then dropped here:
 *
 *     row  9  cloude_Mac         epoch 1788462035  ARCHIVED 2026-09-03
 *     row 11  cloude_Mac         epoch 1788463220  running
 *     row 10  cloude_Hirschfeld  epoch 1788462044  ARCHIVED 2026-09-03
 *     row 12  cloude_Hirschfeld  epoch 1788463221  running
 *
 * Rows 9 and 10 are not rows 11 and 12. They are older sessions that
 * happened to be given the same tmux name. The name key could not tell
 * them apart, so flipping "show deleted" added exactly ONE row of the six
 * the user had deleted, which reads on screen as a toggle that does
 * nothing.
 *
 * TWO RULES, IN THIS ORDER.
 *
 *   1. AN ARCHIVED ROW IS NEVER EXCLUDED BY THE RUNNING GUARD. The guard
 *      exists to stop a session appearing in RECENT while it is also on
 *      screen under RUNNING. A row the user deleted cannot be that row:
 *      the running list is built from live sessions, none of which is
 *      deleted. So the two facts cannot both hold, and excluding on this
 *      ground can only ever hide something unreachable.
 *
 *   2. A NON-ARCHIVED ROW IS EXCLUDED ONLY ON POSITIVE IDENTITY, i.e.
 *      when a live session's `session_row_id` IS this row's `id`. That
 *      still covers the case the guard was written for - a stopped row
 *      whose reaper has not run yet, whose tmux session is plainly in the
 *      listing - because that live session maps back to this very row.
 *
 * FAIL OPEN, DELIBERATELY, AND THE FALLBACK IS NAMED. The asymmetry is
 * already stated server-side in `list_recent_sessions`: "Showing a
 * duplicate is untidy; HIDING a session the user can no longer reach from
 * this screen is the failure a list filter must never produce." Keying on
 * identity is strictly more fail-open than keying on a name. But a payload
 * carrying NO `session_row_id` at all (an older server, a shape change) is
 * not evidence that nothing is duplicated - it is evidence we could not
 * look. In that one case the name key is used again for NON-archived rows,
 * so the duplicate the guard exists to prevent does not come back. Rule 1
 * holds in every case.
 */
(function () {
    'use strict';

    /**
     * Read a live session's stored row id, or null when it has none.
     *
     * Description: normalises the three ways "no id" arrives (absent,
     *   null, undefined) to one value, so a caller never has to repeat
     *   the check. `0` is not a valid sqlite rowid, so it is treated as
     *   absent too rather than as a falsy trap.
     * Inputs: live (object|null) - one AttachableSession from
     *   `GET /sessions/attachable`.
     * Output: number|null.
     * Example: rowIdOf({ session_row_id: 11 })  // 11
     */
    function rowIdOf(live) {
        if (!live) return null;
        const raw = live.session_row_id;
        if (raw === undefined || raw === null) return null;
        const n = Number(raw);
        return Number.isFinite(n) && n > 0 ? n : null;
    }

    /**
     * Is this stored row one the user deleted?
     *
     * Inputs: row (object|null) - one SessionRecord from the wire.
     * Output: boolean - true when `archived_at` carries a value.
     * Example: isArchived({ archived_at: '2026-09-03T16:30:54Z' })  // true
     */
    function isArchived(row) {
        return !!(row && row.archived_at);
    }

    /**
     * Filter stored RECENT rows down to the ones that may be painted.
     *
     * Description: applies the two rules in this module's header. Pure -
     *   it reads both lists and returns a new array, mutating neither.
     * Inputs: recentRows (Array<object>) - SessionRecord rows from
     *   `GET /sessions/recent`, archived ones included when the user
     *   asked for them. liveSessions (Array<object>) - AttachableSession
     *   rows from `GET /sessions/attachable`.
     * Output: Array<object> - the subset to render, original order kept.
     * Example:
     *   visibleRecentRows(
     *       [{ id: 9, tmux_name: 'cloude_Mac', archived_at: '...' }],
     *       [{ name: 'cloude_Mac', session_row_id: 11 }]
     *   )  // the archived row is KEPT: row 9 is not row 11
     */
    function visibleRecentRows(recentRows, liveSessions) {
        const rows = Array.isArray(recentRows) ? recentRows : [];
        const live = Array.isArray(liveSessions) ? liveSessions : [];
        if (!live.length) return rows.slice();

        const liveRowIds = new Set();
        for (const s of live) {
            const id = rowIdOf(s);
            if (id !== null) liveRowIds.add(id);
        }
        // NOT "some live session lacks an id" - EVERY one of them. A
        // partial payload still identifies the sessions it identified,
        // and demoting the whole list to a name match because one row was
        // incomplete would re-introduce exactly the collision above.
        const identityUnavailable = liveRowIds.size === 0;
        const liveNames = identityUnavailable
            ? new Set(live.map(s => s && s.name).filter(Boolean))
            : null;

        return rows.filter(row => {
            // Rule 1. See the header: a deleted row is never the row that
            // is currently on screen under RUNNING.
            if (isArchived(row)) return true;
            if (identityUnavailable) {
                // Rule 2, degraded. Named, not silent - see FAIL OPEN.
                return !(row && row.tmux_name && liveNames.has(row.tmux_name));
            }
            const id = row && row.id;
            if (id === undefined || id === null) return true;
            return !liveRowIds.has(Number(id));
        });
    }

    window.SessionRecentVisibility = {
        rowIdOf: rowIdOf,
        isArchived: isArchived,
        visibleRecentRows: visibleRecentRows,
    };
})();

console.log('[SessionRecentVisibility Module] Exported as window.SessionRecentVisibility');
