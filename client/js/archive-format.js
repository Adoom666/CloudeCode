/**
 * Pure display formatting for the archive screen: byte counts, character
 * counts, timestamps, relative ages, sha256 abbreviation and long-slug
 * shortening.
 *
 * WHY EVERY FUNCTION HERE HAS A THIRD RETURN. A formatter is the last
 * place anyone looks for a false green, which is exactly why it is a
 * good place for one to hide. `formatBytes(undefined)` returning "0 B"
 * is a formatter inventing a measurement: it renders a confident,
 * specific, wrong fact in the same typeface as a real one, and no
 * downstream check can tell the difference afterwards. So every function
 * in this file returns the NOT_KNOWN sentinel for input it cannot
 * account for, and never a zero, never a dash, never an empty string.
 * "size: NOT KNOWN" is actionable. A blank cell is not.
 *
 * TWO UNIT FACTS THIS FILE ENCODES, from the live API (2026-08-31). The
 * archive reports `body_chars` in UNICODE CODE POINTS and `body_bytes`
 * in BYTES, and the search response says so explicitly in
 * meta.offset_units / meta.body_size_units. They are different numbers
 * for the same body and they are not interchangeable: body 379 is 19,831
 * code points, 19,843 UTF-16 code units, and a different byte count
 * again. formatChars and formatBytes are therefore separate functions
 * with separate units in their output, so a caller cannot pass one to
 * the other and get a plausible-looking wrong label.
 *
 * Pure. No DOM, no fetch, no globals beyond the export.
 */

console.log('[ArchiveFormat Module] Loading...');

