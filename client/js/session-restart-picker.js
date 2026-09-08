/**
 * RESTART, WITH A CHOICE OF WRAPPER - and an honest answer first.
 * ----------------------------------------------------------------------
 * "when resuming/restarting a session can we pick a new wrapper"
 *
 * Moving a session onto another launch wrapper (claude-chrome, say) used
 * to mean hand-editing `sessions.agent_type` in cloude.db. This panel is
 * what replaces that.
 *
 * IT IS A WARNING BEFORE IT IS A PICKER, and that order matters. The
 * respawn ladder gates on tmux's `#{pane_start_command}`: when that field
 * is EMPTY the restart lands on the `shell` rung and hands back a LOGIN
 * SHELL instead of an agent, silently. A picker that let someone
 * confidently choose claude-chrome and then dropped them at a zsh prompt
 * would be worse than no picker at all. So this panel opens by asking
 * `GET /sessions/restart/preview` - a read-only route that spawns
 * nothing - and paints the PREDICTED outcome next to every choice,
 * including the one where nothing is picked.
 *
 * THE THREE ANSWERS ARE THREE ANSWERS. "will start the agent", "will
 * return a plain shell" and "cannot be determined" are rendered
 * differently and the last never renders as the first. A choice whose
 * predicted rung is not actionable cannot be confirmed at all - the
 * button disables and says why.
 *
 * IT IS ALSO THE CONFIRMATION. A restart destroys what is in the pane,
 * so it must be confirmed - but a second modal after this one would ask
 * the user to agree twice to a decision this panel already spelled out
 * in full. The explicit "restart" button here IS the confirmation, and
 * the sentence above it is a better one than a generic dialog could
 * write. It reuses `.modal-overlay` / `.modal-content` so it is the same
 * object on screen as every other dialog in the app.
 *
 * ACTIVITY STATUS INFORMS, IT NEVER REFUSES. `activity_state` is known to
 * read `working` for about four minutes after a resume before it
 * self-corrects, so a hard block on it would refuse a restart the user
 * can plainly see is needed. When the row says the session is busy this
 * panel says so, in words, above the button - and then lets the user
 * decide.
 *
 * A LIVE SESSION GETS AN ANSWER AND STILL GETS NO BUTTON. The server
 * reports two things about every choice: what it would COME BACK AS
 * (`projected_kind`, which ignores liveness) and whether a restart may
 * act on it right now (`actionable_now`, which does not). The first is
 * what the badge and the sentence say, because "this one would come back
 * a plain shell" is useful about a session that is still running and is
 * the only useful thing there is to say about one. The second, and only
 * the second, enables the radio. A PREDICTION IS NEVER A PERMISSION.
 *
 * REPLACING WHAT IS RUNNING IS A SECOND, DELIBERATE ACT. On a pane that
 * is ALIVE this panel now offers one, and it is deliberately awkward to
 * reach: an unchecked box at the top of the list arms the choices, and
 * the button then routes through the app's confirm modal, which names
 * what is about to be killed. Nothing arrives armed and nothing in the
 * server's payload can arm it - `armLive` starts false on every render
 * whatever the preview says, so a live restart is always at least two
 * user acts away.
 *
 * THAT IS WHERE THE PREDICTION/PERMISSION LINE IS DRAWN, and it is drawn
 * with a value the server never sends. `projected_kind` is what a live
 * session would come back AS and it drives the badge, the sentence and
 * the warning; it does NOT enable anything. What enables a live choice is
 * `armLive && isActionable(projected_kind)`, and `armLive` is a checkbox
 * state. So there is no field in the response a client could echo into a
 * permission, which is the failure the ladder's docstring warns about.
 *
 * IT IS NOT CLOSE-AND-RECREATE. The owner's two calls (2026-09-07) were
 * "same tmux should be fine" and "yes resume the same session", which
 * make this `respawn-pane -k` against the same session name: no new row
 * is minted, so project attribution, pinned theme, unread state, group
 * filing and sidebar position stay put because nothing moves.
 *
 * THE ROWS ARE NOT IN THIS FILE. session-restart-options.js owns the
 * option list, the badge vocabulary and the rule that lifts a
 * session-level sentence out of the per-option loop; this file owns the
 * panel, the promise and the arming gate. The split happened because
 * this file was 578 lines and past the project's 500-line rule, and
 * because "which rows exist" and "what the panel does with a choice" are
 * two jobs. The helpers that moved are re-exported here unchanged, so
 * `SessionRestartPicker.optionsHtml` is still the same one function.
 *
 * Load AFTER api.js, app.js and session-restart-options.js; BEFORE
 * session-sidebar-clicks.js.
 */

