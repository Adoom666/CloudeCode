/**
 * Output scanning - pull copyable items out of raw terminal text.
 * ----------------------------------------------------------------------
 * Deliberately GENERAL. The case that forced this was the sign-in flow
 * (`/login` prints a long url plus a short code, neither of which can be
 * selected on a phone), but nothing here knows about that flow: it finds
 * urls and code-shaped tokens in any output. The login case falls out of
 * the general rule rather than being special-cased.
 *
 * No DOM access, no globals beyond the namespace, so it is unit testable
 * in a bare `vm` sandbox.
 */
(function () {
    'use strict';

    /**
     * http(s) urls. Trailing punctuation is trimmed by the caller since
     * terminals wrap and prose often puts a period right after a url.
     */
    var URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/g;

    /**
     * Code-shaped tokens: 6 to 64 chars of upper/digit/dash/underscore,
     * standing alone. Catches "ABCD-EFGH" style device codes and long
     * opaque verification strings. Requires at least one digit or dash so
     * ordinary shouty words (ERROR, WARNING, README) do not qualify.
     */
    var CODE_RE = /(?:^|[\s:=>[(])([A-Z0-9][A-Z0-9_-]{5,63})(?=$|[\s.,;:)\]])/g;

    /** Characters never meaningful at the end of a captured url. */
    var TRAILING_JUNK = /[.,;:!?)\]}>'"]+$/;

    /**
     * Strip trailing punctuation a terminal line commonly appends.
     *
     * @param {string} value
     * @returns {string}
     */
    function trimTrailing(value) {
        return value.replace(TRAILING_JUNK, '');
    }

    /**
     * Extract every http(s) url, most recent first, de-duplicated.
     *
     * @param {string} text - raw terminal text, newlines included.
     * @returns {string[]} urls in reverse order of appearance.
     */
    function findUrls(text) {
        if (!text) return [];
        var out = [];
        var seen = Object.create(null);
        var m;
        URL_RE.lastIndex = 0;
        while ((m = URL_RE.exec(text)) !== null) {
            var url = trimTrailing(m[0]);
            if (url.length < 12) continue;
            if (seen[url]) continue;
            seen[url] = true;
            out.push(url);
        }
        return out.reverse();
    }

    /**
     * Extract code-shaped tokens, most recent first, de-duplicated.
     * Tokens that are part of a url already returned by findUrls() are
     * skipped so a url does not also surface as three fragments.
     *
     * @param {string} text - raw terminal text.
     * @returns {string[]} codes in reverse order of appearance.
     */
    function findCodes(text) {
        if (!text) return [];
        var urls = findUrls(text).join(' ');
        var out = [];
        var seen = Object.create(null);
        // Pad so a token sitting at the very end still has the trailing
        // delimiter the pattern requires.
        var padded = text + ' ';
        var m;
        CODE_RE.lastIndex = 0;
        while ((m = CODE_RE.exec(padded)) !== null) {
            var code = m[1];
            if (!/[0-9-]/.test(code)) continue;
            if (seen[code]) continue;
            if (urls.indexOf(code) !== -1) continue;
            seen[code] = true;
            out.push(code);
        }
        return out.reverse();
    }

    /**
     * Everything worth offering as a one-tap copy, urls before codes.
     *
     * @param {string} text - raw terminal text.
     * @param {number} [limit=12] - maximum items returned.
     * @returns {Array<{kind: string, value: string}>}
     */
    function scan(text, limit) {
        var max = typeof limit === 'number' ? limit : 12;
        var items = [];
        findUrls(text).forEach(function (u) {
            items.push({ kind: 'url', value: u });
        });
        findCodes(text).forEach(function (c) {
            items.push({ kind: 'code', value: c });
        });
        return items.slice(0, max);
    }

    window.OutputScan = {
        scan: scan,
        findUrls: findUrls,
        findCodes: findCodes
    };
})();
