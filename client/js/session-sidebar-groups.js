/**
 * Session sidebar GROUPS - the section headers that split the list into
 * the pinned band, the user's own named groups, and everything else.
 *
 * THIS FILE USED TO SAY IT DREW A TWO-BAND SPLIT AND NOTHING MORE. That
 * was true when the only seam was pinned-versus-rest. The user can now
 * make their OWN groups, which live in the database (see
 * src/core/session_group_store.py and
 * client/js/session-sidebar-group-store.js), so the number of sections is
 * no longer two and no longer fixed.
 *
 * THIS IS STILL PRESENTATION OVER A MODEL THAT ALREADY DECIDED. Nothing
 * here re-decides which band a row is in: `arrange()` stamps `band_key`
 * on every row, the group store owns `bandOrder()`, and this module only
 * buckets and draws. There is still exactly one piece of state stored
 * here - which sections are folded - and it still rides the SAME
 * localStorage envelope with no VERSION bump; a user group's fold is
 * `g:<uuid>` beside `pinned` and `other`, and an older build that reads
 * one drops it and unfolds that section, losing nothing, because a fold
 * is graded as a preference and not as data.
 *
 * PINNED IS NOT A GROUP, and the header row says so without a caption:
 * a user group's header carries a menu button (rename, reorder, delete),
 * and `pinned` and `other` do not, because there is nothing to rename or
 * delete about them. Offering the control would be offering an action
 * that cannot work.
 *
 * TWO RULES ABOUT WHEN A HEADER EXISTS AT ALL, and they are different:
 *
 *   1. AN EMPTY PINNED GROUP RENDERS NOTHING. Not a header with no rows
 *      under it: nothing. A bare header for a band the user has never put
 *      anything in is chrome that asks a question nobody asked.
 *   2. WITH NOTHING PINNED AND NO USER GROUPS, THE REST RENDERS
 *      UNGROUPED TOO. Headers exist to SEPARATE. With one band there is
 *      nothing to separate it from, so a lone "other" header over the
 *      entire list would be a label pretending to be a division. The
 *      seam appears when there is a seam.
 *   2b. A USER'S OWN EMPTY GROUP ALWAYS RENDERS, which is the exact
 *      opposite of rule 1 and deliberately so. An empty PINNED band is
 *      chrome asking a question nobody asked; an empty group is a thing
 *      the user deliberately made, and a group that vanished when you
 *      emptied it could never be refilled.
 *
 * THE ONE EXCEPTION IS A DRAG IN FLIGHT. While a row is being dragged the
 * pinned group is drawn even when empty, because it is the drop target
 * that pins - and a target that only exists once you have already hit it
 * cannot be hit. Steady state is still rule 1: the moment the drag ends,
 * an empty pinned group disappears again.
 *
 * A COLLAPSED GROUP DOES NOT RENDER ITS ROWS - it does not render them
 * hidden. `client/js/session-sidebar-reorder.js` reads the visible order
 * straight off the DOM with `querySelectorAll('.session-sidebar-row')`,
 * which finds a `hidden` element just as happily as a visible one, so
 * folding a section that still held its rows would leave every reorder
 * and every drag computing positions against rows nobody can see. The
 * remembered order survives regardless: `mergeOrder()` keeps names it is
 * not shown in the slots they already hold.
 *
 * Must load AFTER session-sidebar-rows.js and BEFORE session-sidebar.js.
 */

console.log('[SessionSidebarGroups Module] Loading...');

