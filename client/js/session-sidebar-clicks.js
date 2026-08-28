/**
 * Session sidebar CLICK ROUTING - every handler for a click that lands
 * inside the list, lifted out of client/js/session-sidebar.js.
 *
 * WHY THIS IS A SEPARATE FILE. session-sidebar.js reached 556 lines this
 * round, over the project's 500-line budget, and the click handlers were
 * the largest coherent thing in it that is not lifecycle. The controller
 * keeps what it is actually about - open, close, poll, fetch, render -
 * and hands off what happens when the user clicks something.
 *
 * WHY FREE FUNCTIONS TAKING `ctrl` RATHER THAN A MIXIN OR A SUBCLASS.
 * Splitting a class across two files by assigning onto its prototype
 * makes the class definition an incomplete statement of what the class
 * is: you cannot read session-sidebar.js and know its full surface. A
 * plain function that takes the controller as its first argument is
 * honest about the dependency, is directly callable from a test with a
 * stub controller, and leaves the class in one file. The controller
 * keeps thin delegating methods so every existing caller and every
 * existing test keeps working unchanged.
 *
 * ORDER MATTERS INSIDE onRowClick and is the whole reason it is one
 * function rather than several listeners: each nested control has to
 * claim the click before the row-level switch handler sees it, or
 * clicking pin would also navigate. A second listener cannot express
 * "before".
 *
 * Must load AFTER session-sidebar-rows.js and BEFORE session-sidebar.js.
 */

console.log('[SessionSidebarClicks Module] Loading...');

