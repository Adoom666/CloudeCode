/**
 * Session sidebar REORDER - the interaction that puts a session where the
 * user wants it, and the pin toggle that decides which band it lives in.
 *
 * TWO INTERACTIONS, AND WHY BOTH.
 *
 * Keyboard is the PRIMARY one, not the accessibility afterthought.
 *   ArrowUp / ArrowDown      move FOCUS between rows (roving tabindex, so
 *                            the list is one tab stop, per the WAI-ARIA
 *                            practice for a vertical collection)
 *   Alt+ArrowUp / Alt+Arrow  MOVE the focused row within its band
 *   Home / End               first / last row
 *   p                        toggle the focused row's pin
 *   Enter / Space            switch to the focused session
 * The Alt modifier is doing real work: bare arrows have to stay as
 * navigation or the list stops being browsable, and Alt+Up/Down is what
 * this audience already presses to move a line in VS Code, so the gesture
 * arrives already learned. Every one of these is a REAL key event in the
 * tests, dispatched at the row, not a method call standing in for one.
 *
 * Pointer drag is the SECONDARY one, on an explicit grip handle rather
 * than the whole row, because the row's own click already means "switch
 * to this conversation" and a drag that starts anywhere on it would make
 * every mis-swipe a navigation. Built on Pointer Events, so mouse, touch
 * and pen are one code path - HTML5 drag-and-drop would have been less
 * code and silently dead on a phone.
 *
 * A MOVE ACROSS THE BOUNDARY NOW PINS OR UNPINS, AND THAT REVERSES THE
 * RULE THIS FILE USED TO STATE. It used to read "a move never changes a
 * pin", and both interactions refused to cross. The user has since asked
 * for the crossing ("not sure how hard to be able to drag the items in
 * and our of pinned group"), so the protection MOVES rather than
 * disappearing: the thing being prevented was an unpin the user could not
 * see themselves perform, not the unpin itself. So every crossing move is
 * ANNOUNCED in words in the live region, and the row's pin button changes
 * its pressed state in the same paint. The crossing arithmetic still
 * lives in exactly one place (client/js/session-sidebar-arrangement.js),
 * so the pointer and the keyboard cannot disagree about it.
 *
 * KEYBOARD PARITY IS NOT OPTIONAL HERE. A pointer-only pin/unpin would
 * make the pinned group unreachable without a mouse, so there are TWO
 * keyboard routes to it and both work: `p` toggles the focused row's pin
 * where it stands, and Alt+Arrow past a band edge crosses the boundary
 * exactly as a drag does. The LIST edges are still hard refusals - Alt+Up
 * on the very first row has nothing above it to cross past, and
 * overloading it with "pin into the empty band" would make the top row's
 * behaviour depend on state that is not on screen.
 *
 * WHICH BAND A DROP LANDS IN IS READ OFF THE GROUP UNDER THE POINTER, not
 * off the nearest row. That resolution lives in
 * client/js/session-sidebar-drop-target.js. Those differ in the one case that matters: an
 * EMPTY pinned group has no rows to be near, and inferring the band from
 * the nearest row would make it permanently undroppable - which is the
 * case the whole feature exists for. That is also why
 * client/js/session-sidebar-groups.js draws the empty pinned group while
 * and only while a drag is in flight: a drop target that appears after
 * you hit it cannot be hit.
 *
 * FOCUS SURVIVES THE REPAINT. Every move rewrites the list's innerHTML,
 * which destroys the focused element; without restoring focus onto the
 * moved row, holding Alt+ArrowUp moves a row exactly once and then goes
 * dead. The restore is the feature, not polish.
 *
 * Announcements go to the panel's live region so a screen reader hears
 * "moved to position 2 of 6" - a reorder whose only feedback is visual is
 * not operable without a mouse in any sense that matters.
 *
 * Must load AFTER session-sidebar.js and session-sidebar-arrangement.js.
 */

console.log('[SessionSidebarReorder Module] Loading...');

