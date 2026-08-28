/**
 * Server status - the panel behind the home bar's second menu row.
 * ----------------------------------------------------------------------
 * WHAT IT IS FOR: one place that answers "what does this machine and this
 * server look like right now, and what is running on the tmux socket".
 * Everything it shows is READ-ONLY except one control: closing a tmux
 * session.
 *
 * THAT CONTROL DOES NOT KILL ANYTHING ITSELF. It routes through
 * `SessionRowActions` - the module that already owns the confirmation
 * copy and the choice between the two destruction endpoints for every
 * other session row in the app. A panel with its own kill call would be a
 * second way to destroy a session, and the two would drift; the launcher
 * and the sidebar already prove how fast that happens.
 *
 * THE CONFIRMATION NAMES THE SESSION, AND ADDS THE TWO FACTS A LIST
 * CANNOT SHOW. From a list of eight rows, "are you sure?" is how the
 * wrong one gets killed, so the name is in the dialog. And the panel
 * knows two things the row does not say out loud - whether the session is
 * open in this app right now, and how many tmux clients are attached to
 * it - so both are passed as context and lead the copy. The rest of the
 * copy is the shared table's, unchanged, because it is already true: the
 * process dies, the uploads bucket goes with it on the tracked path, and
 * the transcript under ~/.claude/projects survives.
 *
 * WHY IT REFETCHES AFTER A KILL rather than removing the row locally: the
 * row is a rendering of a server-side fact, and a local removal would be
 * this panel asserting an outcome it did not measure.
 *
 * THE RELEASE SECTION IS NOT OURS. Version and self-check plumbing is
 * owned by the packaging work; this panel fetches it separately and
 * renders "could not check" when it is absent, which is also exactly what
 * it must render when the check is merely offline. See
 * `ServerStatusFormat.renderRelease` for the shape assumed.
 */

console.log('[ServerStatusPanel Module] Loading...');