(function () {
    /**
     * The section keys, top to bottom. `pinned` is first because that is
     * the whole request; `other` is everything the user has not pinned.
     * @type {Array<string>}
     */
    const KEYS = ['pinned', 'other'];

    /**
     * Human label per section key. Lowercase, matching the app's UI voice
     * and the panel's own "conversations" title.
     * @type {Object<string, string>}
     */
    const LABELS = { pinned: 'pinned', other: 'other' };

    /**
     * Description: the chevron that shows and toggles a section's fold.
     *   Presentational only - `aria-expanded` on the button carries the
     *   real state, so the fold is never shape-only. The rotation lives in
     *   CSS keyed off that same attribute, so the glyph cannot disagree
     *   with the state it is drawn from.
     * Inputs: none.
     * Output: string - HTML for one `<svg>`.
     */
    function chevronHtml() {
        return (
            '<span class="session-sidebar-group__chevron" aria-hidden="true">'
            + '<svg width="10" height="10" viewBox="0 0 12 12" fill="none" '
            + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            + 'stroke-linejoin="round"><path d="M4 2l4 4-4 4"/></svg></span>'
        );
    }

    /**
     * Description: one section header, drawn as a row of the list rather
     *   than as a caption floating above it. It is a real `<button>` so
     *   the fold is operable by keyboard and reachable by tab, and it
     *   carries `aria-expanded` plus `aria-controls` pointing at the body
     *   it opens.
     * Inputs: key (string) - one of KEYS. count (number) - rows in the
     *   section, shown so a folded section still says how much it hides.
     *   collapsed (boolean).
     * Output: string - HTML.
     * Example: headerHtml('pinned', 2, false)
     */
    function headerHtml(key, count, collapsed) {
        const esc = window.SessionSidebarRows.esc;
        const label = labelFor(key);
        const bodyId = `session-sidebar-group-body-${esc(key)}`;
        const verb = collapsed ? 'Expand' : 'Collapse';
        const title = `${verb} the ${label} group (${count} `
            + `${count === 1 ? 'conversation' : 'conversations'})`;
        const uuid = groupUuidOf(key);
        // A USER GROUP GETS A MENU BUTTON; A RESERVED BAND DOES NOT, and
        // that asymmetry is the UI saying out loud that pinned is not a
        // group. There is nothing to rename or delete about "pinned" or
        // "other", so offering the control would be offering an action
        // that cannot work.
        const menu = uuid
            ? (`<button type="button" class="session-sidebar-group__menu" `
                + `data-group-menu="${esc(uuid)}" `
                + `title="${esc(`Rename, reorder or delete the ${label} group`)}" `
                + `aria-label="${esc(`Rename, reorder or delete the ${label} group`)}" `
                + `aria-haspopup="menu">`
                + '<span aria-hidden="true">&#8943;</span></button>')
            : '';
        return (
            '<div class="session-sidebar-group__headerrow">'
            + `<button type="button" class="session-sidebar-group__header" `
            + `data-group-toggle="${esc(key)}" `
            + `aria-expanded="${collapsed ? 'false' : 'true'}" `
            + `aria-controls="${bodyId}" `
            + `title="${esc(title)}" aria-label="${esc(title)}">`
            + chevronHtml()
            + `<span class="session-sidebar-group__label">${esc(label)}</span>`
            + `<span class="session-sidebar-group__count">${count}</span>`
            + '</button>'
            + menu
            + '</div>'
        );
    }

    /**
     * Description: the group store, or null when groups are unknown or
     *   unreadable. A null is a MEANINGFUL state - draw the pre-groups
     *   two-band list - not an error.
     * Inputs: none. Output: object|null.
     */
    function store() {
        const G = window.SessionSidebarGroupStore;
        return (G && G.isUsable()) ? G : null;
    }

    /**
     * Description: the group uuid inside a band key, or null for a
     *   reserved band.
     * Inputs: key (string). Output: string|null.
     */
    function groupUuidOf(key) {
        const G = window.SessionSidebarGroupStore;
        if (G) return G.groupUuidOf(key);
        return null;
    }

    /**
     * Description: the human label for a band key, falling back to the
     *   reserved labels when the group store cannot answer. NEVER
     *   returns an empty string: a header that renders as nothing is a
     *   section the user cannot click.
     * Inputs: key (string). Output: string.
     */
    function labelFor(key) {
        const G = window.SessionSidebarGroupStore;
        if (G) {
            const label = G.labelFor(key);
            if (label && label.trim()) return label;
        }
        return LABELS[key] || key;
    }

    /**
     * Description: one whole section - header plus, when it is open, its
     *   rows. A folded section emits an EMPTY body element rather than no
     *   body at all, so `aria-controls` always resolves to something real
     *   and the drop target for a drag survives the fold.
     * Inputs: key (string), rows (Array<object>), density (string),
     *   collapsed (boolean).
     * Output: string - HTML.
     */
    function sectionHtml(key, rows, density, collapsed) {
        const esc = window.SessionSidebarRows.esc;
        const bodyId = `session-sidebar-group-body-${esc(key)}`;
        const body = collapsed
            ? ''
            : rows.map((r) => window.SessionSidebarRows.rowHtml(r, density)).join('');
        return (
            `<div class="session-sidebar-group" role="group" data-group="${esc(key)}" `
            + `data-collapsed="${collapsed ? '1' : '0'}" data-count="${rows.length}">`
            + headerHtml(key, rows.length, collapsed)
            + `<div class="session-sidebar-group__body" id="${bodyId}">${body}</div>`
            + '</div>'
        );
    }

    /**
     * Description: split arranged rows into the two bands. `is_pinned` is
     *   stamped by `SessionSidebarArrangement.arrange()`, which is the one
     *   place that decides it; this only reads it.
     * Inputs: rows (Array<object>) - already arranged, pinned band first.
     * Output: object - {pinned (Array<object>), other (Array<object>)}.
     */
    function split(rows) {
        const list = Array.isArray(rows) ? rows : [];
        return {
            pinned: list.filter((r) => !!r.is_pinned),
            other: list.filter((r) => !r.is_pinned),
        };
    }

    /**
     * Description: bucket arranged rows by their `band_key`, in the band
     *   order the group store declares. `arrange()` already stamped the
     *   key and already ordered the rows, so this only groups them - it
     *   never re-decides which band a row is in, which is what keeps one
     *   answer to that question in one place.
     * Inputs: rows (Array<object>) - arranged rows carrying `band_key`.
     * Output: Array<object> - [{key, rows}] top to bottom.
     */
    function bands(rows) {
        const G = store();
        const keys = G ? G.bandOrder() : KEYS;
        const buckets = new Map(keys.map((k) => [k, []]));
        for (const row of (Array.isArray(rows) ? rows : [])) {
            const key = buckets.has(row.band_key)
                ? row.band_key
                : (row.is_pinned ? 'pinned' : 'other');
            if (buckets.has(key)) buckets.get(key).push(row);
        }
        return keys.map((key) => ({ key, rows: buckets.get(key) }));
    }

    /**
     * Description: the grouped body of the list, or the ungrouped rows
     *   when there is no seam to draw. See the file docblock for the two
     *   rules and the one drag exception.
     * Inputs: rows (Array<object>) - arranged rows, pinned band first.
     *   density (string). arrangement (object|null) - the arrangement
     *   state, read for its `collapsed` list only. opts (object) -
     *   {dragging (boolean)}, which forces the empty pinned group to be
     *   drawn as a drop target.
     * Output: string - HTML.
     * Example: SessionSidebarGroups.bodyHtml(rows, 'cozy', state, {})
     */
    function bodyHtml(rows, density, arrangement, opts) {
        const dragging = !!(opts && opts.dragging);
        const folded = (arrangement && Array.isArray(arrangement.collapsed))
            ? arrangement.collapsed
            : [];
        const isFolded = (key) => folded.indexOf(key) !== -1;
        const sections = bands(rows);
        const userGroups = sections.filter(
            (b) => b.key !== 'pinned' && b.key !== 'other',
        );

        // RULE 1 AND 2 STILL HOLD, now over N bands: headers exist to
        // SEPARATE, so with nothing pinned AND no user groups there is
        // nothing to separate and the list renders flat, exactly as it
        // did before groups existed. The moment either one has content
        // there is a seam, and every section gets a header.
        const pinned = sections.find((b) => b.key === 'pinned');
        const hasPinned = !!(pinned && pinned.rows.length);
        if (!hasPinned && userGroups.length === 0 && !dragging) {
            const other = sections.find((b) => b.key === 'other');
            return (other ? other.rows : [])
                .map((r) => window.SessionSidebarRows.rowHtml(r, density))
                .join('');
        }

        return sections
            // An EMPTY PINNED BAND still renders nothing in steady state
            // (rule 1) - and still IS drawn mid-drag, because the drop
            // target that pins cannot appear only after you have hit it.
            // A user's own empty group is the opposite case and always
            // renders: they made it deliberately, and a group that
            // vanishes when you empty it cannot be refilled.
            .filter((b) => !(b.key === 'pinned' && b.rows.length === 0 && !dragging))
            .map((b) => sectionHtml(b.key, b.rows, density, isFolded(b.key)))
            .join('');
    }

    window.SessionSidebarGroups = {
        bodyHtml, sectionHtml, headerHtml, chevronHtml, split, bands,
        labelFor, KEYS, LABELS,
    };
    console.log('[SessionSidebarGroups Module] Exported as window.SessionSidebarGroups');
})();