(function () {
    /**
     * Description: route a click inside the list. Order matters: every
     *   nested control must claim the click before the row-level switch
     *   handler sees it, or clicking pin would also navigate.
     * Inputs: ctrl (object) - the SessionSidebarController.
     *   e (MouseEvent).
     * Output: Promise<void>.
     */
    async function onRowClick(ctrl, e) {
        // A SECTION HEADER IS A ROW OF THIS LIST, so its click arrives
        // here and has to be claimed before anything row-shaped runs. It
        // is not inside a `.session-sidebar-row`, so nothing below would
        // have matched it - but claiming it explicitly is what keeps a
        // future header control from falling through to the switch path.
        const groupToggle = e.target.closest && e.target.closest('[data-group-toggle]');
        if (groupToggle) {
            e.stopPropagation();
            onGroupToggleClick(ctrl, groupToggle);
            return;
        }
        // An edit in progress owns every click inside itself. Without
        // this the click that puts the caret in the input also reaches
        // the row handler and switches conversation out from under it.
        if (window.SessionSidebarRename && window.SessionSidebarRename.onListClick(e)) return;
        if (window.SessionSidebarReorder && window.SessionSidebarReorder.onPinClick(e)) return;

        const actionEl = window.SessionRowActions
            ? e.target.closest(`[${window.SessionRowActions.ATTR_ACTION}]`)
            : null;
        if (actionEl) {
            e.stopPropagation();
            await onRowActionClick(ctrl, actionEl);
            return;
        }

        const toggleEl = e.target.closest('[data-mark-unread]');
        if (toggleEl) {
            e.stopPropagation();
            await onMarkUnreadClick(ctrl, toggleEl);
            return;
        }

        // A click that landed on the grip was a drag gesture, not a
        // switch - the pointer handlers own it.
        if (e.target.closest('[data-grip-session]')) return;

        const rowEl = e.target.closest('.session-sidebar-row');
        if (!rowEl) return;

        // THE NAME IS THE ONE TARGET WHERE A CLICK HAS TO WAIT.
        // Double-click on the name means rename, and a browser delivers
        // the first click of a double-click before it delivers the
        // double-click, so an instant switch here would navigate away
        // from the row the user was about to edit. The wait is scoped as
        // tightly as it can be: only on the NAME, and only on a row that
        // is actually renameable. Every other part of the row, and every
        // row that has nothing to edit, still activates immediately.
        if (window.SessionSidebarRename
            && window.SessionSidebarRename.deferActivation(e, rowEl, () => activateRow(ctrl, rowEl))) {
            return;
        }
        await activateRow(ctrl, rowEl);
    }

    /**
     * Description: fold or unfold one section, persist it, and repaint.
     *   The fold lives in the arrangement envelope, so it survives a
     *   reload alongside the pins and the order it describes.
     * Inputs: ctrl (object) - the SessionSidebarController.
     *   btnEl (Element) - the clicked `[data-group-toggle]` button.
     * Output: void.
     */
    function onGroupToggleClick(ctrl, btnEl) {
        const key = btnEl.getAttribute('data-group-toggle');
        const arrangement = window.SessionSidebarArrangement;
        if (!key || !arrangement) return;
        const nowCollapsed = arrangement.toggleCollapsed(key);
        ctrl.repaint();
        const region = document.getElementById('session-sidebar-live');
        if (region) {
            region.textContent = `${key} group ${nowCollapsed ? 'collapsed' : 'expanded'}`;
        }
    }

    /**
     * Description: switch to the conversation a row names. Reuses the same
     *   two paths the launchpad uses (return-to-active vs adopt-external)
     *   so behaviour is identical regardless of the surface clicked from.
     *   Public because the keyboard path (Enter on a focused row) needs
     *   the same entry point a click takes.
     * Inputs: ctrl (object) - the SessionSidebarController.
     *   rowEl (Element) - a `.session-sidebar-row`.
     * Output: Promise<void>.
     */
    async function activateRow(ctrl, rowEl) {
        const name = rowEl && rowEl.dataset.name;
        if (!name || name === ctrl._activeTmuxName) {
            ctrl._closeAfterSwitch();
            return;
        }
        const sessionId = rowEl.dataset.sessionId || null;
        try {
            // A row carries session_id only when it came from
            // GET /sessions/list - it is bound to a live backend and can
            // be rejoined directly. No session_id means attachable-only
            // and must go through the adopt flow instead.
            if (sessionId) {
                const info = await window.API.getSession(sessionId, { includeScrollback: true });
                if (info) {
                    ctrl._closeAfterSwitch();
                    window.App.returnToExistingTerminal(info);
                }
                return;
            }
            const response = await window.API.adoptSession(name, true);
            const session = response.session || response;
            ctrl._closeAfterSwitch();
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: {
                    session,
                    initialScrollbackB64: response.initial_scrollback_b64 || '',
                    fifoStartOffset: typeof response.fifo_start_offset === 'number'
                        ? response.fifo_start_offset
                        : null,
                    adopted: true,
                },
            }));
        } catch (err) {
            console.error('SessionSidebar: switch failed:', err);
            alert(`Error: failed to switch conversation: ${err.message || err}`);
        }
    }

    /**
     * Description: toggle the manual unread flag for one row and re-render
     *   immediately (optimistic - the next poll tick reconciles either
     *   way, but a full POLL_MS with no visual feedback feels broken).
     * Inputs: ctrl (object) - the SessionSidebarController.
     *   toggleEl (Element) - the `[data-mark-unread]` span clicked.
     * Output: Promise<void>.
     */
    async function onMarkUnreadClick(ctrl, toggleEl) {
        const tmuxName = toggleEl.dataset.markUnread;
        if (!tmuxName) return;
        const next = toggleEl.dataset.unreadCurrent !== 'true';
        try {
            await window.API.setSessionUnread(tmuxName, next);
            ctrl._lastSig = null; // force a repaint even if the poll sig matches
            await ctrl._fetchAndRender();
        } catch (err) {
            console.error('SessionSidebar: mark-unread failed:', err);
        }
    }

    /**
     * Description: run a row's destructive action - close a running
     *   session (X) or remove a stopped one (trash). Which action the row
     *   painted is read back off the button, so the confirm always matches
     *   the control the user clicked.
     *
     *   THIS tab's own active session delegates straight to
     *   `TerminalController.destroySession(action)` - avoids a double
     *   confirm and a stale-WS state only that method knows how to avoid.
     *   The row's action is PASSED THROUGH rather than dropped: this
     *   branch used to call it with no argument, so an own-tab row painted
     *   with a trash still confirmed with the close copy and claimed a
     *   process was about to be terminated when it had already exited.
     * Inputs: ctrl (object) - the SessionSidebarController.
     *   btnEl (Element) - the clicked `[data-session-action]` button.
     * Output: Promise<void>. No-op if the user cancels.
     */
    async function onRowActionClick(ctrl, btnEl) {
        const actions = window.SessionRowActions;
        const name = btnEl.getAttribute(actions.ATTR_NAME);
        if (!name) return;
        const action = btnEl.getAttribute(actions.ATTR_ACTION) || actions.ACTION_CLOSE;
        const rowEl = btnEl.closest('.session-sidebar-row');
        const isThisTab = !!rowEl && rowEl.dataset.active === '1';

        // RESTART is handled before the own-tab branch and before the
        // confirm, and both of those are deliberate. It destroys nothing,
        // so it needs no dialog (SessionRowActions.requiresConfirm), and
        // it must NOT route through destroySession() even for the tab the
        // user is looking at - reviving this pane is the opposite of
        // tearing it down, and the sidebar should stay open around it.
        if (action === actions.ACTION_RESTART) {
            try {
                const result = await window.API.respawnSession(name);
                if (!result || result.ok !== true) {
                    // The server's sentence, verbatim. It is the only
                    // thing that knows whether the pane could not be read
                    // or the agent started and exited again.
                    alert(
                        `could not restart "${name}": `
                        + ((result && result.detail) || 'no reason given')
                    );
                }
            } catch (err) {
                console.error('SessionSidebar: restart failed:', err);
                alert(`could not restart "${name}": ${err.message || err}`);
            }
            ctrl._lastSig = null; // force a repaint even if the poll sig matches
            await ctrl._fetchAndRender();
            return;
        }

        if (isThisTab) {
            ctrl.close();
            await window.TerminalController.destroySession(action);
            return;
        }

        const confirmed = await actions.confirm(action, name);
        if (!confirmed) return;

        try {
            const sessionId = rowEl ? (rowEl.dataset.sessionId || null) : null;
            if (sessionId) {
                await window.API.destroySession(sessionId);
            } else {
                await window.API.destroyExternalSession(name);
            }
            ctrl._lastSig = null; // force a repaint even if the poll sig matches
            await ctrl._fetchAndRender();
        } catch (err) {
            console.error(`SessionSidebar: ${action} failed:`, err);
            alert(`Error: failed to ${action} conversation: ${err.message || err}`);
        }
    }
    window.SessionSidebarClicks = {
        onRowClick, onGroupToggleClick, activateRow,
        onMarkUnreadClick, onRowActionClick,
    };
    console.log('[SessionSidebarClicks Module] Exported as window.SessionSidebarClicks');
})();
