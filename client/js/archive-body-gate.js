/**
 * The body GATE POLICY and the MASK APPLICATION: the size thresholds
 * that decide whether a transcript body may be fetched at all, the
 * eight states a body can be in, and the one function that applies
 * archive-mask.js's findings to text before anything can render it.
 *
 * WHY THIS IS SEPARATE FROM archive-body-cache.js. The sibling file is
 * a MECHANISM - an LRU with a deadline, an in-flight map and eviction
 * arithmetic. This file is a POLICY, and it is the security-relevant
 * half: gateFor() is what stops a 54 MB body being pulled by scrolling
 * past it, and _applyMask() is what stops a credential reaching the
 * screen. A policy and the cache that enforces it change for different
 * reasons and should be readable apart.
 *
 * THE GATE IS EVALUATED FROM THE SPINE, BEFORE ANY NETWORK HAPPENS.
 * That is the whole point of separating size from content: the spine
 * already carries the byte length, so nothing has to be downloaded to
 * decide that it must not be downloaded.
 *
 * THE HARD GATE IS NOT A STRONGER SOFT GATE. A soft gate is "not
 * automatically, but you may ask"; a hard gate is "never, whatever
 * `force` says". Collapsing them into one threshold with a flag is how
 * a render-anyway button ends up able to hang the tab.
 *
 * EIGHT STATES, AND THEY ARE EXPORTED AS CONSTANTS so callers compare
 * against a name rather than retyping a string literal. STATE_
 * CANNOT_DETERMINE is a first-class member of that set, not an error
 * flavour: "the server refused to say" is a different finding from
 * "there is no body" and from "the body is withheld", and a reader that
 * cannot tell the three apart will report one of them wrongly.
 *
 * A MASK REFUSAL FAILS CLOSED. If the masker cannot account for every
 * finding it declared, the text does not render at all - a partially
 * masked body is worse than no body, because it looks safe.
 *
 * Depends on archive-mask.js and archive-outcome.js.
 * Exports window.ArchiveBodyGate.
 */

console.log('[ArchiveBodyGate Module] Loading...');

