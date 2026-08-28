/**
 * Session Row Actions - the single source of truth for the destructive
 * control a session row offers, in EVERY place a session row is drawn
 * (the launcher's "running sessions" list in client/js/launchpad.js and
 * the in-terminal conversation sidebar in client/js/session-sidebar.js).
 *
 * The two actions, and the promise each one makes to the user:
 *
 *   X (close)      kills the tmux session. The Claude process stops, and
 *                  that part is irreversible. The transcript JSONL is NOT
 *                  touched and stays under ~/.claude/projects, so the
 *                  conversation can be resumed later by Claude Code
 *                  itself.
 *   trash (remove) forgets the entry in CloudeCode. Only offered for a
 *                  row whose process has ALREADY exited, so there is
 *                  nothing running left to terminate; clearing the
 *                  leftover tmux husk is what makes the entry go away.
 *
 * NEITHER action is disk-free. Both can route to DELETE /sessions, which
 * removes the session's `.cloude_uploads` bucket from the project folder.
 * See the CONFIRM_COPY docblock below for the full per-path breakdown -
 * that is the authoritative account, and the dialog copy is written from
 * it. Do not describe either action as leaving disk untouched.
 *
 * SEGREGATION IS THE POINT: a row offers exactly one of the two, never
 * both, chosen from its activity status. A running row gets the X; a
 * stopped row gets the trash. Offering both would ask the user to
 * understand a distinction the row's own state already answers.
 *
 * Why a module and not two copies of the markup: before this, the
 * launcher inlined its own X SVG with an aria-label and no title (no
 * hover tooltip at all) while the sidebar drew a trash can labelled
 * "Delete session" for the identical operation. Same click, two glyphs,
 * two names, one of them silent on hover. One builder ends that class of
 * drift for good.
 *
 * Load order: AFTER session-status-ui.js (uses its glyphs), BEFORE
 * launchpad.js and session-sidebar.js (both call into this).
 */

console.log('[SessionRowActions Module] Loading...');

