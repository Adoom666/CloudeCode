/**
 * Subsequence ("fuzzy") matching for the archive's per-column filters.
 *
 * WHY SUBSEQUENCE AND NOT SUBSTRING. The three columns this filters are
 * a session title, a session_ref and a timestamp, and every one of them
 * is a string a person remembers the SHAPE of rather than a span of.
 * Measured against the live corpus 2026-09-01: a ref like
 * `ee039f7f-cfac-4688-86dc-30a4e28483bb` is recalled as "ee03...83bb",
 * and a timestamp `2026-08-29 18:28:32` as "0829 1828". Neither is a
 * contiguous substring of its own row, so an exact-substring matcher
 * answers zero for both while the row is sitting on screen.
 *
 * THE SCORE IS ORDINAL, NOT A PERCENTAGE, AND IT IS NEVER RENDERED. It
 * exists only to order rows against each other within one query. Four
 * things earn points, in the order they matter:
 *   1. CONSECUTIVE runs - `ee03` matched as one run beats the same four
 *      characters scattered across the string, because a run is almost
 *      always what the person meant.
 *   2. A match at a WORD BOUNDARY (string start, or after a separator in
 *      `- _ . : / space`), which is where people start typing.
 *   3. An EARLY first match, so `journal` ranks a row whose title starts
 *      with it above one that merely contains it late.
 *   4. A SHORTER haystack, breaking ties toward the more specific row.
 *
 * NO MATCH IS `null`, NEVER A ZERO SCORE. Zero is a legitimate score for
 * a real but poor match, so a caller testing `if (score)` on a numeric
 * miss would silently drop a matching row. The two answers have two
 * shapes and cannot be confused.
 *
 * AN EMPTY QUERY MATCHES EVERYTHING WITH NO SPANS. It is not a match of
 * nothing and it is not an error - a filter nobody has typed into is not
 * a filter, and returning null for it would empty the list on first
 * paint.
 *
 * CASE IS FOLDED FOR MATCHING AND NEVER FOR DISPLAY. Spans are indices
 * into the ORIGINAL string, so the caller highlights the bytes that are
 * actually on screen. Folding for display would rewrite a UUID.
 *
 * Pure. No DOM, no fetch, no globals beyond the export.
 */

console.log('[ArchiveFuzzy Module] Loading...');

