/**
 * Session sidebar GROUP ACTIONS - every way to change a group that is
 * NOT a drag, plus the one commit that a drag ends in.
 *
 * DRAG IS NEVER THE ONLY WAY TO DO ANYTHING HERE. That is a hard rule,
 * not a nicety. A drag needs a pointer, a steady hand and a screen big
 * enough to show both ends of the gesture at once - and this app is used
 * from a phone, where the sidebar is a narrow overlay and a long drag
 * scrolls the list out from under you. It is also completely unreachable
 * by keyboard. So every capability below has at least two routes:
 *
 *   move a session into a group   drag onto the group   |  the row's
 *                                 (session-sidebar-      |  group picker
 *                                  reorder.js)           |  (kebab menu),
 *                                                        |  or `g` on the
 *                                                        |  focused row,
 *                                                        |  or Alt+Arrow
 *                                                        |  across a band
 *                                                        |  edge
 *   create a group                the picker's "new      |  the header's
 *                                 group" entry           |  + button
 *   rename a group                the header menu        |  F2 on a
 *                                                        |  focused header
 *   delete a group                the header menu        |  -
 *   reorder groups                the header menu's      |  Alt+Arrow on a
 *                                 move up / move down    |  focused header
 *
 * The picker is a real menu of real buttons, so it is tabbable, operable
 * by Enter and Space for free, and works identically under a finger. It
 * is deliberately the SAME control from the kebab and from `g`, so there
 * is one thing to learn and one thing to test.
 *
 * ALL THREE MOVE ROUTES END IN `commitAssignment`, which ends in ONE API
 * call. They cannot drift apart in what a move means, and there is one
 * place where failure is handled.
 *
 * WHAT HAPPENS WHEN A WRITE FAILS. The optimistic move is NOT inverted
 * by hand - an invented undo is a second write path that can itself be
 * wrong. Instead the groups are RE-READ from the server, which already
 * holds the answer, and the failure is announced in words. So a move
 * that did not land visibly snaps back and says why, rather than sitting
 * on screen as a change the user believes they made.
 *
 * Must load AFTER session-sidebar-reorder.js, whose live-region
 * announcer this borrows.
 */

console.log('[SessionSidebarGroupActions Module] Loading...');