(function () {
    /** Pixels a pointer must travel on the grip before it counts as a drag. */
    const DRAG_SLOP_PX = 4;

    /** The row currently holding the list's single tab stop. */
    let focusName = null;

    /** In-flight pointer drag, or null. */
    let drag = null;

    let wired = false;

    /**
     * Description: the sidebar's list container, or null before wiring.
     * Inputs: none. Output: Element|null.
     */
    function listEl() {
        const sidebar = window.SessionSidebar;
        return (sidebar && sidebar.listEl) || null;
    }

    /**
     * Description: every rendered session row, top to bottom.
     * Inputs: none. Output: Array<Element>.
     */
    function rowEls() {
        const list = listEl();
        if (!list) return [];
        return Array.prototype.slice.call(list.querySelectorAll('.session-sidebar-row'));
    }

    /**
     * Description: the tmux names of the rendered rows, top to bottom.
     *   This is what the arrangement writes: the list as the user is
     *   looking at it, never a model of it.
     * Inputs: none. Output: Array<string>.
     */
    function visibleNames() {
        return rowEls().map((el) => el.dataset.name).filter((n) => !!n);
    }

    /**
     * Description: true when a pointer drag is in progress, so the poller
     *   can hold off repainting the list out from under the user's finger.
     * Inputs: none. Output: boolean.
     */
    function isDragging() { return !!drag && drag.active; }

    /**
     * Description: put the list's single tab stop on one row and give it
     *   focus. Called after every repaint so a move does not strand focus
     *   on a destroyed element.
     * Inputs: name (string|null) - row to focus, or null to just restore
     *   the tab stop without moving focus.
     *   takeFocus (boolean) - whether to actually call focus().
     * Output: void.
     */
    function setFocusRow(name, takeFocus) {
        const rows = rowEls();
        if (!rows.length) return;
        let target = rows.find((el) => el.dataset.name === name) || null;
        if (!target) target = rows[0];
        for (const el of rows) {
            el.setAttribute('tabindex', el === target ? '0' : '-1');
        }
        focusName = target.dataset.name || null;
        if (takeFocus) target.focus();
    }

    /**
     * Description: re-establish the roving tab stop after the sidebar
     *   repaints, restoring focus only when the list already had it - so
     *   a background poll tick can never steal focus from elsewhere.
     * Inputs: none. Output: void.
     */
    function afterRender() {
        const list = listEl();
        if (!list) return;
        const hadFocus = !!document.activeElement
            && list.contains(document.activeElement);
        setFocusRow(focusName, hadFocus);
    }

    /**
     * Description: say what just happened, in the panel's live region.
     * Inputs: text (string). Output: void.
     */
    function announce(text) {
        const region = document.getElementById('session-sidebar-live');
        if (region) region.textContent = text;
    }

    /**
     * Description: repaint the list from the sidebar's cached rows and put
     *   focus back on `name`. One helper because every mutation below has
     *   to do exactly this and forgetting the focus half is the bug that
     *   makes held-key repeat stop after one press.
     * Inputs: name (string) - the row to keep focused. Output: void.
     */
    function repaintKeepingFocus(name) {
        focusName = name;
        const sidebar = window.SessionSidebar;
        if (sidebar) sidebar.repaint();
        setFocusRow(name, true);
    }

    /**
     * Description: move the named row one slot up or down within its band,
     *   persist it, repaint, and announce the new position. A refused move
     *   (band edge) announces that it was refused rather than silently
     *   doing nothing, so the user is not left pressing a dead key.
     * Inputs: name (string), delta (number) - -1 up, +1 down.
     * Output: boolean - true when the row actually moved.
     */
    function moveRow(name, delta) {
        const arrangement = window.SessionSidebarArrangement;
        if (!arrangement) return false;
        const result = arrangement.move(name, delta, visibleNames());
        if (!result) {
            announce(`${name} is already at the ${delta < 0 ? 'top' : 'bottom'} of the list`);
            return false;
        }
        repaintKeepingFocus(name);
        const rows = visibleNames();
        const where = `position ${rows.indexOf(name) + 1} of ${rows.length}`;
        // A CROSSING SAYS SO. The position alone would describe the move
        // accurately and still hide the part the user most needs to know,
        // which is that the pin changed.
        announce(result.crossed
            ? `${name} ${result.pinned ? 'pinned' : 'unpinned'}, now at ${where}`
            : `${name} moved to ${where}`);
        return true;
    }

    /**
     * Description: flip the named row's pin, persist, repaint, announce.
     * Inputs: name (string). Output: boolean - the new pinned state.
     */
    function togglePinRow(name) {
        const arrangement = window.SessionSidebarArrangement;
        if (!arrangement) return false;
        const now = arrangement.togglePin(name, visibleNames());
        repaintKeepingFocus(name);
        announce(now ? `${name} pinned to the top` : `${name} unpinned`);
        return now;
    }

    /**
     * Description: keyboard handling for the row list. Returns early for
     *   anything that is not a row key so the sidebar's own Enter/Space
     *   handling for nested controls is untouched.
     * Inputs: e (KeyboardEvent). Output: void.
     */
    function onKeydown(e) {
        const row = e.target.closest && e.target.closest('.session-sidebar-row');
        if (!row || !row.dataset.name) return;
        // A nested control (pin button, grip, mark-unread, delete) owns its
        // own keys; the row-level handler must not also fire for them.
        if (e.target !== row) return;
        const name = row.dataset.name;
        const rows = rowEls();
        const idx = rows.indexOf(row);

        // F2 is the platform's rename key and it is handed off whole, so
        // the gate on the three renameability states lives in exactly one
        // place rather than being restated here.
        if (window.SessionSidebarRename
            && window.SessionSidebarRename.onRowKeydown(e, row)) return;

        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            const step = e.key === 'ArrowDown' ? 1 : -1;
            if (e.altKey) { moveRow(name, step); return; }
            const next = rows[idx + step];
            if (next) setFocusRow(next.dataset.name, true);
            return;
        }
        if (e.key === 'Home' || e.key === 'End') {
            e.preventDefault();
            const target = e.key === 'Home' ? rows[0] : rows[rows.length - 1];
            if (target) setFocusRow(target.dataset.name, true);
            return;
        }
        if (e.key === 'p' || e.key === 'P') {
            e.preventDefault();
            togglePinRow(name);
            return;
        }
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            const sidebar = window.SessionSidebar;
            if (sidebar) sidebar.activateRow(row);
        }
    }

    /**
     * Description: click handling for the per-row pin button. The pin is a
     *   real control inside a row whose own click switches conversations,
     *   so it stops propagation - clicking pin must never also navigate.
     * Inputs: e (MouseEvent). Output: boolean - true when handled.
     */
    function onPinClick(e) {
        const btn = e.target.closest && e.target.closest('[data-pin-session]');
        if (!btn) return false;
        e.stopPropagation();
        e.preventDefault();
        togglePinRow(btn.getAttribute('data-pin-session'));
        return true;
    }

    /**
     * Description: begin a potential grip drag. Nothing moves until the
     *   pointer has travelled DRAG_SLOP_PX, so a tap on the grip is not a
     *   zero-distance reorder.
     * Inputs: e (PointerEvent). Output: void.
     */
    function onPointerDown(e) {
        const grip = e.target.closest && e.target.closest('[data-grip-session]');
        if (!grip) return;
        const row = grip.closest('.session-sidebar-row');
        if (!row) return;
        e.preventDefault();
        e.stopPropagation();
        drag = {
            name: grip.getAttribute('data-grip-session'),
            startY: e.clientY,
            active: false,
            // undefined until a placement actually crosses the boundary,
            // so "did not cross" and "crossed into unpinned" stay
            // distinguishable - false would collapse them.
            crossed: undefined,
            // The group this drag has optimistically moved the row into,
            // and whether it moved at all. `group` stays undefined until
            // a placement actually names one, so "was never over a group
            // band" and "was dropped into ungrouped" stay distinguishable
            // - the same reason `crossed` starts undefined rather than
            // false.
            group: undefined,
            groupChanged: false,
            pointerId: e.pointerId,
        };
        if (grip.setPointerCapture) {
            try { grip.setPointerCapture(e.pointerId); } catch (_) { /* not captured */ }
        }
    }

    /**
     * Description: during a grip drag, find which row the pointer is over
     *   and, when that is a different row, commit the move immediately so
     *   the list reorders live under the finger.
     * Inputs: e (PointerEvent). Output: void.
     */
    function onPointerMove(e) {
        if (!drag) return;
        if (!drag.active) {
            if (Math.abs(e.clientY - drag.startY) < DRAG_SLOP_PX) return;
            drag.active = true;
            const list = listEl();
            if (list) list.classList.add('session-sidebar-list--dragging');
            // Repaint FIRST, so an empty pinned group exists to be
            // dropped into before the pointer can reach it.
            const sidebar = window.SessionSidebar;
            if (sidebar) sidebar.setDragging(true);
        }
        const arrangement = window.SessionSidebarArrangement;
        if (!arrangement) return;
        const rows = rowEls();
        let beforeName = null;
        for (const el of rows) {
            if (el.dataset.name === drag.name) continue;
            const box = el.getBoundingClientRect();
            if (e.clientY < box.top + box.height / 2) {
                beforeName = el.dataset.name;
                break;
            }
        }
        // The band comes from the GROUP under the pointer; only when the
        // list is ungrouped does it fall back to the row's current band,
        // which in that case is the only band there is.
        const G = window.SessionSidebarGroupStore;
        const overKey = window.SessionSidebarDropTarget.bandKeyAt(e.clientY);
        const intent = (overKey !== null && G)
            ? G.bandIntent(overKey)
            : { pinned: arrangement.isPinned(drag.name), group: undefined };

        // A DROP IS TWO FACTS, AND ONLY ONE OF THEM IS LOCAL. The pin and
        // the order live in localStorage and commit here, per pointer
        // sample, because the list has to reorder under the finger. The
        // GROUP lives in the database and is only moved OPTIMISTICALLY
        // here - the write happens once, on pointer up. Firing a request
        // per sample would hammer the API and could land out of order.
        //
        // `intent.group === undefined` means LEAVE THE FILING ALONE and
        // is what the pinned band returns; `null` means REMOVE it. The
        // `in` test rather than a truthiness test is what keeps those
        // apart, and collapsing them would make dragging a row into the
        // pinned band quietly empty the group it was filed in.
        if (G && intent.group !== undefined) {
            const before = G.setOptimistic(drag.name, intent.group);
            if (before !== intent.group) drag.groupChanged = true;
            drag.group = intent.group;
        }

        const result = arrangement.placeAt(
            drag.name, intent.pinned, beforeName, visibleNames(),
        );
        // A pure GROUP change moves no row within its band and flips no
        // pin, so placeAt correctly reports "nothing to do" - but the row
        // still has to be repainted into its new section. Returning early
        // on a null here is what would make a drag between two groups
        // look like it did nothing.
        //
        // NOT COVERED BY A TEST, AND SAID SO RATHER THAN IMPLIED.
        // scripts/verify_sidebar_group_drag.py measures the row's band
        // mid-drag with the pointer still down, and that assertion passes
        // WITH AND WITHOUT this branch - measured, not assumed. The
        // reason is that the first pointer sample to reach the target
        // band also changes the flat visible order (the empty pinned band
        // appearing shifts every row), so placeAt returns non-null there
        // and repaints anyway; placeAt was instrumented and returns null
        // on 6 of 8 samples, so the branch DOES execute, but its removal
        // is not observable in any scenario that could be constructed.
        // It is kept because the case it guards is real - a drag whose
        // flat order never changes at any sample would leave the row
        // stuck under the finger until release - and deleting correct
        // defensive code because a harness cannot reach it is how the
        // narrow case ships broken. If you can build that scenario,
        // assert it here and delete this comment.
        if (!result) {
            if (drag.groupChanged) repaintKeepingFocus(drag.name);
            return;
        }
        if (result.crossed) drag.crossed = result.pinned;
        repaintKeepingFocus(drag.name);
    }

    /**
     * Description: end a grip drag, announcing the final position when the
     *   drag actually moved something.
     * Inputs: e (PointerEvent). Output: void.
     */
    function onPointerUp(e) {
        if (!drag) return;
        const wasActive = drag.active;
        const name = drag.name;
        const crossed = drag.crossed;
        const group = drag.group;
        const groupChanged = drag.groupChanged;
        drag = null;
        const list = listEl();
        if (list) list.classList.remove('session-sidebar-list--dragging');
        // Drop the drag flag LAST, and unconditionally: an empty pinned
        // group that was drawn as a drop target has to disappear again
        // even when the drag never moved anything, or a cancelled drag
        // leaves a header standing over nothing.
        const sidebar = window.SessionSidebar;
        if (sidebar) sidebar.setDragging(false);
        if (!wasActive) return;
        const rows = visibleNames();
        const where = `position ${rows.indexOf(name) + 1} of ${rows.length}`;
        const actions = window.SessionSidebarGroupActions;
        if (groupChanged && actions) {
            // COMMIT ONCE, HERE. The optimistic move already happened, so
            // this only makes it durable - and if it fails, the actions
            // module re-reads from the server and says so out loud rather
            // than leaving the user looking at a move that did not land.
            actions.commitAssignment(name, group === undefined ? null : group);
            return;
        }
        announce(crossed === undefined
            ? `${name} moved to ${where}`
            : `${name} ${crossed ? 'pinned' : 'unpinned'}, now at ${where}`);
    }

    /**
     * Description: wire the list's keyboard and pointer handlers once.
     *   Bound to the stable list container rather than the rows, which are
     *   destroyed and rebuilt on every repaint.
     * Inputs: none. Output: void.
     */
    function init() {
        if (wired) return;
        const list = listEl();
        if (!list) return;
        list.addEventListener('keydown', onKeydown);
        list.addEventListener('pointerdown', onPointerDown);
        document.addEventListener('pointermove', onPointerMove);
        document.addEventListener('pointerup', onPointerUp);
        document.addEventListener('pointercancel', onPointerUp);
        wired = true;
    }

    window.SessionSidebarReorder = {
        init, afterRender, moveRow, togglePinRow, onPinClick, isDragging,
        setFocusRow, visibleNames, announce, DRAG_SLOP_PX,
    };
    console.log('[SessionSidebarReorder Module] Exported as window.SessionSidebarReorder');
})();