(function () {
    'use strict';

    /** The open overlay, or null. Only ever one. */
    var overlayEl = null;

    /** Element to hand focus back to on close. */
    var triggerEl = null;

    /**
     * Fetch the status snapshot, or null when the call failed.
     *
     * @returns {Promise<object|null>}
     */
    async function fetchStatus() {
        try {
            return await window.API.getServerStatus();
        } catch (err) {
            console.error('ServerStatusPanel: status fetch failed', err);
            return null;
        }
    }

    /**
     * Fetch the release/self-check payload, or a could-not-check marker.
     *
     * A failure here is deliberately NOT fatal to the panel and is never
     * silently dropped: an absent version check renders as "could not
     * check" with the reason, because an unanswered check that looked
     * like "up to date" would be a lie with a long shelf life.
     *
     * @returns {Promise<object>} the payload, or `{available: false,
     *   error: <reason>}`.
     */
    async function fetchRelease() {
        if (!window.API || typeof window.API.getReleaseStatus !== 'function') {
            return {
                available: false,
                error: 'this build has no version check'
            };
        }
        try {
            return await window.API.getReleaseStatus();
        } catch (err) {
            return { available: false, error: releaseFailureReason(err) };
        }
    }

    /**
     * Turn a failed release fetch into lowercase copy the panel can show.
     *
     * A raw `err.message` is the server's or the fetch layer's wording,
     * which is capitalised ("Not Found") and sometimes carries a URL.
     * Neither belongs in this app's voice, and "Not Found" alone tells the
     * user nothing about what was not found.
     *
     * @param {Error|null} err - the rejection from the API call.
     * @returns {string} a lowercase reason.
     */
    function releaseFailureReason(err) {
        var raw = (err && err.message) || '';
        if (/not found|404/i.test(raw)) {
            return 'this server has no version check yet';
        }
        if (!raw) return 'the version check did not answer';
        return 'the version check did not answer: ' + raw.toLowerCase();
    }

    /**
     * Repaint the body from the server.
     *
     * @param {HTMLElement} bodyEl - the panel body.
     * @returns {Promise<void>}
     */
    async function refresh(bodyEl) {
        if (!bodyEl) return;
        bodyEl.textContent = 'loading...';
        var results = await Promise.all([fetchStatus(), fetchRelease()]);
        var snapshot = results[0];
        var release = results[1];
        if (!snapshot) {
            bodyEl.innerHTML = window.ServerStatusFormat.line(
                'server status',
                'cannot determine: the server did not answer',
                'server-status-value--unknown'
            );
            return;
        }
        bodyEl.innerHTML = window.ServerStatusFormat.renderBody(
            snapshot, undefined, release
        );
    }

    /**
     * Handle a click on a session row's close control.
     *
     * @param {HTMLElement} btn - the clicked button.
     * @param {HTMLElement} bodyEl - the panel body, repainted after.
     * @returns {Promise<void>}
     */
    async function onKillClick(btn, bodyEl) {
        var actions = window.SessionRowActions;
        if (!actions) {
            // Load-order bug, not a user-facing state. Refuse to run a
            // destructive action without the shared confirm rather than
            // fall back to an unreviewed second path.
            console.error('[ServerStatusPanel] SessionRowActions missing, refusing to act');
            return;
        }
        var name = btn.getAttribute('data-kill-name') || '';
        var sessionId = btn.getAttribute('data-kill-id') || null;
        var confirmed = await actions.confirm(actions.ACTION_CLOSE, name, {
            openInApp: btn.getAttribute('data-kill-open') === '1',
            attachedClients: Number(btn.getAttribute('data-kill-attached')) || 0
        });
        if (!confirmed) return;

        btn.disabled = true;
        try {
            await actions.perform(name, sessionId);
            if (window.FabMenu) {
                window.FabMenu.notify('closed "' + name + '"', 'success');
            }
        } catch (err) {
            btn.disabled = false;
            if (window.FabMenu) {
                window.FabMenu.notify(
                    'could not close "' + name + '": '
                    + ((err && err.message) || 'the server refused'),
                    'error'
                );
            }
            return;
        }
        await refresh(bodyEl);
    }

    /**
     * Close the panel and hand focus back to whatever opened it.
     *
     * @returns {void}
     */
    function close() {
        if (!overlayEl) return;
        if (window.ModalStack) window.ModalStack.pop(overlayEl);
        if (overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
        overlayEl = null;
        if (triggerEl && typeof triggerEl.focus === 'function') {
            try { triggerEl.focus(); } catch (_) { /* no-op */ }
        }
        triggerEl = null;
    }

    /**
     * Open the panel. Safe to call while already open.
     *
     * @param {HTMLElement|null} opener - element to refocus on close.
     * @returns {Promise<void>}
     */
    async function open(opener) {
        if (overlayEl) return;
        triggerEl = opener || document.activeElement;

        overlayEl = document.createElement('div');
        overlayEl.className = 'modal-overlay';
        overlayEl.setAttribute('data-modal', 'server-status');
        overlayEl.innerHTML = ''
            + '<div class="server-status-content" role="dialog" aria-modal="true"'
            + ' aria-labelledby="server-status-title">'
            + '  <div class="modal-header server-status-header">'
            + '    <h2 id="server-status-title">server status</h2>'
            + '    <button type="button" class="server-status-refresh"'
            + '            id="server-status-refresh" title="refresh"'
            + '            aria-label="refresh">refresh</button>'
            + '    <button type="button" class="modal-close" id="server-status-close"'
            + '            title="close" aria-label="close server status">&times;</button>'
            + '  </div>'
            + '  <div class="modal-body server-status-body" id="server-status-body">'
            + 'loading...</div>'
            + '</div>';

        document.body.appendChild(overlayEl);
        if (window.ModalStack) {
            window.ModalStack.push(overlayEl, { onEscape: close });
        }

        var bodyEl = overlayEl.querySelector('#server-status-body');
        overlayEl.querySelector('#server-status-close')
            .addEventListener('click', close);
        overlayEl.querySelector('#server-status-refresh')
            .addEventListener('click', function () { refresh(bodyEl); });
        overlayEl.addEventListener('click', function (e) {
            if (e.target === overlayEl) close();
        });
        overlayEl.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                e.stopPropagation();
                close();
            }
        });
        // One delegated handler, so rows repainted by refresh() stay live.
        bodyEl.addEventListener('click', function (e) {
            var btn = e.target && e.target.closest
                ? e.target.closest('.server-status-kill')
                : null;
            if (btn) onKillClick(btn, bodyEl);
        });

        await refresh(bodyEl);
    }

    /**
     * True while the panel is on screen.
     * @returns {boolean}
     */
    function isOpen() {
        return overlayEl !== null;
    }

    window.ServerStatusPanel = {
        open: open,
        close: close,
        isOpen: isOpen,
        refresh: refresh
    };
})();

console.log('[ServerStatusPanel Module] Exported as window.ServerStatusPanel');