(function () {
    'use strict';

    /**
     * Above this many UTF-16 code units a body is not auto-fetched and
     * not auto-rendered; the row shows the size and a "render anyway"
     * action. 256 KiB. @type {number}
     */
    var BODY_INLINE_MAX = 262144;

    /**
     * Above this many UTF-16 code units a body is never fetched and
     * never rendered: download only, with the reason stated. There is no
     * render option at any depth of the UI. 2 MiB. @type {number}
     */
    var BODY_RENDER_HARD_MAX = 2097152;

    /** Hard cap on cached bodies, by count. @type {number} */
    var BODY_CACHE_MAX_ENTRIES = 300;

    /** Hard cap by total characters. 32 MiB of text. @type {number} */
    var BODY_CACHE_MAX_CHARS = 33554432;

    /**
     * Deadline for one body fetch, milliseconds. A 54 MB body is a
     * legitimate slow transfer, so this is generous; what it is not is
     * absent. A request with no terminal condition is a state that can
     * never fail.
     * @type {number}
     */
    var BODY_DEADLINE_MS = 30000;

    /** Entry states. Exported so callers compare constants, not strings. */
    var STATE_OK = 'included';
    var STATE_GATED_SOFT = 'gated-soft';
    var STATE_GATED_HARD = 'gated-hard';
    var STATE_WITHHELD = 'withheld-server';
    var STATE_MASK_REFUSED = 'mask-refused';
    var STATE_CANNOT_DETERMINE = 'cannot-determine';
    var STATE_LOADING = 'loading';
    var STATE_NO_BODY = 'no-body';

    /**
     * Decide, from spine metadata alone, whether a body may be fetched
     * and rendered. NORMATIVE: every fetch path calls this first and
     * honours it. Nothing here reads a body.
     * @param {?object} row a spine row; reads `body_chars`, `body_state`
     *   and `body_id` only
     * @returns {{state: string, chars: ?number, reason: ?string}} state is
     *   included|gated-soft|gated-hard|withheld-server|no-body|
     *   cannot-determine. `included` means "small enough to fetch", NOT
     *   "already fetched".
     * @example gateFor({body_id: 1, body_chars: 54376859})
     *   // -> {state: 'gated-hard', chars: 54376859, reason: '...'}
     */
    function gateFor(row) {
        if (!row || typeof row !== 'object') {
            return { state: STATE_CANNOT_DETERMINE, chars: null,
                reason: 'no spine row for this line' };
        }
        // The server's own refusal wins outright: it is a finding the
        // server made about its own limits and the client must render
        // it as the server's, not restate it as its own.
        if (row.body_state === 'withheld_too_large') {
            return { state: STATE_WITHHELD,
                chars: Number.isFinite(row.body_chars) ? row.body_chars : null,
                reason: 'the server withheld this body as too large' };
        }
        if (row.body_id === null || row.body_id === undefined) {
            return { state: STATE_NO_BODY, chars: null,
                reason: 'this line has no body row' };
        }
        var chars = row.body_chars;
        // A size we cannot read is not a small size. Refusing to guess
        // here is the difference between a gate and a coin flip.
        if (!Number.isFinite(chars) || chars < 0) {
            return { state: STATE_CANNOT_DETERMINE, chars: null,
                reason: 'body_chars is ' + String(chars) +
                        ', so the size of this body is not known' };
        }
        if (chars > BODY_RENDER_HARD_MAX) {
            return { state: STATE_GATED_HARD, chars: chars,
                reason: 'this body is ' + chars + ' characters, past the ' +
                        BODY_RENDER_HARD_MAX + ' character hard limit. ' +
                        'Rendering it would hang the tab with no way back, ' +
                        'so there is no render option.' };
        }
        if (chars > BODY_INLINE_MAX) {
            return { state: STATE_GATED_SOFT, chars: chars,
                reason: 'this body is ' + chars + ' characters, past the ' +
                        BODY_INLINE_MAX + ' character inline limit.' };
        }
        return { state: STATE_OK, chars: chars, reason: null };
    }

    /**
     * Run archive-mask.js over a fetched body and fold its result into a
     * cache entry. The ONLY place this module turns body text into
     * something renderable. `text` is null on every refusal path.
     * @param {string} body @param {?Array} findings
     * @param {number} declaredCount
     * @returns {{state: string, text: ?string, masked: number,
     *   reason: ?string, findingCount: number}}
     */
    function _applyMask(body, findings, declaredCount) {
        var mask = window.ArchiveMask.maskBody(body, findings, declaredCount);
        if (mask.status === window.ArchiveMask.MASK_REFUSED) {
            return { state: STATE_MASK_REFUSED, text: null, masked: 0,
                reason: mask.reason, findingCount: mask.findingCount };
        }
        return { state: STATE_OK, text: mask.text, masked: mask.masked,
            reason: null, findingCount: Number.isInteger(declaredCount)
                ? declaredCount : 0 };
    }

    /**
     * One-line reason from a classified failure envelope, naming what
     * could not be evaluated rather than leaving a blank.
     * @param {object} c an ArchiveOutcome.classify() result
     * @returns {string}
     */
    function _reasonFrom(c) {
        var first = c.reasons && c.reasons.length ? c.reasons[0] : null;
        if (first && first.reason) {
            return (first.subject ? first.subject + ': ' : '') + first.reason;
        }
        return 'the body request returned ' + c.token +
               ' and carried no reason';
    }

    window.ArchiveBodyGate = {
        gateFor: gateFor,
        applyMask: _applyMask,
        reasonFrom: _reasonFrom,
        BODY_INLINE_MAX: BODY_INLINE_MAX,
        BODY_RENDER_HARD_MAX: BODY_RENDER_HARD_MAX,
        BODY_CACHE_MAX_ENTRIES: BODY_CACHE_MAX_ENTRIES,
        BODY_CACHE_MAX_CHARS: BODY_CACHE_MAX_CHARS,
        BODY_DEADLINE_MS: BODY_DEADLINE_MS,
        STATE_OK: STATE_OK,
        STATE_GATED_SOFT: STATE_GATED_SOFT,
        STATE_GATED_HARD: STATE_GATED_HARD,
        STATE_WITHHELD: STATE_WITHHELD,
        STATE_MASK_REFUSED: STATE_MASK_REFUSED,
        STATE_CANNOT_DETERMINE: STATE_CANNOT_DETERMINE,
        STATE_LOADING: STATE_LOADING,
        STATE_NO_BODY: STATE_NO_BODY
    };
    console.log('[ArchiveBodyGate Module] Exported as window.ArchiveBodyGate');
})();
