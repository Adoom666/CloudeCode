/**
 * Masks flagged secret material in an archive body before it is rendered.
 *
 * WHY THIS FILE HAS NO DOM IMPORT: it is the one function in this screen
 * whose bug is a credential disclosure. Keeping it a pure
 * string-in/string-out function means it is testable under plain node
 * with no harness, and it means no rendering path can accidentally reach
 * around it.
 *
 * THE SERVER FLAGS. IT NEVER REDACTS. Byte-exactness is the whole point
 * of the archive: a body that came back redacted would not be the bytes
 * that were on disk, and the archive would be worthless as evidence. So
 * the server returns the real body plus a findings array, and masking is
 * the client's job. This is a LENS over stored bytes, not a change to
 * them.
 *
 * THE TRAP, MEASURED. A JavaScript string is indexed in UTF-16 code
 * units. A Python string is indexed in code points. Every character
 * outside the Basic Multilingual Plane is one code point and TWO UTF-16
 * code units, so a Python-computed offset and a JavaScript index diverge
 * by one for every astral character that precedes them, and the
 * divergence grows monotonically through the body.
 *
 * Measured on real corpus body 379, live server, 2026-08-31. The body is
 * 19,831 code points and 19,843 UTF-16 code units - twelve astral
 * characters. Three findings, all the same 40-character credential:
 *
 *     finding   match_offset   match_offset_utf16   drift
 *        1          5,197            5,201           +4
 *        2         11,058           11,066           +8
 *        3         17,340           17,352          +12
 *
 * Masking finding 1 with match_offset instead of match_offset_utf16
 * slides the 40-unit window four units LEFT: it covers four characters
 * of the preceding JSON key and stops four characters short of the end
 * of the credential. THE LAST FOUR CHARACTERS OF THE CREDENTIAL ARE
 * LEFT ON SCREEN. By finding 3 it would be twelve of forty. There is no
 * error, no warning, and the output looks entirely plausible: a run of
 * marker text with a short hex tail that reads like surrounding prose.
 *
 * So: the `_utf16` fields are the ONLY ones a JavaScript client may use,
 * and this file never reads match_offset or match_length at all.
 *
 * REFUSAL IS THE DEFAULT, NOT THE EDGE CASE. Half-masked output is worse
 * than no output, because it looks like it worked. Every input this
 * module cannot fully account for produces a refusal with null text -
 * never a best-effort mask.
 *
 * NOTHING IN THIS FILE EVER PUTS MATCHED TEXT INTO A RETURN VALUE, A
 * REASON STRING, A LOG LINE OR AN EXCEPTION. The reason strings below
 * carry counts, offsets and lengths only.
 *
 * Pure. No DOM, no fetch, no globals beyond the export.
 */

console.log('[ArchiveMask Module] Loading...');

