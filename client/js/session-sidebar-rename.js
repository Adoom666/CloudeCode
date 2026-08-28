/**
 * Session sidebar INLINE RENAME - "double click to inline edit session
 * name in sidebar."
 *
 * THE HARD PART IS NOT THE EDITOR, IT IS THE CLICK. A row's single click
 * already means "switch to this conversation", and a browser delivers the
 * first click of a double-click BEFORE it delivers the dblclick - so the
 * naive version navigates away from the row a moment before the user
 * finishes asking to edit it. The fix is a deferral, and the whole design
 * is about keeping that deferral as small as it can possibly be:
 *
 *   - it applies ONLY to a click that landed on the row's NAME. Anywhere
 *     else on the row - the dot, the badge, the padding - switches
 *     instantly, exactly as before.
 *   - it applies ONLY to a row whose `data-rename-state` is
 *     'renameable'. A row with nothing to edit has nothing to wait for.
 *   - it is DBLCLICK_MS, and the operation it delays is a network round
 *     trip plus a terminal attach.
 * So the cost is bounded to one target on one class of row, and it buys
 * the gesture the user asked for. Anything wider than that would be
 * paying for this feature on every click in the list.
 *
 * THREE RENAMEABILITY STATES, AND THE EDITOR RESPECTS ALL THREE. The
 * classifier is `SessionSidebarRows.renameState()`, shared with the row
 * markup so the state the row DRAWS and the state the editor GATES ON
 * cannot disagree. Only 'renameable' opens an editor. 'unavailable' and
 * 'unknown' say why, in the live region and in the row's own title, and
 * open nothing - because an edit box that accepts text and then fails is
 * worse than no edit box: it takes the user's input and throws it away.
 * 'unknown' in particular is not folded into 'unavailable'; we do not
 * know that the rename would fail, we know that we cannot tell, and those
 * are different sentences.
 *
 * A FAILED RENAME RESTORES THE OLD NAME. The row is never left showing a
 * value the server rejected. The optimistic path here is deliberately
 * NOT optimistic: nothing is written into the row until the PATCH
 * resolves, and a rejection puts the editor back with the text still in
 * it and the reason underneath, so the user can fix it rather than retype
 * it.
 *
 * KEYBOARD PARITY: F2 on a focused row opens the same editor through the
 * same gate. F2 rather than Enter because Enter already activates the row
 * (client/js/session-sidebar-reorder.js), and rather than a bare letter
 * because `p` is already spent on pinning.
 *
 * Must load AFTER session-sidebar-rows.js and BEFORE session-sidebar.js.
 */

console.log('[SessionSidebarRename Module] Loading...');

