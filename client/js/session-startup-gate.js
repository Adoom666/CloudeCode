/**
 * Session Startup Gate - the "needs a keypress" indicator.
 *
 * Punchlist item 19. A claude parked on its folder-trust dialog is a live
 * pane running a real process that has fired no lifecycle hook, so the
 * row painted a green dot and a pid over a session that was waiting for
 * somebody to press a key. The server now measures that and ships
 * `startup_gate` at the WRAPPER level of SessionInfo (not inside
 * `.session` - see CLAUDE.md, this is the single most repeated bug here).
 *
 * THREE VALUES, AND ONLY ONE OF THEM PAINTS ANYTHING:
 *
 *   'awaiting_startup_prompt'  measured: the session is blocked. Renders
 *                              the badge.
 *   'ready'                    measured: it is not blocked. Renders
 *                              nothing, because "not blocked" is the
 *                              normal case and a badge for it would be
 *                              noise on every row in the list.
 *   'unknown'                  could not be determined. ALSO renders
 *                              nothing, and that is not the same
 *                              decision as 'ready' - it is the refusal
 *                              to state either. A badge reading "maybe
 *                              stuck?" on every unmeasurable row would
 *                              train the user to ignore the one row that
 *                              means it.
 *
 * Anything else - a field absent because the browser tab predates the
 * server that added it, a null, a typo - normalizes to 'unknown' and
 * therefore paints nothing. An older client simply never sees the badge,
 * which is the correct degradation.
 *
 * Copy is lowercase and plain, like the rest of the UI, and says what to
 * DO rather than what was detected: "needs a keypress", not "no
 * SessionStart hook observed".
 *
 * No dependencies. Must load BEFORE session-sidebar-rows.js and
 * launchpad.js, both of which call into it.
 */

console.log('[SessionStartupGate Module] Loading...');

(function () {
    /** Measured: this session is blocked on an unanswered startup prompt. */
    const AWAITING = 'awaiting_startup_prompt';
    /** Measured: it is not blocked. */
    const READY = 'ready';
    /** Could not determine. Never rendered as either of the other two. */
    const UNKNOWN = 'unknown';

    /** Every value the server may send. @type {Array<string>} */
    const ALL = [READY, AWAITING, UNKNOWN];

    /** The badge's visible text and its accessible name. */
    const LABEL = 'needs a keypress';

    /**
     * The sentence behind the badge - what is actually happening and what
     * the user should do about it. Used as the title/aria-label so the
     * two-word badge is never the only explanation available.
     */
    const REASON =
        'this session is waiting at a startup prompt and has not started '
        + 'yet. open it and answer the prompt.';

    /**
     * Normalize any input into one of the three known gate values.
     *
     * Description: Defensive normalizer. A field the server did not send
     *   (older cached response, a payload built before this feature) must
     *   land on 'unknown' rather than being treated as either measured
     *   answer.
     * Inputs:
     *   gate (string|null|undefined) - raw `startup_gate` from the API.
     * Output:
     *   string - one of 'ready' | 'awaiting_startup_prompt' | 'unknown'.
     * Example:
     *   normalize(undefined) -> 'unknown'
     *   normalize('ready')   -> 'ready'
     */
    function normalize(gate) {
        return ALL.indexOf(gate) === -1 ? UNKNOWN : gate;
    }

    /**
     * Is this row blocked on a startup prompt?
     *
     * Description: The one predicate every call site should use, so that
     *   "does it paint" is decided in a single place. Deliberately a
     *   strict equality against the measured value rather than a
     *   truthiness test on the field: `!!row.startup_gate` would be true
     *   for 'unknown' and for 'ready' alike.
     * Inputs:
     *   gate (string|null|undefined) - raw `startup_gate` value.
     * Output:
     *   boolean - true only for the measured blocked state.
     * Example:
     *   isAwaiting('unknown') -> false
     */
    function isAwaiting(gate) {
        return normalize(gate) === AWAITING;
    }

    /**
     * Escape a value for interpolation into a double-quoted HTML attribute.
     *
     * Description: This module has no dependencies by design (it loads
     *   before everything that calls it), so it carries its own escaper
     *   rather than reaching for SessionStatusUI.escapeAttr. `&` is
     *   replaced first or the later replacements would be re-escaped.
     * Inputs: value (any) - stringified; null/undefined become ''.
     * Output: string - safe between the quotes of an attribute.
     * Example: escapeAttr('a"b') -> 'a&quot;b'
     */
    function escapeAttr(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /**
     * Build the "needs a keypress" badge for a session row or card.
     *
     * Description: Returns markup ONLY for the measured blocked state;
     *   'ready' and 'unknown' both return the empty string. Carries
     *   `title` and `aria-label` (the full sentence, not the two-word
     *   badge) plus `role="img"`, so the meaning is not conveyed by
     *   position or color alone - the same accessibility contract
     *   SessionStatusUI.dotHtml holds itself to.
     * Inputs:
     *   gate (string|null|undefined) - raw `startup_gate` value.
     * Output:
     *   string - HTML for one inline `<span>`, or '' when nothing should
     *   be painted.
     * Example:
     *   indicatorHtml('awaiting_startup_prompt')
     *     -> '<span class="session-startup-gate" role="img" ...>needs a keypress</span>'
     *   indicatorHtml('ready') -> ''
     */
    function indicatorHtml(gate) {
        if (!isAwaiting(gate)) return '';
        return (
            '<span class="session-startup-gate" role="img" '
            + `title="${escapeAttr(REASON)}" `
            + `aria-label="${escapeAttr(REASON)}">${escapeAttr(LABEL)}</span>`
        );
    }

    /**
     * The plain-text label, for a caller that wants words without markup.
     * Inputs: gate (string|null|undefined).
     * Output: string|null - the label, or null when nothing should be said.
     * Example: labelFor('unknown') -> null
     */
    function labelFor(gate) {
        return isAwaiting(gate) ? LABEL : null;
    }

    window.SessionStartupGate = {
        AWAITING, READY, UNKNOWN, ALL, LABEL, REASON,
        normalize, isAwaiting, indicatorHtml, labelFor, escapeAttr,
    };
    console.log('[SessionStartupGate Module] Exported as window.SessionStartupGate');
})();