(function () {
    /** Open picker/menu element, or null. Only ever one at a time. */
    let openMenu = null;

    let wired = false;

    /**
     * Description: say something in the sidebar's live region, so a
     *   screen reader hears a group change. Routed through the reorder
     *   module rather than duplicated, so there is one live region and
     *   one voice.
     * Inputs: text (string). Output: void.
     */
    function announce(text) {
        const reorder = window.SessionSidebarReorder;
        if (reorder && reorder.announce) reorder.announce(text);
    }

    /**
     * Description: the group store, or null when it cannot be used.
     * Inputs: none. Output: object|null.
     */
    function store() {
        const G = window.SessionSidebarGroupStore;
        return (G && G.isUsable()) ? G : null;
    }

    /**
     * Description: escape text for insertion into markup.
     * Inputs: value (string). Output: string.
     */
    function esc(value) {
        return window.SessionSidebarRows.esc(String(value === null || value === undefined ? '' : value));
    }

    /**
     * Description: re-read the groups from the server and repaint. THE
     *   ONE RECOVERY PATH - used after every failed write, and after
     *   every successful one, so the rendered list always came from an
     *   authoritative payload rather than from a local guess.
     * Inputs: none. Output: Promise<void>.
     */
    async function refresh() {
        const G = window.SessionSidebarGroupStore;
        if (!G) return;
        try {
            G.apply(await window.API.listSessionGroups());
        } catch (err) {
            G.markUnavailable(err && err.message ? err.message : String(err));
        }
        repaint();
    }

    /**
     * Description: repaint the sidebar from its current rows.
     * Inputs: none. Output: void.
     */
    function repaint() {
        const sidebar = window.SessionSidebar;
        if (sidebar && typeof sidebar.repaint === 'function') sidebar.repaint();
    }

    /**
     * Description: apply a group response and repaint. Used by every
     *   write, because each route returns the WHOLE list.
     * Inputs: body (object). Output: void.
     */
    function applyAndRepaint(body) {
        const G = window.SessionSidebarGroupStore;
        if (G) G.apply(body);
        repaint();
    }

    /**
     * Description: MAKE A MOVE DURABLE. The single write behind the
     *   pointer drag, the row picker, the `g` key and Alt+Arrow across a
     *   band edge - so none of them can come to mean something different
     *   from the others.
     *
     *   On failure the local state is NOT rolled back by hand: the groups
     *   are re-read from the server, which already holds the truth, and
     *   the reason is announced. A move that did not land snaps back
     *   visibly and says why, instead of sitting on screen as a change
     *   the user believes they made.
     * Inputs: name (string) - tmux name. groupUuid (string|null) - target
     *   group, or null for ungrouped.
     * Output: Promise<boolean> - whether the write landed.
     * Example: await commitAssignment('cloude_a', 'u1')
     */
    async function commitAssignment(name, groupUuid) {
        const G = window.SessionSidebarGroupStore;
        if (G) G.setOptimistic(name, groupUuid);
        repaint();
        try {
            applyAndRepaint(await window.API.assignSessionGroup(name, groupUuid));
        } catch (err) {
            const reason = err && err.message ? err.message : String(err);
            await refresh();
            announce(`could not move ${name}: ${reason}`);
            return false;
        }
        const label = groupUuid
            ? (store() ? store().labelFor(store().bandKeyFor(groupUuid)) : 'a group')
            : 'other';
        announce(`${name} moved to ${label}`);
        return true;
    }

    /**
     * Description: close whatever menu is open, if any.
     * Inputs: none. Output: void.
     */
    function closeMenu() {
        if (openMenu && openMenu.parentNode) openMenu.parentNode.removeChild(openMenu);
        openMenu = null;
    }

    /**
     * Description: build and show a menu of buttons anchored near an
     *   element. A real `<button>` per entry, inside `role="menu"`, so it
     *   is reachable by tab, operable by Enter and Space with no extra
     *   code, and hittable by a finger - which is the whole point of
     *   having a non-drag route at all.
     * Inputs: anchor (Element) - what to sit beside.
     *   entries (Array<object>) - [{label, onPick, current}].
     *   ariaLabel (string).
     * Output: void.
     */
    function showMenu(anchor, entries, ariaLabel) {
        closeMenu();
        const menu = document.createElement('div');
        menu.className = 'session-sidebar-group-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-label', ariaLabel);
        menu.innerHTML = entries.map((entry, index) => (
            `<button type="button" role="menuitem" `
            + `class="session-sidebar-group-menu__item`
            + `${entry.current ? ' session-sidebar-group-menu__item--current' : ''}" `
            + `data-menu-index="${index}"`
            // The CURRENT group is marked with aria-checked, not with
            // colour alone - a checkmark nobody can see is not a state.
            + `${entry.current ? ' aria-checked="true" role="menuitemradio"' : ''}>`
            + `${esc(entry.label)}</button>`
        )).join('');
        const box = anchor.getBoundingClientRect();
        menu.style.position = 'fixed';
        menu.style.top = `${Math.round(box.bottom + 2)}px`;
        menu.style.left = `${Math.round(box.left)}px`;
        document.body.appendChild(menu);
        openMenu = menu;
        menu.addEventListener('click', (e) => {
            const btn = e.target.closest && e.target.closest('[data-menu-index]');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            const entry = entries[Number(btn.getAttribute('data-menu-index'))];
            closeMenu();
            if (entry && entry.onPick) entry.onPick();
        });
        const first = menu.querySelector('button');
        if (first) first.focus();
    }

    /**
     * Description: the group picker for one session row - the NON-DRAG
     *   way to file a conversation. Lists every group, plus "other", plus
     *   "new group", with the row's current group marked.
     * Inputs: anchor (Element) - what to hang the menu off.
     *   name (string) - tmux name.
     * Output: void.
     */
    function openPickerFor(anchor, name) {
        const G = store();
        if (!G) {
            announce('groups are unavailable, so this conversation cannot be filed');
            return;
        }
        const currentUuid = G.groupOf(name);
        const entries = G.current().groups.map((group) => ({
            label: group.name,
            current: group.group_uuid === currentUuid,
            onPick: () => commitAssignment(name, group.group_uuid),
        }));
        entries.push({
            label: 'other (no group)',
            current: currentUuid === null,
            onPick: () => commitAssignment(name, null),
        });
        entries.push({
            label: 'new group...',
            current: false,
            onPick: () => createGroupThenAssign(name),
        });
        showMenu(anchor, entries, `Move ${name} to a group`);
    }

    /**
     * Description: ask for a name, create the group, and file the session
     *   into it in one gesture - which is what "new group..." from a
     *   row's picker means.
     * Inputs: name (string|null) - tmux name to file, or null to just
     *   create the group.
     * Output: Promise<void>.
     */
    async function createGroupThenAssign(name) {
        const raw = window.prompt('New group name');
        if (raw === null) return;
        try {
            const body = await window.API.createSessionGroup(raw);
            applyAndRepaint(body);
            const made = (body.groups || []).find((g) => g.name === String(raw).trim()
                || g.name === raw.split(/\s+/).filter(Boolean).join(' '));
            if (name && made) await commitAssignment(name, made.group_uuid);
            else announce(`group ${raw} created`);
        } catch (err) {
            announce(`could not create the group: ${err && err.message ? err.message : err}`);
        }
    }

    /**
     * Description: the menu on a user group's own header - rename,
     *   reorder and delete. Reserved bands emit no menu button, so this
     *   is only ever reached for a real group.
     * Inputs: anchor (Element), groupUuid (string).
     * Output: void.
     */
    function openGroupMenu(anchor, groupUuid) {
        const G = store();
        if (!G) return;
        const group = G.groupByUuid(groupUuid);
        if (!group) return;
        const order = G.current().groups.map((g) => g.group_uuid);
        const at = order.indexOf(groupUuid);
        const entries = [
            { label: 'rename', onPick: () => renameGroup(groupUuid) },
            {
                label: 'move up',
                onPick: () => moveGroup(groupUuid, -1),
            },
            {
                label: 'move down',
                onPick: () => moveGroup(groupUuid, 1),
            },
            {
                label: `delete (${group.members.length} `
                    + `${group.members.length === 1 ? 'conversation' : 'conversations'} `
                    + 'move to other)',
                onPick: () => deleteGroup(groupUuid),
            },
        ];
        if (at === 0) entries.splice(1, 1);
        if (at === order.length - 1) entries.splice(entries.length - 2, 1);
        showMenu(anchor, entries, `Actions for the ${group.name} group`);
    }

    /**
     * Description: rename one group.
     * Inputs: groupUuid (string). Output: Promise<void>.
     */
    async function renameGroup(groupUuid) {
        const G = store();
        const group = G && G.groupByUuid(groupUuid);
        if (!group) return;
        const raw = window.prompt('Rename group', group.name);
        if (raw === null) return;
        try {
            applyAndRepaint(await window.API.renameSessionGroup(groupUuid, raw));
            announce(`group renamed to ${raw}`);
        } catch (err) {
            announce(`could not rename: ${err && err.message ? err.message : err}`);
        }
    }

    /**
     * Description: delete one group. ITS CONVERSATIONS ARE NOT DELETED -
     *   they become ungrouped and render in OTHER - and the confirmation
     *   says so with the real count, rather than asking the user to
     *   approve a consequence it will not name.
     * Inputs: groupUuid (string). Output: Promise<void>.
     */
    async function deleteGroup(groupUuid) {
        const G = store();
        const group = G && G.groupByUuid(groupUuid);
        if (!group) return;
        const count = group.members.length;
        const noun = count === 1 ? 'conversation' : 'conversations';
        const ok = window.confirm(
            `Delete the group "${group.name}"?\n\n`
            + `${count} ${noun} will move to "other". `
            + 'No conversation is deleted.',
        );
        if (!ok) return;
        try {
            const body = await window.API.deleteSessionGroup(groupUuid);
            applyAndRepaint(body);
            announce(`group ${group.name} deleted, ${body.freed} ${noun} moved to other`);
        } catch (err) {
            announce(`could not delete: ${err && err.message ? err.message : err}`);
        }
    }

    /**
     * Description: move one group up or down among the groups. Sends the
     *   WHOLE resulting order rather than a delta, so the client and the
     *   database cannot disagree about the result of a sequence of moves.
     * Inputs: groupUuid (string), delta (number) - -1 up, +1 down.
     * Output: Promise<void>.
     */
    async function moveGroup(groupUuid, delta) {
        const G = store();
        if (!G) return;
        const order = G.current().groups.map((g) => g.group_uuid);
        const at = order.indexOf(groupUuid);
        const to = at + (delta < 0 ? -1 : 1);
        if (at === -1 || to < 0 || to >= order.length) return;
        const next = order.slice();
        next[at] = order[to];
        next[to] = groupUuid;
        try {
            applyAndRepaint(await window.API.reorderSessionGroups(next));
            announce(`group moved to position ${to + 1} of ${order.length}`);
        } catch (err) {
            await refresh();
            announce(`could not reorder: ${err && err.message ? err.message : err}`);
        }
    }

    /**
     * Description: wire the click and key handlers once, on the document,
     *   because every element they target is destroyed and rebuilt on
     *   each repaint.
     * Inputs: none. Output: void.
     */
    function init() {
        if (wired) return;
        document.addEventListener('click', (e) => {
            const groupMenu = e.target.closest && e.target.closest('[data-group-menu]');
            if (groupMenu) {
                e.preventDefault();
                e.stopPropagation();
                openGroupMenu(groupMenu, groupMenu.getAttribute('data-group-menu'));
                return;
            }
            const picker = e.target.closest && e.target.closest('[data-group-pick]');
            if (picker) {
                e.preventDefault();
                e.stopPropagation();
                openPickerFor(picker, picker.getAttribute('data-group-pick'));
                return;
            }
            const add = e.target.closest && e.target.closest('[data-group-add]');
            if (add) {
                e.preventDefault();
                e.stopPropagation();
                createGroupThenAssign(null);
                return;
            }
            if (openMenu && !openMenu.contains(e.target)) closeMenu();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && openMenu) {
                closeMenu();
                return;
            }
            // `g` on a focused ROW opens the same picker the kebab does -
            // the keyboard twin of dragging onto a group, and the reason
            // this feature is operable with no pointer at all.
            const row = e.target.closest && e.target.closest('.session-sidebar-row');
            if (row && e.key === 'g' && !e.altKey && !e.metaKey && !e.ctrlKey) {
                e.preventDefault();
                openPickerFor(row, row.dataset.name);
                return;
            }
            // F2 on a focused group HEADER renames it, matching the
            // rename gesture the rest of this app already uses.
            const header = e.target.closest && e.target.closest('[data-group-toggle]');
            if (header && e.key === 'F2') {
                const G = window.SessionSidebarGroupStore;
                const uuid = G && G.groupUuidOf(header.getAttribute('data-group-toggle'));
                if (uuid) {
                    e.preventDefault();
                    renameGroup(uuid);
                }
            }
        });
        wired = true;
    }

    window.SessionSidebarGroupActions = {
        init, refresh, commitAssignment, openPickerFor, openGroupMenu,
        renameGroup, deleteGroup, moveGroup, createGroupThenAssign,
        closeMenu,
    };
    console.log('[SessionSidebarGroupActions Module] Exported as window.SessionSidebarGroupActions');
})();