(function () {
    'use strict';

    /** Characters after which a position counts as a word boundary.
     *  These are the separators the three filtered columns actually use:
     *  hyphens in a UUID, colons and spaces in a timestamp, dots and
     *  slashes in a path-derived title. @type {string} */
    var BOUNDARY_CHARS = ' -_./:\\\t';

    /** Points for each character matched immediately after the previous
     *  match. The largest weight, because a run is the strongest signal
     *  that the query is a real fragment. @type {number} */
    var CONSECUTIVE_BONUS = 8;

    /** Points for a character matched at a word boundary. @type {number} */
    var BOUNDARY_BONUS = 6;

    /** Points every matched character earns regardless. @type {number} */
    var BASE_PER_CHAR = 1;

    /** Penalty per character skipped before the FIRST match, capped so a
     *  long haystack cannot drive the score arbitrarily negative and
     *  swamp the bonuses above. @type {number} */
    var LEADING_PENALTY = 1;

    /** Cap on the leading penalty. @type {number} */
    var LEADING_PENALTY_MAX = 20;

    /**
     * Description: is the character at `i` the start of a word?
     * Inputs: text (string), i (number) - index into text.
     * Output: boolean - true at index 0 and after any BOUNDARY_CHARS.
     * Example: isBoundary('a-b', 2)  // -> true
     */
    function isBoundary(text, i) {
        if (i <= 0) return true;
        return BOUNDARY_CHARS.indexOf(text.charAt(i - 1)) !== -1;
    }

    /**
     * Description: match `query` as a subsequence of `text`, greedily
     *   left to right, and score the result.
     *
     *   THE GREEDY WALK IS DELIBERATE AND ITS LIMIT IS STATED. It takes
     *   the FIRST position for each query character rather than searching
     *   every assignment for the best-scoring one, so a pathological
     *   query can score lower than an optimal matcher would. That is
     *   accepted: the walk is O(len(text)) with no allocation, it runs
     *   over every loaded row on every keystroke, and being one rank out
     *   on a rare input is a far smaller cost than a filter that stutters.
     *   It never changes WHETHER a row matches - only its order.
     *
     * Inputs: query (string) - what was typed, may be empty.
     *         text (string|null|undefined) - the row's value for a column.
     * Output: {score: number, spans: Array<[number, number]>} on a match,
     *         where each span is [startInclusive, endExclusive) into the
     *         ORIGINAL `text`. `null` when the query is not a
     *         subsequence. An empty query returns score 0 and no spans.
     * Example: match('e483bb', 'ee039f7f-30a4e28483bb')
     *          // -> {score: 39, spans: [[13, 14], [16, 21]]}
     */
    function match(query, text) {
        var q = typeof query === 'string' ? query : '';
        var hay = (text === null || text === undefined) ? '' : String(text);
        if (q.length === 0) return { score: 0, spans: [] };
        if (hay.length === 0) return null;

        var ql = q.toLowerCase();
        var hl = hay.toLowerCase();
        var spans = [];
        var score = 0;
        var qi = 0;
        var prevMatched = -2;
        var firstAt = -1;

        for (var hi = 0; hi < hl.length && qi < ql.length; hi++) {
            if (hl.charAt(hi) !== ql.charAt(qi)) continue;
            if (firstAt === -1) firstAt = hi;
            score += BASE_PER_CHAR;
            if (hi === prevMatched + 1) {
                score += CONSECUTIVE_BONUS;
                // Extend the open span rather than opening a second one,
                // so a run highlights as one <mark> and not as N.
                spans[spans.length - 1][1] = hi + 1;
            } else {
                if (isBoundary(hay, hi)) score += BOUNDARY_BONUS;
                spans.push([hi, hi + 1]);
            }
            prevMatched = hi;
            qi++;
        }

        if (qi < ql.length) return null;
        score -= Math.min(firstAt * LEADING_PENALTY, LEADING_PENALTY_MAX);
        // Shorter haystack wins a tie: it is the more specific row.
        score -= Math.min(hay.length / 100, 5);
        return { score: score, spans: spans };
    }

    /**
     * Description: split `text` into alternating unmatched/matched runs,
     *   ready for a renderer to wrap the matched ones. Returns SEGMENTS
     *   rather than markup so this file stays free of the DOM and the
     *   caller keeps control of the element and class it emits.
     * Inputs: text (string|null|undefined), spans (Array<[number,number]>).
     * Output: Array<{text: string, hit: boolean}> - in order, no empty
     *   segments, covering the whole string exactly once.
     * Example: segments('abcd', [[1, 3]])
     *          // -> [{text:'a',hit:false},{text:'bc',hit:true},{text:'d',hit:false}]
     */
    function segments(text, spans) {
        var hay = (text === null || text === undefined) ? '' : String(text);
        var list = Array.isArray(spans) ? spans : [];
        var out = [];
        var at = 0;
        for (var i = 0; i < list.length; i++) {
            var s = list[i][0];
            var e = list[i][1];
            if (s > at) out.push({ text: hay.slice(at, s), hit: false });
            if (e > s) out.push({ text: hay.slice(s, e), hit: true });
            at = e;
        }
        if (at < hay.length) out.push({ text: hay.slice(at), hit: false });
        return out;
    }

    /**
     * Description: filter and rank rows against a map of per-column
     *   queries. A row must match EVERY non-empty query (AND, not OR) -
     *   the columns narrow together, which is what a per-column filter
     *   row means to the person using it.
     * Inputs: rows (Array<object>) - the loaded rows, untouched.
     *         queries (Object<string,string>) - column key -> typed text.
     *         valueOf (function(row, columnKey): string|null) - reads one
     *           column out of one row. Passed in so this file knows
     *           nothing about the row shape.
     * Output: Array<{row: object, score: number, spans: Object<string,
     *   Array<[number,number]>>, rank: number}> - highest score first,
     *   ties broken by the row's ORIGINAL order so the list does not
     *   reshuffle under equal scores. `rank` is the row's index in the
     *   input, kept so a caller can prove stability.
     * Example: rank(rows, {ref: 'e48'}, function (r, k) { return r[k]; })
     */
    function rank(rows, queries, valueOf) {
        var list = Array.isArray(rows) ? rows : [];
        var keys = Object.keys(queries || {}).filter(function (k) {
            return typeof queries[k] === 'string' && queries[k].length > 0;
        });
        var out = [];
        for (var i = 0; i < list.length; i++) {
            var total = 0;
            var spans = {};
            var ok = true;
            for (var j = 0; j < keys.length; j++) {
                var m = match(queries[keys[j]], valueOf(list[i], keys[j]));
                if (m === null) { ok = false; break; }
                total += m.score;
                spans[keys[j]] = m.spans;
            }
            if (ok) out.push({ row: list[i], score: total, spans: spans, rank: i });
        }
        out.sort(function (a, b) {
            if (b.score !== a.score) return b.score - a.score;
            return a.rank - b.rank;
        });
        return out;
    }

    /**
     * Description: is any column being filtered? Distinguishes "nobody
     *   has typed anything" from "a query matched nothing", which render
     *   as different things and must never be merged.
     * Inputs: queries (Object<string,string>|null).
     * Output: boolean.
     */
    function isActive(queries) {
        var keys = Object.keys(queries || {});
        for (var i = 0; i < keys.length; i++) {
            if (typeof queries[keys[i]] === 'string' && queries[keys[i]].length > 0) {
                return true;
            }
        }
        return false;
    }

    window.ArchiveFuzzy = {
        match: match,
        segments: segments,
        rank: rank,
        isActive: isActive,
        isBoundary: isBoundary,
        CONSECUTIVE_BONUS: CONSECUTIVE_BONUS,
        BOUNDARY_BONUS: BOUNDARY_BONUS
    };
    console.log('[ArchiveFuzzy Module] Exported as window.ArchiveFuzzy');
})();