(function () {
    /**
     * Action identifiers. Carried in the DOM on ``data-session-action``
     * so a delegated click handler learns which action it is running
     * without re-deriving it from status (the row was painted from a
     * status that may since have changed under a poll tick).
     * @type {string}
     */
    const ACTION_CLOSE = 'close';
    const ACTION_REMOVE = 'remove';
    /**
     * Restart the agent inside a session whose process has exited. The
     * ONLY non-destructive action in this module: it starts a process in
     * a pane that is already sitting empty, and destroys nothing. See
     * ``requiresConfirm``.
     * @type {string}
     */
    const ACTION_RESTART = 'restart';

    /**
     * Activity statuses that mean "this session is not running any more".
     * Sourced from the vocabulary in client/js/session-status-ui.js
     * (mirroring src/core/session_status.py). Only ``dead`` qualifies:
     * the pane's foreground process exited and tmux is holding the
     * corpse open under remain-on-exit.
     *
     * ``unknown`` is deliberately NOT here. Unknown means we could not
     * determine the state, and guessing "stopped" would offer a control
     * that silently discards a live session's entry.
     * @type {Array<string>}
     */
    const STOPPED_STATUSES = ['dead'];

    /**
     * Hover tooltip + accessible name per action. Identical text goes to
     * both `title` (pointer) and `aria-label` (assistive tech) so the two
     * can never drift, and lowercase to match the app's UI voice.
     * @type {Object<string, string>}
     */
    const ACTION_LABELS = {
        [ACTION_CLOSE]: 'close session',
        [ACTION_REMOVE]: 'remove from the list',
        [ACTION_RESTART]: 'restart the agent',
    };

    /**
     * DOM contract shared by every call site. Exported, and every call
     * site reads them from here rather than writing the attribute name
     * out again, so the markup builder and the click handlers cannot
     * disagree about it.
     *
     * That includes the module-missing case: a call site must NOT fall
     * back to a hardcoded selector, because `html()` returns '' when this
     * module has not loaded, so no element carrying these attributes can
     * exist for the fallback to match. Such a fallback is dead code that
     * also re-introduces the literal this constant exists to remove.
     * @type {string}
     */
    const ATTR_ACTION = 'data-session-action';
    const ATTR_NAME = 'data-session-name';
    const BASE_CLASS = 'session-row-action';

    /**
     * Confirm-modal copy per action.
     *
     * Accuracy is the whole point of this table. A dialog that misstates
     * consequences is worse than no dialog, because it teaches the user
     * that dialogs in this app can be clicked through. That cuts both
     * ways: do not overstate ("everything is deleted"), and do not
     * understate ("nothing on disk is touched").
     *
     * WHAT EACH ACTION ACTUALLY DOES, on every path it can take. Both
     * actions reach the server through the same two calls; which one runs
     * is decided by whether a session id resolves for the row, NOT by
     * which glyph was clicked:
     *
     *   path A - a session id resolves (row carries data-session-id, or
     *     GET /sessions/list matches the tmux name) -> DELETE /sessions ->
     *     SessionManager.destroy_session(). Stops the idle watcher, stops
     *     the backend (kills tmux), **`shutil.rmtree`s
     *     `<working_dir>/.cloude_uploads`**, and may unlink
     *     session_metadata.json. THIS PATH TOUCHES DISK.
     *   path B - no session id resolves -> DELETE /sessions/external/{name}
     *     -> a bare `tmux kill-session` plus dropping the ownership
     *     record. Touches no user files.
     *
     * The sidebar's own-tab row additionally routes through
     * TerminalController.destroySession(), which is always path A.
     *
     * So REMOVE can and does delete files from the project folder: the
     * uploads bucket is real user content, not app scratch. Both dialogs
     * therefore name it. On path B there is no tracked session and so no
     * bucket for CloudeCode to remove, which makes the sentence vacuous
     * rather than false; stating it unconditionally keeps the copy short
     * and errs toward warning instead of toward surprise.
     *
     * The transcript JSONL is never touched on ANY path, which is why
     * both dialogs can promise that unconditionally.
     * @type {Object<string, {title: string, primaryLabel: string, details: string}>}
     */
    const CONFIRM_COPY = {
        [ACTION_CLOSE]: {
            title: 'close session',
            primaryLabel: 'close',
            details:
                'this cannot be undone. the running process is terminated, ' +
                'and files uploaded to this session are deleted from the ' +
                "project's .cloude_uploads folder. the transcript is not " +
                'deleted and stays under ~/.claude/projects.',
        },
        [ACTION_REMOVE]: {
            title: 'remove session',
            primaryLabel: 'remove',
            details:
                'this cannot be undone. this session already exited, so no ' +
                'running process is stopped, but files uploaded to it are ' +
                "deleted from the project's .cloude_uploads folder. the " +
                'leftover tmux shell is cleared and cloudecode forgets the ' +
                'entry. the transcript is not deleted and stays under ' +
                '~/.claude/projects.',
        },
    };

    /**
     * Escape a value for interpolation into an HTML attribute.
     *
     * Description: session names come from tmux and from user input, so
     *   they are never trusted into markup. Uses the DOM's own escaping
     *   for text plus an explicit quote replacement, since textContent
     *   escaping alone does not cover the attribute-delimiter case.
     * Inputs:
     *   value (string|null|undefined) - raw value.
     * Output:
     *   string - safe to place inside a double-quoted HTML attribute.
     * Example:
     *   escapeAttr('a"b<c') -> 'a&quot;b&lt;c'
     */
    function escapeAttr(value) {
        const div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML.replace(/"/g, '&quot;');
    }

    /**
     * Decide which action a row with this status is allowed to offer.
     *
     * Inputs:
     *   status (string|null|undefined) - raw activity_status/status value
     *     from the API payload.
     * Output:
     *   string - ACTION_CLOSE or ACTION_REMOVE.
     * Example:
     *   actionFor('working') -> 'close'
     *   actionFor('dead')    -> 'remove'
     *   actionFor(undefined) -> 'close'   // unknown is never assumed dead
     */
    function actionsFor(status) {
        const key = window.SessionStatusUI
            ? window.SessionStatusUI.normalizeStatus(status)
            : 'unknown';
        if (STOPPED_STATUSES.indexOf(key) !== -1) {
            // Restart first: it is what the user came for, and putting the
            // destructive control under the cursor's first stop would be a
            // poor trade on a row whose whole problem is that it looks
            // finished.
            return [ACTION_RESTART, ACTION_REMOVE];
        }
        return [ACTION_CLOSE];
    }

    /**
     * Back-compat single-action accessor.
     *
     * Description: kept so no existing call site has to change in the
     *   same edit that adds restart. Returns the row's PRIMARY action,
     *   which for a dead row is now restart rather than remove - a dead
     *   row's primary intent is to get the agent back, not to discard it.
     *   New code should call ``actionsFor`` and render all of them.
     * Inputs:
     *   status (string|null|undefined) - raw activity_status value.
     * Output:
     *   string - one action id.
     * Example:
     *   actionFor('working') -> 'close'
     *   actionFor('dead')    -> 'restart'
     */
    function actionFor(status) {
        return actionsFor(status)[0];
    }

    /**
     * Whether an action must be confirmed before it runs.
     *
     * Description: the policy, stated once, in the module that owns the
     *   controls. Close and remove both destroy something the user cannot
     *   get back (a running process, an uploads bucket), so both confirm.
     *
     *   RESTART DOES NOT, and that is a decision rather than an omission.
     *   It kills nothing, deletes nothing, and writes no database row; it
     *   puts a process into a pane that is already empty, and the close
     *   button sitting beside it undoes the result. A dialog here would
     *   put a second click back into precisely the flow the user reported
     *   as broken - and a confirmation that guards a harmless action is
     *   how users learn to click through the ones that matter.
     * Inputs:
     *   action (string) - an ACTION_* id.
     * Output:
     *   boolean - true when a confirm modal must be shown first.
     * Example:
     *   requiresConfirm('restart') -> false
     *   requiresConfirm('remove')  -> true
     */
    function requiresConfirm(action) {
        return action !== ACTION_RESTART;
    }

    /**
     * Human-readable name of an action, for tooltip and accessible name.
     *
     * Inputs:
     *   action (string) - ACTION_CLOSE or ACTION_REMOVE.
     * Output:
     *   string - lowercase label.
     * Example:
     *   labelFor('remove') -> 'remove from the list'
     */
    function labelFor(action) {
        return ACTION_LABELS[action] || ACTION_LABELS[ACTION_CLOSE];
    }

    /**
     * Glyph for an action, from the shared icon family in
     * client/js/session-status-ui.js. Never inline SVG at a call site.
     *
     * Inputs:
     *   action (string) - ACTION_CLOSE or ACTION_REMOVE.
     * Output:
     *   string - a self-contained 16x16 `<svg>`, or '' if the icon module
     *     has not loaded (the button still renders with its label).
     * Example:
     *   iconFor('close') -> '<svg width="16" ...>...</svg>'
     */
    function iconFor(action) {
        if (!window.SessionStatusUI) return '';
        if (action === ACTION_REMOVE) return window.SessionStatusUI.trashIconSvg();
        if (action === ACTION_RESTART) {
            return window.SessionStatusUI.restartIconSvg();
        }
        return window.SessionStatusUI.closeIconSvg();
    }

    /**
     * Build the row's action control.
     *
     * Description: emits a real `<button>` (not a `role="button"` span)
     *   so Enter/Space and focus work without per-surface key handling.
     *   The caller supplies its own CSS class for surface-specific
     *   sizing; BASE_CLASS and the data attributes are the shared
     *   behavioral contract every call site's delegated handler matches
     *   on.
     * Inputs:
     *   status (string|null|undefined) - row activity status; picks the
     *     action.
     *   tmuxName (string) - literal tmux session name, carried back to
     *     the handler on ATTR_NAME.
     *   surfaceClass (string) - extra CSS class for the host surface
     *     (e.g. 'running-session-kill', 'session-sidebar-row-delete').
     * Output:
     *   string - HTML for one `<button>` element.
     * Example:
     *   html('dead', 'cloude_api', 'session-sidebar-row-delete')
     *     -> '<button type="button" class="session-row-action ..." ...>'
     */
    function html(status, tmuxName, surfaceClass) {
        const safeName = escapeAttr(tmuxName);
        const cls = surfaceClass ? `${BASE_CLASS} ${surfaceClass}` : BASE_CLASS;
        return actionsFor(status)
            .map(function (action) {
                const label = labelFor(action);
                const extra =
                    action === ACTION_RESTART ? ` ${BASE_CLASS}-restart` : '';
                return (
                    `<button type="button" class="${cls}${extra}" ` +
                    `${ATTR_ACTION}="${action}" ${ATTR_NAME}="${safeName}" ` +
                    `title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}">` +
                    `${iconFor(action)}</button>`
                );
            })
            .join('');
    }

    /**
     * Ask the user to confirm an action, naming the session.
     *
     * Description: routes through `App.showConfirmModal()`, the app's ONE
     *   confirmation implementation - this module adds copy, never a
     *   second modal. The modal escapes its own arguments, so the display
     *   name is passed raw here.
     *
     *   No archive check is performed. This app has no notion of the
     *   user's conversation archive: nothing in the client, the API, or
     *   src/core knows whether a transcript has been archived, and there
     *   is no endpoint that could answer it. Asserting "not archived" in
     *   this modal would be an invented fact, so the copy stays silent on
     *   it and states only what is verifiable against the server code.
     * Inputs:
     *   action (string) - ACTION_CLOSE or ACTION_REMOVE.
     *   displayName (string) - session name as shown in the row.
     * Output:
     *   Promise<boolean> - true only on an explicit confirm click.
     * Example:
     *   await confirm('close', 'api-work') -> true
     */
    function confirm(action, displayName, context) {
        const copy = CONFIRM_COPY[action] || CONFIRM_COPY[ACTION_CLOSE];
        const verb = action === ACTION_REMOVE ? 'remove' : 'close';
        return window.App.showConfirmModal(
            copy.title,
            `${verb} "${displayName}"?`,
            attachmentPreamble(context) + copy.details,
            copy.primaryLabel,
            'cancel'
        );
    }

    /**
     * Sentences naming what is CURRENTLY ATTACHED to the target session.
     *
     * Description: a confirmation that only describes the operation in
     *   the abstract lets the user destroy something they are looking at
     *   without being told. These two facts are the ones a list-based
     *   surface (the status panel) cannot convey from the row alone, so
     *   they lead the details rather than trail them. Both are omitted
     *   when absent rather than stated in the negative, so a plain row
     *   keeps the short copy it has always had - this cannot change the
     *   text of any existing call site that passes no context.
     * Inputs:
     *   context (object|null|undefined) - optional
     *     `{openInApp: boolean, attachedClients: number}`.
     * Output:
     *   string - zero, one or two sentences, each ending in a space.
     * Example:
     *   attachmentPreamble({openInApp: true, attachedClients: 1})
     *     -> 'this session is open in cloudecode right now, and that '
     *      + 'terminal will disconnect. 1 tmux client is attached to it '
     *      + 'right now and will be detached. '
     */
    function attachmentPreamble(context) {
        if (!context) return '';
        let out = '';
        if (context.openInApp) {
            out += 'this session is open in cloudecode right now, and that '
                + 'terminal will disconnect. ';
        }
        const attached = Number(context.attachedClients) || 0;
        if (attached > 0) {
            out += attached === 1
                ? '1 tmux client is attached to it right now and will be detached. '
                : `${attached} tmux clients are attached to it right now and `
                  + 'will be detached. ';
        }
        return out;
    }

    /**
     * Run the destructive action against the server.
     *
     * Description: THE one place the two destruction endpoints are chosen
     *   between, so a new surface cannot invent a third way to kill a
     *   tmux session. The branch is on whether a session id resolved, NOT
     *   on which glyph was clicked - see the CONFIRM_COPY docblock for
     *   what each path actually destroys.
     *
     *   The launcher (`launchpad.js`) and the sidebar
     *   (`session-sidebar.js`) still inline this same two-line branch.
     *   They should call this instead; that is deliberately not done in
     *   this change because both files are being edited on another branch
     *   right now, and a merge conflict in a destruction path is a worse
     *   outcome than a duplicated `if`.
     * Inputs:
     *   tmuxName (string) - literal tmux session name.
     *   sessionId (string|null) - id of the live backend bound to that
     *     name, when there is one.
     * Output:
     *   Promise<object> - the server's SuccessResponse.
     * Example:
     *   await perform('cloude_api', null)  // DELETE /sessions/external/...
     */
    function perform(tmuxName, sessionId) {
        if (sessionId) return window.API.destroySession(sessionId);
        return window.API.destroyExternalSession(tmuxName);
    }

    window.SessionRowActions = {
        ACTION_CLOSE,
        ACTION_REMOVE,
        ACTION_RESTART,
        ATTR_ACTION,
        ATTR_NAME,
        BASE_CLASS,
        actionFor,
        actionsFor,
        requiresConfirm,
        labelFor,
        iconFor,
        html,
        confirm,
        attachmentPreamble,
        perform,
    };
    console.log('[SessionRowActions Module] Exported as window.SessionRowActions');
})();
