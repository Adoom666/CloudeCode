/**
 * KILLING WHAT IS RUNNING, AND SAYING SO FIRST.
 * ----------------------------------------------------------------------
 * TODO item 22 part 2. Restarting a session whose pane is ALIVE kills the
 * process in it and starts a new one in the same pane
 * (`tmux respawn-pane -k`), keeping the tmux name, the row and therefore
 * project attribution, pinned theme, unread state, group filing and
 * sidebar position - nothing is re-carried because nothing moves.
 *
 * IT IS DESTRUCTIVE AND IRREVERSIBLE, so it lives in its own file. The
 * restart picker is a chooser; this is the part that destroys something,
 * and a reviewer should be able to read all of it in one place rather
 * than find it woven through a panel that mostly does not.
 *
 * TWO PIECES, AND THEY ARE THE TWO GATES THE CLIENT OWNS.
 *
 *   `armHtml()`         an UNCHECKED checkbox, always. It takes no
 *                       argument, so there is no field in the server's
 *                       response that could pre-arm it. That is what
 *                       makes "a prediction is never a permission"
 *                       structural here rather than a rule to remember:
 *                       `projected_kind` drives the badge, the sentence
 *                       and the warning, and enables nothing, because
 *                       what enables a live choice is a checkbox state.
 *
 *   `liveConfirmCopy()` the sentence the user reads before anything
 *                       dies, naming the bare-shell outcome when that is
 *                       what the rung says. Measured on the owner's box
 *                       2026-09-07: 15 of 19 live sessions had BOTH an
 *                       empty `#{pane_start_command}` and a NULL
 *                       `agent_type`; re-measured against the live tmux
 *                       socket the same day, 18 of 22 have an empty
 *                       start command, and the ladder gates on THAT
 *                       field alone. So most of this box comes back a
 *                       login shell, and a confirmation that did not say
 *                       so would be teaching the user to click through
 *                       it.
 *
 * FAIL CLOSED IF THIS FILE DOES NOT LOAD. The picker asks for the arm
 * control through `window.SessionRestartLive` and renders nothing when it
 * is absent, so a missing module means a live session simply cannot be
 * restarted - never that it can be restarted without asking.
 *
 * Load AFTER session-status-ui.js; BEFORE session-restart-picker.js.
 */

console.log('[SessionRestartLive Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: the arming control for a live restart. An UNCHECKED
     *   checkbox, always, on every render - it takes no argument from the
     *   preview and there is no payload field that could set it. That is
     *   deliberate: it is the one input in this panel the server cannot
     *   reach, which is what makes "a prediction is never a permission"
     *   structural here rather than a rule someone has to follow.
     * Inputs: none.
     * Output: string - HTML for the checkbox row.
     */
    function armHtml() {
        return '<label class="restart-picker__arm" for="restart-picker-live">'
            + '<input type="checkbox" id="restart-picker-live">'
            + '<span>kill what is running and restart this session in place'
            + '</span></label>';
    }

    /**
     * Description: which rung the panel is currently promising, for the
     *   choice the user has selected. The baseline row answers with the
     *   preview's own `projected`; a wrapper row answers with that
     *   option's `projected_kind`. Always the PROJECTED rung, because on
     *   a live pane it is the only one that says anything.
     * Inputs:
     *   preview (object) - the RestartPreviewResponse body.
     *   agentType (string|null) - the picked wrapper id, null for the
     *     baseline row.
     * Output: object - {kind, detail}, both '' when nothing matched.
     */
    function outcomeFor(preview, agentType) {
        if (!agentType) {
            var projected = (preview && preview.projected) || {};
            var unchanged = (preview && preview.unchanged) || {};
            return {
                kind: projected.kind || unchanged.kind || '',
                detail: projected.detail || unchanged.detail || '',
            };
        }
        var found = ((preview && preview.options) || []).filter(function (o) {
            return o && o.agent_type === agentType;
        })[0];
        if (!found) return { kind: '', detail: '' };
        return {
            kind: found.projected_kind || found.kind || '',
            detail: found.projected_detail || found.detail || '',
        };
    }

    /**
     * Description: the confirmation copy for killing a live pane.
     *
     *   THE BARE SHELL WARNING IS THE REASON THIS IS SAFE TO SHIP.
     *   Measured on the owner's box 2026-09-07, 15 of 19 live sessions
     *   have BOTH an empty `#{pane_start_command}` and a NULL
     *   `agent_type`, so 79 percent of them come back as a login shell
     *   rather than as an agent. A confirmation that did not say so
     *   would be teaching the user to click through it.
     *
     *   IT ALSO SAYS WHAT HAPPENS TO THE CONVERSATION, because a
     *   restart MEANS resume - the owner's definition, 2026-09-07 - and
     *   the one case where that is not true is the one a user must be
     *   told about before the pane dies. The sentence comes from
     *   `SessionRestartContinuity.line`, which treats anything it does
     *   not recognise as 'unknown'. If that module is missing the
     *   continuity line is omitted rather than guessed: no line at all
     *   is better than a reassurance nobody measured.
     *
     *   The busy line INFORMS and never refuses: `activity_state` is
     *   known to read "working" for about four minutes after a resume
     *   before it self-corrects, so blocking on it would refuse a
     *   restart the user can plainly see is needed.
     * Inputs:
     *   preview (object) - the RestartPreviewResponse body.
     *   agentType (string|null) - the picked wrapper, null for baseline.
     *   status (string|null) - the row's activity status.
     * Output: object - {title, message, details, primaryLabel}.
     */
    function liveConfirmCopy(preview, agentType, status, displayName) {
        var outcome = outcomeFor(preview, agentType);
        var details = 'this cannot be undone. the process running in this pane '
            + 'is killed and a new one is started in the same pane. the session '
            + 'keeps its tmux name, its row and its place in the list, and the '
            + 'transcript is kept. ';
        if (outcome.kind === 'shell') {
            details += 'this session has no recorded start command, so it does '
                + 'not come back as an agent. it comes back as a plain login '
                + 'shell. ';
        }
        if (window.SessionRestartContinuity) {
            details += window.SessionRestartContinuity.line(
                window.SessionRestartContinuity.conversationFor(
                    preview, agentType
                )
            );
        }
        var key = window.SessionStatusUI
            ? window.SessionStatusUI.normalizeStatus(status)
            : 'unknown';
        if (key === 'working' || key === 'question' || key === 'notice') {
            details += 'this row currently reads "' + key + '". that signal can '
                + 'lag a few minutes behind what the session is really doing, '
                + 'so it is shown rather than acted on. ';
        }
        if (outcome.detail) {
            details += 'what will be started: ' + outcome.detail + '.';
        }
        return {
            title: 'replace what is running',
            message: 'restart "' + (displayName || '') + '" while it is running?',
            details: details,
            primaryLabel: 'kill and restart',
        };
    }

    window.SessionRestartLive = {
        armHtml: armHtml,
        outcomeFor: outcomeFor,
        liveConfirmCopy: liveConfirmCopy,
    };
})();

console.log('[SessionRestartLive Module] Exported as window.SessionRestartLive');