(function () {
    'use strict';

    /**
     * What every function here returns when it cannot evaluate its input.
     * Exported so callers and tests compare against the constant.
     * @type {string}
     */
    var NOT_KNOWN = 'NOT KNOWN';

    /** Bytes per binary step. Binary because the API's own caps are
     *  binary (MAX_BODY_BYTES 67,108,864 = 64 MiB).
     *  @type {number} */
    var BYTES_PER_STEP = 1024;

    /** Binary byte unit ladder, ascending. @type {string[]} */
    var BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

    /** Decimal places used for every unit above plain bytes.
     *  @type {number} */
    var BYTE_DECIMALS = 1;

    /** Seconds in each relative-age step, with its label. Ordered
     *  largest first so the first match wins.
     *  @type {Array<{seconds: number, one: string, many: string}>} */
    var AGE_STEPS = [
        { seconds: 31557600, one: 'year',   many: 'years' },
        { seconds: 2629800,  one: 'month',  many: 'months' },
        { seconds: 86400,    one: 'day',    many: 'days' },
        { seconds: 3600,     one: 'hour',   many: 'hours' },
        { seconds: 60,       one: 'minute', many: 'minutes' },
        { seconds: 1,        one: 'second', many: 'seconds' }
    ];

    /** How many leading hex characters of a sha256 to show. Twelve is
     *  the git-ish convention and is unambiguous across the corpus'
     *  6,240 secret-bearing bodies.
     *  @type {number} */
    var SHA_ABBREV_CHARS = 12;

    /** A full sha256 in lowercase hex. @type {number} */
    var SHA256_HEX_CHARS = 64;

    /** Default maximum rendered width for a slug, in characters.
     *  @type {number} */
    var SLUG_MAX_CHARS = 48;

    /** What replaces the elided middle of a shortened slug.
     *  @type {string} */
    var SLUG_ELLIPSIS = '...';

    /**
     * Description: is this a real, finite, non-negative number we can
     *   format? Rejects NaN, Infinity, null, undefined, strings and
     *   negatives, all of which are "I was not given a measurement".
     * Inputs: n (*) - candidate.
     * Output: boolean.
     */
    function _isCountable(n) {
        return typeof n === 'number' && isFinite(n) && n >= 0;
    }

    /**
     * Description: group an integer with thousands separators, without
     *   depending on the host locale (which would make two machines
     *   render the same corpus differently).
     * Inputs: n (number) - a non-negative finite number.
     * Output: string, e.g. '2447028' -> '2,447,028'.
     */
    function _group(n) {
        var whole = String(Math.floor(n));
        var out = '';
        var count = 0;
        for (var i = whole.length - 1; i >= 0; i--) {
            out = whole.charAt(i) + out;
            count++;
            if (count % 3 === 0 && i > 0) out = ',' + out;
        }
        return out;
    }

    /**
     * Description: render a byte count in binary units.
     * Inputs: bytes (number) - a non-negative finite byte count.
     * Output: string, or NOT_KNOWN when `bytes` is not a usable number.
     * Example:
     *   formatBytes(54376879)  // -> '51.9 MiB'
     *   formatBytes(0)         // -> '0 B'
     *   formatBytes(undefined) // -> 'NOT KNOWN'   (not '0 B')
     */
    function formatBytes(bytes) {
        if (!_isCountable(bytes)) return NOT_KNOWN;
        if (bytes < BYTES_PER_STEP) return _group(bytes) + ' B';
        var value = bytes;
        var unit = 0;
        while (value >= BYTES_PER_STEP && unit < BYTE_UNITS.length - 1) {
            value = value / BYTES_PER_STEP;
            unit++;
        }
        return value.toFixed(BYTE_DECIMALS) + ' ' + BYTE_UNITS[unit];
    }

    /**
     * Description: render a character count. Deliberately NOT abbreviated
     *   into units: `body_chars` is in unicode code points and the exact
     *   number is what the size gates are evaluated against, so rounding
     *   it in the UI would make a gated row's stated size disagree with
     *   the reason it was gated.
     * Inputs: chars (number) - a non-negative finite code-point count.
     * Output: string, or NOT_KNOWN.
     * Example: formatChars(54376859) // -> '54,376,859 chars'
     */
    function formatChars(chars) {
        if (!_isCountable(chars)) return NOT_KNOWN;
        return _group(chars) + (chars === 1 ? ' char' : ' chars');
    }

    /**
     * Description: render a plain count of things (transcripts, lines,
     *   findings) with thousands separators and no unit.
     * Inputs: n (number) - a non-negative finite count.
     * Output: string, or NOT_KNOWN.
     * Example: formatCount(3416) // -> '3,416'
     */
    function formatCount(n) {
        if (!_isCountable(n)) return NOT_KNOWN;
        return _group(n);
    }

    /**
     * Description: parse an API timestamp into epoch milliseconds.
     *   Shared by formatTimestamp and formatRelativeAge so the two can
     *   never disagree about whether a given string is parseable.
     * Inputs: iso (string) - e.g. '2026-08-30T16:01:00.290244Z'.
     * Output: number (epoch ms), or null when unparseable.
     */
    function _epochMs(iso) {
        if (typeof iso !== 'string' || iso === '') return null;
        var ms = Date.parse(iso);
        return isFinite(ms) ? ms : null;
    }

    /**
     * Description: render an API timestamp for display, in the viewer's
     *   local time zone.
     * Inputs: iso (string) - an ISO 8601 timestamp from the API.
     * Output: string, or NOT_KNOWN when the value is absent or does not
     *   parse. Never the epoch, and never today's date as a stand-in.
     * Example:
     *   formatTimestamp('2026-08-30T16:01:00.290244Z')
     *   // -> '2026-08-30 12:01:00' (rendered in local time)
     */
    function formatTimestamp(iso) {
        var ms = _epochMs(iso);
        if (ms === null) return NOT_KNOWN;
        var d = new Date(ms);
        return d.getFullYear() + '-' +
               _pad2(d.getMonth() + 1) + '-' +
               _pad2(d.getDate()) + ' ' +
               _pad2(d.getHours()) + ':' +
               _pad2(d.getMinutes()) + ':' +
               _pad2(d.getSeconds());
    }

    /**
     * Description: zero-pad a number to two digits.
     * Inputs: n (number).
     * Output: string.
     */
    function _pad2(n) {
        return (n < 10 ? '0' : '') + n;
    }

    /**
     * Description: render how long ago a timestamp was. `nowMs` is a
     *   parameter rather than a call to Date.now() so this function is
     *   pure and a test can pin the clock instead of asserting against a
     *   moving target.
     * Inputs: iso (string) - an ISO 8601 timestamp from the API.
     *         nowMs (number) - the reference instant, epoch milliseconds.
     * Output: string, or NOT_KNOWN when either input is unusable.
     *   A timestamp in the FUTURE returns 'in the future' rather than a
     *   negative age: a negative duration is a fact about clock skew, not
     *   about the record, and rendering it as '-3 days ago' invites the
     *   reader to treat it as a real age.
     * Example:
     *   formatRelativeAge('2026-08-30T16:01:00Z', Date.parse('2026-08-31T16:01:00Z'))
     *   // -> '1 day ago'
     */
    function formatRelativeAge(iso, nowMs) {
        var ms = _epochMs(iso);
        if (ms === null) return NOT_KNOWN;
        if (typeof nowMs !== 'number' || !isFinite(nowMs)) return NOT_KNOWN;
        var deltaSeconds = Math.floor((nowMs - ms) / 1000);
        if (deltaSeconds < 0) return 'in the future';
        if (deltaSeconds < 1) return 'just now';
        for (var i = 0; i < AGE_STEPS.length; i++) {
            var step = AGE_STEPS[i];
            if (deltaSeconds >= step.seconds) {
                var count = Math.floor(deltaSeconds / step.seconds);
                return count + ' ' + (count === 1 ? step.one : step.many) + ' ago';
            }
        }
        return 'just now';
    }

    /**
     * Description: shorten a sha256 to its leading characters for
     *   display. REFUSES anything that is not a full lowercase-hex
     *   sha256, because silently truncating some other string produces
     *   an identifier-looking thing that identifies nothing.
     * Inputs: hex (string) - a 64-character lowercase hex digest.
     * Output: string of SHA_ABBREV_CHARS characters, or NOT_KNOWN.
     * Example: abbreviateSha('0236d0f520b4c7373d7c62dd0563733' +
     *                        '04f8cac3b160103c523132587832454f1')
     *          // -> '0236d0f520b4'
     */
    function abbreviateSha(hex) {
        if (typeof hex !== 'string') return NOT_KNOWN;
        if (hex.length !== SHA256_HEX_CHARS) return NOT_KNOWN;
        if (!/^[0-9a-f]+$/.test(hex)) return NOT_KNOWN;
        return hex.slice(0, SHA_ABBREV_CHARS);
    }

    /**
     * Description: shorten a long project slug or path for a fixed-width
     *   rail, eliding the MIDDLE rather than the tail. Project slugs in
     *   this corpus are path-derived and their distinguishing part is at
     *   the END, so a tail truncation renders a column of identical
     *   prefixes that cannot be told apart.
     *
     *   Operates on code points, not UTF-16 code units, so a slug
     *   containing astral characters is never cut through the middle of
     *   a surrogate pair (which would render as a replacement glyph).
     * Inputs: text (string) - the slug.
     *         maxChars (number, optional) - budget in code points,
     *           default SLUG_MAX_CHARS. Values below the ellipsis width
     *           are treated as unusable.
     * Output: string, or NOT_KNOWN when `text` is not a string.
     * Example:
     *   shortenSlug('-Users-jsugamele-Development-Assistants-Infrastructure', 30)
     *   // -> '-Users-jsuga...s-Infrastructure'
     */
    function shortenSlug(text, maxChars) {
        if (typeof text !== 'string') return NOT_KNOWN;
        var limit = (typeof maxChars === 'number' && isFinite(maxChars))
            ? Math.floor(maxChars) : SLUG_MAX_CHARS;
        if (limit <= SLUG_ELLIPSIS.length) return NOT_KNOWN;

        // Array.from splits on code points, so a surrogate pair stays
        // intact. String.prototype.slice would happily cut one in half.
        var points = Array.from(text);
        if (points.length <= limit) return text;

        var keep = limit - SLUG_ELLIPSIS.length;
        var head = Math.ceil(keep / 2);
        var tail = keep - head;
        return points.slice(0, head).join('') +
               SLUG_ELLIPSIS +
               (tail > 0 ? points.slice(points.length - tail).join('') : '');
    }

    /**
     * Class prefix used by renderTranscriptHeader when a caller supplies
     * none. Named rather than repeated as a literal.
     * @type {string}
     */
    var READER_ROOT_CLASS = 'archive-reader';

    /**
     * Description: build one element with a class and, optionally, text.
     *   Text always goes through a text node, never innerHTML.
     * Inputs: doc (Document), tag (string), cls (string|null),
     *         text (string|null).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) {
            node.appendChild(doc.createTextNode(String(text)));
        }
        return node;
    }

    /**
     * Description: render the transcript header facts as an element.
     *   It stays visible through a cannot-determine on the LINES,
     *   because the header's facts came from a different request that
     *   succeeded, and hiding them would discard a real measurement.
     * Inputs: doc (Document), header (object|null) - the server's
     *           header record, or null when there is none.
     *         rootClass (string|null) - class prefix.
     * Output: Element|null - null when `header` is null. Null is not an
     *   empty header: it means no header request has succeeded yet, and
     *   the caller renders nothing rather than inventing blank facts.
     * Example:
     *   renderTranscriptHeader(document,
     *     {transcript_id: 5767, line_count: 30805,
     *      raw_byte_length: 91950363}, 'archive-reader');
     */
    function renderTranscriptHeader(doc, header, rootClass) {
        if (!doc) throw new Error('renderTranscriptHeader needs a document');
        if (!header) return null;
        var rc = rootClass || READER_ROOT_CLASS;
        var box = el(doc, 'div', rc + '__header-facts', null);
        // FIELD NAMES ARE THE SERVER'S, not invented ones. Measured
        // 2026-08-31 against the live GET
        // /api/v1/archive/transcripts/5767: the header record carries
        // `transcript_id`, `source_path` and `raw_byte_length`. This
        // block previously read `header.byte_length`, `header.path`
        // and `header.id` - none of which the server sends - so a
        // fully successful header request rendered
        // "30,805 lines / size NOT KNOWN" over a byte count the app
        // already held. The unit test passed throughout because its
        // fixture used the same invented names.
        box.appendChild(el(doc, 'h2', rc + '__title',
            header.session_ref || header.source_path ||
                ('transcript ' + header.transcript_id)));
        box.appendChild(el(doc, 'p', rc + '__facts',
            (Number.isFinite(header.line_count)
                ? formatCount(header.line_count) + ' lines'
                : 'line count ' + NOT_KNOWN) + '  /  ' +
            (Number.isFinite(header.raw_byte_length)
                ? formatBytes(header.raw_byte_length)
                : 'size ' + NOT_KNOWN)));
        return box;
    }

    window.ArchiveFormat = {
        renderTranscriptHeader: renderTranscriptHeader,
        formatBytes: formatBytes,
        formatChars: formatChars,
        formatCount: formatCount,
        formatTimestamp: formatTimestamp,
        formatRelativeAge: formatRelativeAge,
        abbreviateSha: abbreviateSha,
        shortenSlug: shortenSlug,
        NOT_KNOWN: NOT_KNOWN,
        SLUG_MAX_CHARS: SLUG_MAX_CHARS,
        SHA_ABBREV_CHARS: SHA_ABBREV_CHARS
    };
    console.log('[ArchiveFormat Module] Exported as window.ArchiveFormat');
})();
