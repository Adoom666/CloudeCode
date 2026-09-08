/**
 * WHAT HAPPENS TO THE CONVERSATION, SAID BEFORE ANYTHING HAPPENS.
 * ----------------------------------------------------------------------
 * The owner's definition of restart, 2026-09-07: "restart on recent is
 * really just resume. restart on open is close and resume session so it
 * loads a new wrapper or new claude binary." One semantic, two mechanics.
 * A dead row has no process to kill so its restart IS a resume; a live
 * row gets its pane killed first, and the kill exists only so the pane
 * picks up a new wrapper or a new claude binary. Both come back on the
 * SAME conversation.
 *
 * WHICH MEANS THE UI HAS TO SAY WHEN IT DOES NOT. `sessions` does not
 * always hold a `claude_session_uuid`, and when it does not there is
 * nothing to resume: the session comes back WITHOUT its history. That is
 * a legitimate restart. Quietly performing it and calling it a restart is
 * not, which is why the server sends `conversation` as its own field and
 * this file turns it into a sentence.
 *
 * THREE VALUES, AND THE THIRD IS NOT A FLAVOUR OF THE OTHER TWO. Same
 * vocabulary the server uses (src/core/session_resume_target.py) and the
 * same one `RestartSessionResponse.conversation` has always used:
 *
 *   'resumed'        the row names a conversation and --resume carries it
 *   'none_recorded'  the row was read and names none, so the history does
 *                    not come back
 *   'unknown'        the row could not be read. AN UNKNOWN IS NEVER A
 *                    YES: it says the history is at risk, it never says
 *                    the history is safe.
 *
 * ANYTHING UNRECOGNISED IS TREATED AS 'unknown', including a missing
 * field from an older server. A client that defaults a missing
 * continuity to "resumed" would be inventing the very reassurance this
 * file exists to stop.
 *
 * ITS OWN FILE because session-restart-picker.js is already 578 lines,
 * past this project's 500-line rule, and because the sentence a user
 * reads before losing a conversation deserves to be readable in one
 * place. Load BEFORE session-restart-live.js.
 */

console.log('[SessionRestartContinuity Module] Loading...');

(function () {
    'use strict';

    /**
     * The one sentence per outcome, lowercase and plain, matching the
     * server's own clause in session_resume_target.continuity_phrase.
     * @type {Object<string, string>}
     */
    var LINES = {
        resumed: 'it comes back on the same conversation, resumed where it '
            + 'is now. ',
        none_recorded: 'no conversation is recorded for this session, so it '
            + 'comes back without its history. what is on screen now is not '
            + 'carried over. ',
        unknown: 'whether it comes back on the same conversation could not '
            + 'be determined, so treat its history as at risk. ',
    };

    /**
     * Description: normalise a server `conversation` value. Anything this
     *   client does not recognise, a missing field included, is
     *   'unknown' - never 'resumed'. A verdict we cannot read is not
     *   evidence the history survives.
     * Inputs: value (string|null|undefined).
     * Output: string - 'resumed', 'none_recorded' or 'unknown'.
     */
    function normalize(value) {
        if (value === 'resumed' || value === 'none_recorded') return value;
        return 'unknown';
    }

    /**
     * Description: the sentence to show a user about their conversation
     *   before a restart. Trailing space included so it concatenates into
     *   the confirmation body.
     * Inputs: value (string|null|undefined) - the server's `conversation`.
     * Output: string - one lowercase sentence, always non-empty.
     */
    function line(value) {
        return LINES[normalize(value)];
    }

    /**
     * Description: the `conversation` the panel is currently promising,
     *   for the choice the user has selected. Reads the PROJECTED plan,
     *   the same rung `SessionRestartLive.outcomeFor` reads, because on a
     *   live pane it is the only one that says anything.
     * Inputs:
     *   preview (object) - the RestartPreviewResponse body.
     *   agentType (string|null) - the picked wrapper id, null for the
     *     baseline row.
     * Output: string - 'resumed', 'none_recorded' or 'unknown'.
     */
    function conversationFor(preview, agentType) {
        if (!agentType) {
            var projected = (preview && preview.projected) || {};
            var unchanged = (preview && preview.unchanged) || {};
            return normalize(projected.conversation || unchanged.conversation);
        }
        var found = ((preview && preview.options) || []).filter(function (o) {
            return o && o.agent_type === agentType;
        })[0];
        return normalize(found && found.conversation);
    }

    window.SessionRestartContinuity = {
        normalize: normalize,
        line: line,
        conversationFor: conversationFor,
    };
})();

console.log('[SessionRestartContinuity Module] Exported as window.SessionRestartContinuity');