console.log('[SessionRestartPicker Module] Loading...');

(function () {
    'use strict';

    /**
     * THE OPTION LIST LIVES IN ITS OWN MODULE. session-restart-options.js
     * owns every row, the badge vocabulary and the rule that lifts a
     * session-level sentence out of the per-option loop. This file kept
     * the panel, the promise and the arming gate, which is what it is
     * for; it re-exports the list's helpers below so the module's public
     * surface is the one it has always had.
     * @type {object}
     */
    var Options = window.SessionRestartOptions;

    /** The open panel element, or null. Only ever one. */
    var overlayEl = null;

    /**
     * Description: HTML-escape for text and attributes. One escaper, in
     *   the options module, used from both.
     * Inputs: value (any).
     * Output: string, safe inside a double-quoted attribute or as text.
     */
    function esc(value) {
        return Options.esc(value);
    }

    /**
     * Description: the sentence shown when the wrapper list itself could
     *   not be read. NOT the same as "no wrappers configured", and it is
     *   spelled out rather than left to an empty list, because an empty
     *   list is what "you have not made any wrappers" also looks like.
     * Inputs: preview (object).
     * Output: string - HTML, or '' when there is nothing to say.
     */
    function noticeHtml(preview) {
        var out = '';
        if (preview.pane_state === 'alive') {
            // SAID BEFORE THE LIST, not discovered by clicking a dead
            // button. The options below show what each choice would come
            // back as either way; this sentence draws the line between
            // "what it would be" and "what it costs to get there".
            out += '<div class="restart-picker__notice">this session is still '
                + 'running. restarting it kills what is in the pane first, so '
                + 'the choices below are locked until you say so.</div>'
                + (window.SessionRestartLive
                    ? window.SessionRestartLive.armHtml()
                    : '');
        } else if (preview.pane_state === 'unknown') {
            out += '<div class="restart-picker__notice">tmux did not answer '
                + 'about this pane, so whether it can be restarted could not '
                + 'be determined. that is not the same as it being fine.</div>';
        }
        return out + wrapperNoticeHtml(preview);
    }

    /**
     * Description: the wrapper-list half of the notices, split out so the
     *   pane-state half above reads as one thought.
     * Inputs: preview (object).
     * Output: string - HTML, or '' when there is nothing to say.
     */
    function wrapperNoticeHtml(preview) {
        if (preview.wrappers_status === 'unavailable') {
            return '<div class="restart-picker__notice">the launch wrapper list '
                + 'could not be read, so no other wrapper can be offered here. '
                + 'this is not the same as having none configured.</div>';
        }
        if (!(preview.options || []).length) {
            return '<div class="restart-picker__notice">no launch wrappers are '
                + 'configured, so there is nothing else to switch to.</div>';
        }
        return '';
    }

    /**
     * Description: the busy line. Informational, never a block - see the
     *   module docblock on why `activity_state` may not be trusted to
     *   refuse.
     * Inputs: status (string|null) - the row's activity status.
     * Output: string - HTML, or '' when there is nothing to warn about.
     */
    function busyHtml(status) {
        var key = window.SessionStatusUI
            ? window.SessionStatusUI.normalizeStatus(status)
            : 'unknown';
        if (key === 'working' || key === 'question' || key === 'notice') {
            return '<div class="restart-picker__notice">this row currently reads '
                + '"' + esc(key) + '". that signal can lag a few minutes behind '
                + 'what the session is really doing, so it is shown rather than '
                + 'acted on.</div>';
        }
        return '';
    }

    /**
     * Description: tear the panel down and settle the promise.
     * Inputs: resolve (function). value (object|null) - the result.
     * Output: void.
     */
    function dismiss(resolve, value) {
        if (overlayEl && overlayEl.parentNode) {
            overlayEl.parentNode.removeChild(overlayEl);
        }
        overlayEl = null;
        resolve(value);
    }

    /**
     * Description: paint the panel and wait for the user to decide.
     * Inputs:
     *   displayName (string) - the session name as the user sees it.
     *   preview (object) - the RestartPreviewResponse body.
     *   status (string|null) - the row's activity status.
     * Output: Promise<{agentType: string|null,
     *   confirmRestartLive: boolean}|null> - null on every dismissal
     *   path (cancel, Escape, backdrop), so a restart can only ever fire
     *   from an explicit click on the restart button.
     *   `confirmRestartLive` is true ONLY when the user ticked the arm
     *   box AND then agreed in the confirm modal. Two acts, and neither
     *   of them is anything the server sent.
     */
    function present(displayName, preview, status) {
        return new Promise(function (resolve) {
            var overlay = document.createElement('div');
            overlay.className = 'modal-overlay restart-picker';
            overlay.innerHTML =
                '<div class="modal-content restart-picker__content" role="dialog" '
                + 'aria-modal="true" aria-label="restart ' + esc(displayName) + '">'
                + '<div class="modal-header">&raquo; restart</div>'
                + '<div class="modal-body">'
                + '<div class="modal-message">restart "' + esc(displayName) + '"</div>'
                + '<div class="modal-description">this stops what is in the pane '
                + 'and starts the choice below in it. the transcript is '
                + 'kept.</div>'
                + noticeHtml(preview)
                + busyHtml(status)
                // WHAT IS TRUE OF THE SESSION, SAID ONCE, DIRECTLY ABOVE
                // THE CHOICES IT CONSTRAINS. Empty unless every row would
                // have printed the same sentence, in which case that
                // sentence was never about any of the rows.
                + Options.sharedDetailHtml(preview)
                + '<div class="restart-picker__options" role="radiogroup" '
                + 'aria-label="what to restart with">'
                + Options.optionsHtml(preview)
                + '</div>'
                + '<div class="restart-picker__why" id="restart-picker-why"></div>'
                + '</div>'
                + '<div class="modal-footer">'
                + '<button type="button" class="modal-btn modal-btn-secondary" '
                + 'id="restart-picker-cancel">cancel</button>'
                + '<button type="button" class="modal-btn modal-btn-primary" '
                + 'id="restart-picker-go">restart</button>'
                + '</div></div>';

            document.body.appendChild(overlay);
            overlayEl = overlay;

            var go = overlay.querySelector('#restart-picker-go');
            var why = overlay.querySelector('#restart-picker-why');
            // Present ONLY on a live pane (noticeHtml renders it there).
            // Absent means there is nothing to arm, and `armed()` then
            // answers false forever, which is the dead-pane behaviour
            // this panel has always had.
            var arm = overlay.querySelector('#restart-picker-live');
            // EVERY ROW REFUSED FOR THE SAME REASON. Read from the same
            // helper that hoisted the sentence above the list, so the
            // panel's explanation and its heading can never contradict
            // each other about whether any choice is left.
            var allGone = Options.sharedDetail(preview).kind
                === 'transcript_missing';

            /**
             * Description: has the user armed a live restart? Reads the
             *   CHECKBOX, never the payload. There is deliberately no
             *   path from the server's response to this value.
             * Inputs: none. Output: boolean.
             */
            function armed() {
                return !!(arm && arm.checked);
            }

            /**
             * Description: keep the button and its explanation in step
             *   with the selected radio. The button is DISABLED whenever
             *   the predicted rung is not actionable, so a user cannot
             *   commit to an outcome the server has already said it will
             *   refuse.
             * Inputs: none. Output: void.
             */
            function sync() {
                var picked = overlay.querySelector(
                    'input[name="restart-picker-choice"]:checked');
                // THE BUTTON FOLLOWS THE RADIO'S OWN DISABLED STATE, which
                // the server's `actionable_now` set. It deliberately does
                // NOT follow `data-kind`, because that carries the
                // PROJECTED rung - a live session's options project
                // 'agent' and must still not be restartable. Reading the
                // prediction here would hand out exactly the permission
                // the server withheld.
                var live = armed();
                // RE-DERIVE EVERY RADIO'S DISABLED STATE FROM TWO FACTS,
                // one of which the server sent and one of which only the
                // user can set. `data-actionable-now` is the server's
                // permission for a DEAD pane; `data-live-eligible` is
                // only ever consulted alongside `live`, so the projected
                // rung can never enable anything on its own.
                var radios = overlay.querySelectorAll(
                    'input[name="restart-picker-choice"]');
                Array.prototype.forEach.call(radios, function (r) {
                    var now = r.getAttribute('data-actionable-now') === '1';
                    var eligible = r.getAttribute('data-live-eligible') === '1';
                    r.disabled = !(now || (live && eligible));
                    var label = r.parentNode;
                    if (label && label.classList) {
                        label.classList.toggle('is-unavailable', r.disabled);
                    }
                });
                var ok = !!picked && !picked.disabled;
                go.disabled = !ok;
                // THE BUTTON SAYS WHAT IT DOES. A live restart destroys
                // a running process, so it must not wear the same word as
                // reviving an empty pane.
                go.textContent = live ? 'kill and restart' : 'restart';
                var nowKind = picked
                    ? picked.getAttribute('data-now-kind')
                    : '';
                var projected = picked ? picked.getAttribute('data-kind') : '';
                if (ok) {
                    if (projected === 'shell') {
                        why.textContent = 'this will not start an agent. it '
                            + 'opens a plain shell.';
                    } else if (live) {
                        why.textContent = 'this kills what is running in the '
                            + 'pane first. you will be asked to confirm.';
                    } else {
                        why.textContent = '';
                    }
                    return;
                }
                if (nowKind === 'not_dead') {
                    why.textContent = 'this session is still running. tick the '
                        + 'box above to kill what is in the pane and restart it '
                        + 'in place, or leave it alone.';
                    return;
                }
                if (nowKind === 'transcript_missing'
                    || projected === 'transcript_missing') {
                    // NOT a could-not-determine. The transcript this would
                    // resume was looked for and is not there, so restarting
                    // would open a pane that exits at once - the exact
                    // false green this app keeps paying for.
                    //
                    // AND THE ADVICE HAS TO CHECK ITSELF. Telling the user
                    // to pick a wrapper instead was true while a chosen
                    // wrapper's command carried no --resume. It no longer
                    // does: the preview resolves every offer with the
                    // session's resume args, so when the conversation is
                    // gone EVERY wrapper is refused too and sending the
                    // user round the list is advice that cannot work.
                    why.textContent = 'the conversation this would resume is '
                        + 'not on this machine, so restarting it would open a '
                        + 'pane that exits straight away.'
                        + (allGone
                            ? ' every choice here resumes that same '
                                + 'conversation, so none of them can start.'
                            : ' pick a wrapper instead to start a fresh one.');
                    return;
                }
                why.textContent = 'what this would run cannot be determined, so '
                    + 'it will not be started.';
            }

            overlay.addEventListener('change', sync);
            sync();

            overlay.addEventListener('keydown', function (e) {
                if (e.key === 'Escape') {
                    e.preventDefault();
                    dismiss(resolve, null);
                }
            });
            overlay.addEventListener('click', function (e) {
                if (e.target === overlay) dismiss(resolve, null);
            });
            overlay.querySelector('#restart-picker-cancel')
                .addEventListener('click', function () {
                    dismiss(resolve, null);
                });
            go.addEventListener('click', function () {
                if (go.disabled) return;
                var picked = overlay.querySelector(
                    'input[name="restart-picker-choice"]:checked');
                var value = picked ? picked.value : '';
                if (!armed()) {
                    dismiss(resolve, {
                        agentType: value || null,
                        confirmRestartLive: false,
                    });
                    return;
                }
                // THE SECOND ACT. The checkbox armed the choices; this
                // names what is about to be killed, and the flag that
                // reaches the server is produced HERE and nowhere else.
                // A cancel leaves the panel standing so the tick can be
                // undone rather than the whole decision restarted.
                var copy = window.SessionRestartLive.liveConfirmCopy(
                    preview, value || null, status, displayName);
                var ask = window.App && window.App.showConfirmModal
                    ? window.App.showConfirmModal(copy.title, copy.message,
                        copy.details, copy.primaryLabel, 'cancel')
                    : Promise.resolve(false);
                Promise.resolve(ask).then(function (agreed) {
                    if (!agreed) return;
                    dismiss(resolve, {
                        agentType: value || null,
                        confirmRestartLive: true,
                    });
                });
            });

            var first = overlay.querySelector(
                'input[name="restart-picker-choice"]:checked');
            if (first && typeof first.focus === 'function') first.focus();
        });
    }

    /**
     * Description: THE entry point. Fetches the read-only preview, shows
     *   the panel, and reports what the user chose.
     *
     *   A PREVIEW THAT FAILS DOES NOT SILENTLY BECOME A RESTART. When the
     *   route cannot be reached there is no honest prediction to show, so
     *   this returns null and the caller reports that it could not find
     *   out what a restart would do. Proceeding blind is precisely the
     *   behaviour that made the shell rung dangerous.
     * Inputs:
     *   tmuxName (string) - literal tmux session name.
     *   displayName (string) - what the row calls it.
     *   status (string|null) - the row's activity status, shown not acted on.
     * Output: Promise<{agentType: string|null,
     *   confirmRestartLive: boolean}|null> - null when the user
     *   cancelled OR the preview could not be fetched. `error` is set on
     *   the returned object shape only in the latter case, via
     *   `lastError()`. Callers MUST forward `confirmRestartLive` to
     *   `API.respawnSession`; dropping it turns a live restart into a
     *   silent no-op the server answers `not_dead`.
     */
    var lastErrorText = '';

    function open(tmuxName, displayName, status) {
        lastErrorText = '';
        return window.API.restartPreview(tmuxName).then(function (preview) {
            return present(displayName || tmuxName, preview, status);
        }).catch(function (err) {
            lastErrorText = (err && err.message) || String(err);
            console.error('SessionRestartPicker: preview failed:', err);
            return null;
        });
    }

    /**
     * Description: why the last `open()` returned null without asking the
     *   user anything, or '' when it returned null because they
     *   cancelled. Lets the caller tell a cancel apart from a failure
     *   without a second return channel.
     * Inputs: none. Output: string.
     */
    function lastError() {
        return lastErrorText;
    }

    // RE-EXPORTED, NOT RE-IMPLEMENTED. These three moved to
    // session-restart-options.js; they are surfaced here unchanged so
    // every existing caller and test keeps the entry point it has, and so
    // there is still exactly one implementation of each.
    window.SessionRestartPicker = {
        KIND_BADGE: Options.KIND_BADGE,
        isActionable: Options.isActionable,
        optionsHtml: Options.optionsHtml,
        sharedDetail: Options.sharedDetail,
        sharedDetailHtml: Options.sharedDetailHtml,
        noticeHtml: noticeHtml,
        wrapperNoticeHtml: wrapperNoticeHtml,
        busyHtml: busyHtml,
        present: present,
        open: open,
        lastError: lastError,
    };
})();

console.log('[SessionRestartPicker Module] Exported as window.SessionRestartPicker');