(function () {
    /**
     * How long a click on a renameable row's NAME waits for a second
     * click before it commits to switching conversation. The platform
     * double-click threshold is around 400-500ms; this is deliberately
     * shorter, because the cost of being too short is one extra click and
     * the cost of being too long is a laggy list.
     * @type {number}
     */
    const DBLCLICK_MS = 250;

    /**
     * Description: the label rule, from the one module that holds it.
     *   Kept as a function rather than a constant so this module has no
     *   copy of the rule at all - see client/js/session-label.js.
     * Inputs: raw (string|null). Output: object - {ok, value} | {ok, reason}.
     */
    function validateLabel(raw) {
        if (window.SessionLabel && window.SessionLabel.validate) {
            return window.SessionLabel.validate(raw);
        }
        // The resolver is a hard dependency of this module (it also
        // supplies the editor's seed). If it is missing, say so rather
        // than falling back to a second copy of the rule - a fallback
        // rule is how the two drift.
        return { ok: false, reason: 'the label rule is unavailable' };
    }

    /** The in-flight deferred activation timer, or null. */
    let pending = null;

    /** The name of the row currently being edited, or null. */
    let editing = null;

    /**
     * Description: the sidebar's list container, or null before wiring.
     * Inputs: none. Output: Element|null.
     */
    function listEl() {
        const sidebar = window.SessionSidebar;
        return (sidebar && sidebar.listEl) || null;
    }

    /**
     * Description: say something in the panel's live region, which is the
     *   only channel a refusal has when it draws nothing.
     * Inputs: text (string). Output: void.
     */
    function announce(text) {
        const region = document.getElementById('session-sidebar-live');
        if (region) region.textContent = text;
    }

    /**
     * Description: true when a rename editor is currently open.
     * Inputs: none. Output: boolean.
     */
    function isEditing() { return editing !== null; }

    /**
     * Description: cancel a deferred row activation, if one is waiting.
     * Inputs: none. Output: boolean - true when one was actually cancelled.
     */
    function clearPending() {
        if (!pending) return false;
        clearTimeout(pending);
        pending = null;
        return true;
    }

    /**
     * Description: hold a click on a renameable row's NAME for
     *   DBLCLICK_MS so a double-click can claim it instead. See the file
     *   docblock for why the deferral is scoped this narrowly.
     * Inputs: e (MouseEvent) - the click. rowEl (Element) - the row it
     *   landed in. activate (function) - what to run if no second click
     *   arrives.
     * Output: boolean - true when the click was taken over, in which case
     *   the caller must not activate the row itself.
     */
    function deferActivation(e, rowEl, activate) {
        if (isEditing()) return true;
        const nameEl = e.target.closest && e.target.closest('[data-row-name]');
        if (!nameEl || !rowEl) return false;
        if (rowEl.getAttribute('data-rename-state') !== 'renameable') return false;
        // A second click of a double-click carries detail >= 2. Swallow it
        // rather than starting a second timer - the dblclick handler is
        // about to fire and it owns the gesture from here.
        if (e.detail && e.detail > 1) { clearPending(); return true; }
        clearPending();
        pending = setTimeout(() => {
            pending = null;
            activate();
        }, DBLCLICK_MS);
        return true;
    }

    /**
     * Description: claim any click that lands inside an open editor, so
     *   putting the caret in the input does not also switch conversation.
     * Inputs: e (MouseEvent). Output: boolean - true when handled.
     */
    function onListClick(e) {
        if (!isEditing()) return false;
        const inside = e.target.closest && e.target.closest('.session-sidebar-rename-edit');
        if (!inside) return false;
        e.stopPropagation();
        return true;
    }

    /**
     * Description: open the editor on a double-clicked name, or say why
     *   it cannot be opened. Also suppresses the browser's own
     *   double-click text selection on the name, which would otherwise
     *   leave the row's text highlighted underneath the input.
     * Inputs: e (MouseEvent). Output: void.
     */
    function onDblClick(e) {
        const nameEl = e.target.closest && e.target.closest('[data-row-name]');
        if (!nameEl) return;
        const rowEl = nameEl.closest('.session-sidebar-row');
        if (!rowEl) return;
        e.preventDefault();
        e.stopPropagation();
        clearPending();
        beginEdit(rowEl);
    }

    /**
     * Description: keyboard entry to the same editor. Returns a verdict so
     *   the reorder module's key handler knows whether it consumed the
     *   event.
     * Inputs: e (KeyboardEvent), rowEl (Element).
     * Output: boolean - true when handled.
     */
    function onRowKeydown(e, rowEl) {
        if (e.key !== 'F2') return false;
        e.preventDefault();
        beginEdit(rowEl);
        return true;
    }

    /**
     * Description: open the inline editor on one row, or refuse and say
     *   which of the three states refused it. This is the ONLY entry to
     *   the editor - both the pointer and the keyboard path come through
     *   here, so the gate cannot be bypassed by one of them.
     * Inputs: rowEl (Element) - a `.session-sidebar-row`.
     * Output: boolean - true when an editor was opened.
     */
    function beginEdit(rowEl) {
        if (isEditing()) return false;
        const state = rowEl.getAttribute('data-rename-state');
        const name = rowEl.dataset.name;
        const sessionId = rowEl.dataset.sessionId || null;
        if (state !== 'renameable' || !sessionId || !name) {
            const nameEl = rowEl.querySelector('[data-row-name]');
            const why = (nameEl && nameEl.getAttribute('title'))
                || 'this session cannot be renamed';
            announce(why);
            rowEl.setAttribute('data-rename-refused', '1');
            return false;
        }
        const nameEl = rowEl.querySelector('[data-row-name]');
        if (!nameEl) return false;
        editing = name;
        rowEl.setAttribute('data-editing', '1');

        // THE SEED IS THE RENDERED TEXT, and taking it from the DOM
        // rather than re-deriving it is the point. This control edits the
        // LABEL; it used to seed itself with `rowEl.dataset.name`, the
        // tmux handle, so a user who had named a session "Media
        // Compression" opened the editor on "cloude_Media" and, on
        // Enter, overwrote their own label with a handle-derived string.
        // That is data loss, and it is silent.
        //
        // Reading `nameEl.textContent` means the seed IS what the user
        // was looking at, by construction rather than by two functions
        // agreeing. If the row's display half ever regresses to the
        // handle again, the seed regresses with it and the same test
        // catches both, instead of the display looking right while the
        // editor quietly hands back something else.
        //
        // Read BEFORE the span is hidden - `hidden` is a display:none,
        // and textContent is unaffected by it, but the ordering is worth
        // not depending on.
        const seed = (nameEl.textContent || '').trim();
        nameEl.hidden = true;

        const wrap = document.createElement('span');
        wrap.className = 'session-sidebar-rename-edit';
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'session-sidebar-rename-input';
        input.value = seed;
        input.setAttribute('aria-label', `Rename ${seed}`);
        // The LABEL's limit, from the one module that mirrors the server.
        // It used to be a hardcoded 64 - the old tmux-name limit - which
        // silently truncated any label past that point at the maxlength,
        // with no error, because a maxlength does not report anything.
        input.setAttribute(
            'maxlength',
            String((window.SessionLabel && window.SessionLabel.LABEL_MAX_CHARS) || 200)
        );
        const err = document.createElement('span');
        err.className = 'session-sidebar-rename-error';
        err.hidden = true;
        err.setAttribute('role', 'alert');
        wrap.appendChild(input);
        wrap.appendChild(err);
        nameEl.insertAdjacentElement('afterend', wrap);

        const ctx = {
            rowEl, nameEl, wrap, input, err, name, seed, sessionId, settled: false,
        };
        input.addEventListener('keydown', (e) => onEditKeydown(e, ctx));
        // BLUR COMMITS, which is what the user asked for, but a blur
        // caused by the editor tearing itself down must not re-enter
        // save() - hence the settled flag rather than removing the
        // listener, which would race with the teardown that removes the
        // element it is bound to.
        input.addEventListener('blur', () => { commit(ctx); });
        input.focus();
        input.select();
        announce(`editing the name of ${seed}. Enter to save, Escape to cancel.`);
        return true;
    }

    /**
     * Description: Enter commits, Escape cancels. Every other key is left
     *   to the input, and the event is stopped either way so the row's own
     *   arrow/pin/activate handling never sees a keystroke meant for the
     *   text box.
     * Inputs: e (KeyboardEvent), ctx (object) - the editor context.
     * Output: void.
     */
    function onEditKeydown(e, ctx) {
        e.stopPropagation();
        if (e.key === 'Enter') {
            e.preventDefault();
            commit(ctx);
            return;
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            cancel(ctx);
        }
    }

    /**
     * Description: tear the editor down and put the row's name back
     *   exactly as it was. Used by cancel, by a successful commit, and by
     *   a failure that has run out of things to offer - in all three
     *   cases the row ends up showing a name the server agrees with.
     * Inputs: ctx (object). Output: void.
     */
    function teardown(ctx) {
        ctx.settled = true;
        editing = null;
        try { if (ctx.wrap.parentNode) ctx.wrap.parentNode.removeChild(ctx.wrap); } catch (_) { /* gone */ }
        ctx.nameEl.hidden = false;
        ctx.rowEl.removeAttribute('data-editing');
    }

    /**
     * Description: abandon the edit, restoring the previous name.
     * Inputs: ctx (object). Output: void.
     */
    function cancel(ctx) {
        if (ctx.settled) return;
        teardown(ctx);
        // The user is told about the thing they were LOOKING AT. A screen
        // reader hearing the tmux handle here would be told a name that
        // appears nowhere on the row.
        announce(`rename cancelled, ${ctx.seed} unchanged`);
    }

    /**
     * Description: validate and send the edit. An unchanged or empty value
     *   is a cancel, not a rename. A locally invalid name never leaves the
     *   browser and keeps the editor open with the text intact, so the
     *   user can correct it rather than retype it.
     *
     *   ON FAILURE THE ROW GOES BACK TO THE OLD NAME. Nothing is written
     *   into the row before the server answers, so there is no optimistic
     *   value to roll back - the row is showing the old name the whole
     *   time, and the failure leaves it there and says what happened.
     * Inputs: ctx (object). Output: Promise<void>.
     */
    async function commit(ctx) {
        if (ctx.settled) return;
        const raw = (ctx.input.value || '').trim();
        // UNCHANGED IS A NO-OP, AND THE BASIS IS THE SEED. It used to be
        // `ctx.name`, the tmux handle, which made the comparison ask a
        // question nobody was answering: the user was editing a label, so
        // their text almost never equalled the handle, so the guard let
        // an unchanged edit through as a write.
        //
        // Comparing against the SEED is what makes this control safe for
        // a session with NO label yet. That session is seeded with the
        // `cloude_`-stripped handle, because an empty box for a row that
        // visibly has a name is worse to use and no safer. Seeding it
        // that way on its own WOULD be a trap - press Enter unchanged and
        // a handle-derived string is promoted into a real stored label,
        // freezing a value the user never typed and never asked for. The
        // guard is what closes it: unchanged writes nothing, so the only
        // way a handle becomes a label is a user deliberately editing it
        // into one, which is indistinguishable from typing it.
        //
        // And for a session that DOES have a label, the seed is that
        // label, so this is also what makes "open the editor and change
        // your mind" leave the label exactly where it was.
        if (!raw || raw === ctx.seed) { cancel(ctx); return; }
        const verdict = validateLabel(raw);
        if (!verdict.ok) {
            showError(ctx, verdict.reason);
            return;
        }
        ctx.settled = true;
        try {
            await window.API.renameSession(ctx.sessionId, verdict.value);
        } catch (err) {
            // Re-open rather than close: the text the user typed is still
            // the best starting point for their next attempt.
            ctx.settled = false;
            const detail = (err && (err.message || err.detail)) || 'the server refused it';
            showError(ctx, `rename failed: ${detail}`);
            announce(`rename failed, ${ctx.seed} is unchanged: ${detail}`);
            return;
        }
        teardown(ctx);
        announce(`renamed ${ctx.seed} to ${verdict.value}`);
        const sidebar = window.SessionSidebar;
        if (sidebar) {
            sidebar._lastSig = null;
            await sidebar._fetchAndRender();
        }
    }

    /**
     * Description: show a reason under the still-open editor and put the
     *   caret back in it. The message is a real element with role=alert,
     *   not a title attribute, because a failure the user has to act on
     *   must not require a hover to read.
     * Inputs: ctx (object), text (string). Output: void.
     */
    function showError(ctx, text) {
        ctx.err.textContent = text;
        ctx.err.hidden = false;
        ctx.input.focus();
        ctx.input.select();
    }

    /**
     * Description: close any editor left over from a repaint. The list's
     *   innerHTML is rewritten wholesale, so an open editor's elements are
     *   destroyed without any of its handlers firing - without this the
     *   module would believe an edit was still in progress forever and
     *   swallow every subsequent click.
     * Inputs: none. Output: void.
     */
    function afterRender() {
        if (!isEditing()) return;
        const list = listEl();
        if (list && list.querySelector('.session-sidebar-rename-edit')) return;
        editing = null;
    }

    /**
     * Description: nothing to bind - the sidebar controller routes click,
     *   dblclick and keydown into this module. Present so the controller's
     *   init sequence reads the same for every sibling module, and so the
     *   deferral timer is dropped if the panel is re-initialised.
     * Inputs: none. Output: void.
     */
    function init() { clearPending(); }

    window.SessionSidebarRename = {
        init, afterRender, deferActivation, onListClick, onDblClick,
        onRowKeydown, beginEdit, isEditing, clearPending,
        DBLCLICK_MS, validateLabel,
    };
    console.log('[SessionSidebarRename Module] Exported as window.SessionSidebarRename');
})();