(function () {
    'use strict';

    /**
     * Sentinel `status` meaning the body MUST NOT be rendered at all.
     * Exported so callers compare against the constant rather than
     * re-typing the string, and so a test can assert on it.
     * @type {string}
     */
    var MASK_REFUSED = 'mask-refused';

    /**
     * Sentinel `status` meaning the returned text is safe to render.
     * @type {string}
     */
    var MASK_OK = 'ok';

    /**
     * The replacement text. Fixed width and self-describing: it must be
     * obvious that something was removed BY THIS VIEW, and not that the
     * archive stored asterisks. The archive is byte-exact; this is a
     * lens.
     *
     * FIXED WIDTH IS DELIBERATE. A marker whose length is proportional
     * to the secret publishes the credential's length, which is a small
     * leak taken for no benefit. The offsets of not-yet-applied findings
     * are kept valid by splicing from the HIGHEST offset down (see
     * maskBody case 5), not by preserving the string's length.
     * @type {string}
     */
    var SECRET_MARKER = '[SECRET REDACTED IN THIS VIEW]';

    /**
     * Description: is one finding usable for masking a JavaScript string?
     *   Every branch that returns false is a distinct way of saying "I do
     *   not know where the secret is", and each one poisons the whole
     *   body rather than just its own window.
     * Inputs: f (object) - one entry from an API `secrets` array.
     *         len (number) - the body's length in UTF-16 code units,
     *           i.e. plain `body.length`.
     * Output: boolean - true only if the UTF-16 window is fully known and
     *   lies inside the string.
     * Example: _findingIsUsable({utf16_state: 'computed',
     *   match_offset_utf16: 5201, match_length_utf16: 40}, 19843) // true
     */
    function _findingIsUsable(f, len) {
        if (!f || typeof f !== 'object') return false;

        // THREE-OUTCOME GATE. `utf16_state` is 'computed' when the server
        // derived the UTF-16 pair from the body, or 'cannot_determine'
        // when it could not. NORMATIVE: on cannot_determine we do NOT
        // fall back to the code-point offsets and we do NOT render the
        // body. A body carrying a finding whose position is unknown is a
        // body with a credential at an unknown location, and there is no
        // partial masking that is safe.
        if (f.utf16_state !== 'computed') return false;

        var o = f.match_offset_utf16;
        var l = f.match_length_utf16;

        // Number.isInteger is false for undefined, null, NaN, Infinity,
        // strings and non-integral numbers, which is every shape of "the
        // field is not a usable index".
        if (!Number.isInteger(o) || !Number.isInteger(l)) return false;
        if (o < 0 || l <= 0) return false;

        // `len` is body.length, which IS the UTF-16 code-unit count -
        // exactly the unit the _utf16 fields are in. Do NOT compute a
        // code-point length with [...body].length and compare against a
        // UTF-16 offset; that reintroduces the original bug inside the
        // validator, where it is even harder to see.
        if (o + l > len) return false;

        return true;
    }

    /**
     * Description: replace every flagged secret in a body with a
     *   fixed-width marker, using ONLY the UTF-16 offsets. Refuses
     *   outright rather than masking approximately.
     * Inputs: body (string) - the body_json exactly as the server sent it.
     *         findings (Array|null|undefined) - the `secrets` array from
     *           GET /archive/bodies/{id}. `null` and `undefined` are NOT
     *           the same as `[]`; see cases 1 and 2 below.
     *         declaredCount (number) - `secret_finding_count` from the
     *           line or body row. An INDEPENDENT count of how many
     *           secrets the server believes are in this body, read from a
     *           different column than the array.
     * Output: {status: 'ok', text: string, masked: number}
     *      or {status: 'mask-refused', text: null, reason: string,
     *          findingCount: number}
     *   A refusal always carries `text: null`. There is no shape of this
     *   return value that hands back partially-masked text.
     * Example:
     *   maskBody(body, [{utf16_state: 'computed',
     *                    match_offset_utf16: 5201,
     *                    match_length_utf16: 40}], 1)
     *   // -> {status: 'ok', text: '...[SECRET REDACTED IN THIS VIEW]...',
     *   //     masked: 1}
     */
    function maskBody(body, findings, declaredCount) {
        var declared = Number.isInteger(declaredCount) ? declaredCount : 0;

        if (typeof body !== 'string') {
            return _refuse('body is not a string', declared);
        }

        var list = Array.isArray(findings) ? findings : null;

        // CASE 1: the server says there are no secrets and sent no
        // findings. Render as-is. The archive is byte-exact and masking
        // must be a no-op when there is nothing to mask.
        if (declared === 0 && (list === null || list.length === 0)) {
            return { status: MASK_OK, text: body, masked: 0 };
        }

        // CASE 2: the server says there ARE secrets but gave us no
        // findings array. This is the live /lines?include_bodies=true
        // shape as measured 2026-08-31: line 292 of transcript 4 carries
        // secret_finding_count 3, a 19,831-character body with the real
        // credentials in it, and NO `secrets` key at all. We know a
        // credential is in this string and we do not know where. Refuse.
        if (declared > 0 && (list === null || list.length === 0)) {
            return _refuse(
                'the body declares ' + declared + ' secret finding(s) but ' +
                'carries no findings array, so their positions are unknown',
                declared
            );
        }

        // CASE 3: fewer findings than declared. The count and the array
        // come from different columns and can disagree; something was
        // dropped in transit or in serialization. Masking what we have
        // would leave the rest visible WHILE LOOKING MASKED, which is
        // worse than refusing. This is a positive control on the masking
        // input: trusting only the array means a body whose array was
        // dropped renders unmasked with no complaint.
        if (declared > 0 && list.length < declared) {
            return _refuse(
                'the body declares ' + declared + ' secret finding(s) but ' +
                'only ' + list.length + ' were returned',
                declared
            );
        }

        // CASE 4: any finding whose UTF-16 window is not fully known
        // poisons the WHOLE body, not just its own window.
        var len = body.length;
        var i;
        for (i = 0; i < list.length; i++) {
            if (!_findingIsUsable(list[i], len)) {
                return _refuse(_unusableReason(list[i], len),
                               declared || list.length);
            }
        }

        // CASE 5: mask.
        //
        // Merge overlapping windows first. Two independent splices over
        // one region produce garbage, and overlapping detector hits are
        // a real shape rather than a hypothetical. The MERGE IS SAFE by
        // construction: the union of two windows covers every code unit
        // either of them covered, so no character of either match can
        // survive it.
        var windows = list.map(function (f) {
            return {
                start: f.match_offset_utf16,
                end: f.match_offset_utf16 + f.match_length_utf16
            };
        }).sort(function (a, b) { return a.start - b.start; });

        var merged = [];
        for (i = 0; i < windows.length; i++) {
            var w = windows[i];
            var last = merged.length ? merged[merged.length - 1] : null;
            if (last && w.start <= last.end) {
                if (w.end > last.end) last.end = w.end;
            } else {
                merged.push({ start: w.start, end: w.end });
            }
        }

        // Splice from the HIGHEST offset down, so an applied replacement
        // cannot shift the offsets of the ones still pending. This is
        // what keeps the remaining findings valid; it is the reason the
        // marker does not need to be the same width as the secret.
        var out = body;
        for (i = merged.length - 1; i >= 0; i--) {
            out = out.slice(0, merged[i].start) +
                  SECRET_MARKER +
                  out.slice(merged[i].end);
        }
        return { status: MASK_OK, text: out, masked: merged.length };
    }

    /**
     * Description: build a refusal. Centralised so that `text: null` is
     *   structurally impossible to forget on a refusal path.
     * Inputs: reason (string) - why, in words a person can act on. Never
     *   contains body content or matched text.
     *         findingCount (number) - how many secrets are believed to be
     *   in the body, so the view can say "3 secrets, positions unknown".
     * Output: {status: 'mask-refused', text: null, reason: string,
     *          findingCount: number}
     */
    function _refuse(reason, findingCount) {
        return {
            status: MASK_REFUSED,
            text: null,
            reason: reason,
            findingCount: Number.isInteger(findingCount) ? findingCount : 0
        };
    }

    /**
     * Description: name WHY a finding is unusable, distinguishing the
     *   server's own cannot_determine from a window that is structurally
     *   broken. A refusal that does not say what it could not evaluate is
     *   a blank cell, and a blank cell is not an answer.
     * Inputs: f (object|null) - the rejected finding.
     *         len (number) - body length in UTF-16 code units.
     * Output: string - offsets, lengths and state only. Never body text.
     */
    function _unusableReason(f, len) {
        var state = f && f.utf16_state;
        if (state === 'cannot_determine') {
            return 'a finding reports utf16_state=cannot_determine, so its ' +
                   'position in a JavaScript string is not known';
        }
        return 'a finding has no usable UTF-16 window (utf16_state=' +
               String(state) +
               ' offset=' + String(f && f.match_offset_utf16) +
               ' length=' + String(f && f.match_length_utf16) +
               ' bodyLength=' + len + ')';
    }

    window.ArchiveMask = {
        maskBody: maskBody,
        MASK_REFUSED: MASK_REFUSED,
        MASK_OK: MASK_OK,
        SECRET_MARKER: SECRET_MARKER
    };
    console.log('[ArchiveMask Module] Exported as window.ArchiveMask');
})();
