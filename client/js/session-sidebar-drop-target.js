/**
 * Session sidebar DROP TARGET - which band the pointer is over.
 *
 * One question, one file. It was inside
 * client/js/session-sidebar-reorder.js until that file crossed this
 * project's 500-line budget, and it is genuinely a separate job: reorder
 * owns the GESTURE (slop, capture, commit, focus, announcement) and this
 * owns the HIT TEST.
 *
 * THE BAND IS READ OFF THE GROUP CONTAINER UNDER THE POINTER, NEVER OFF
 * THE NEAREST ROW, and that is the whole reason this is not a one-liner.
 * An EMPTY band has no rows to be near, so inferring a band from the
 * nearest row would make an empty band permanently undroppable - which
 * is precisely the case the feature exists for. That used to be only the
 * empty pinned band; a user can now create an empty group deliberately
 * and expects to be able to drop into it, which is also why an empty
 * user group is drawn in steady state while an empty pinned band is not
 * (see client/js/session-sidebar-groups.js).
 *
 * The header counts as part of its own group, so dropping onto the word
 * "work" files into "work" - which is what the gesture looks like it
 * should do. A pointer below every group lands in the last one.
 *
 * Must load BEFORE session-sidebar-reorder.js.
 */

console.log('[SessionSidebarDropTarget Module] Loading...');

(function () {
    /**
     * Description: the sidebar's list container, or null before wiring.
     * Inputs: none. Output: Element|null.
     */
    function listEl() {
        const sidebar = window.SessionSidebar;
        return (sidebar && sidebar.listEl) || null;
    }

    /**
     * Description: WHICH BAND the pointer is over, as a band key.
     * Inputs: clientY (number) - the pointer's viewport Y.
     * Output: string|null - a band key ('pinned', 'other', 'g:<uuid>'),
     *   or null when the list is ungrouped and there is no band to name.
     *   Null is NOT "other": an ungrouped list has no sections at all,
     *   and the caller reads it as "leave the band alone".
     * Example: SessionSidebarDropTarget.bandKeyAt(420) // 'g:3f2a...'
     */
    function bandKeyAt(clientY) {
        const list = listEl();
        if (!list) return null;
        const groups = Array.prototype.slice.call(
            list.querySelectorAll('.session-sidebar-group[data-group]'),
        );
        if (!groups.length) return null;
        let last = null;
        for (const g of groups) {
            const box = g.getBoundingClientRect();
            last = g;
            if (clientY < box.bottom) return g.getAttribute('data-group');
        }
        return last ? last.getAttribute('data-group') : null;
    }

    /**
     * Description: the pre-groups two-band form of `bandKeyAt`, kept
     *   because it is the shape the pin-crossing logic and its tests were
     *   written against.
     * Inputs: clientY (number).
     * Output: boolean|null - true pinned, false not, null unknown.
     */
    function bandAt(clientY) {
        const key = bandKeyAt(clientY);
        return key === null ? null : key === 'pinned';
    }

    window.SessionSidebarDropTarget = { bandKeyAt, bandAt };
    console.log('[SessionSidebarDropTarget Module] Exported as window.SessionSidebarDropTarget');
})();
