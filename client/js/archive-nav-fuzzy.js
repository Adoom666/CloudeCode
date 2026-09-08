/**
 * FUZZY SUBSEQUENCE MATCHING for the nav rail's filter, with the matched
 * character positions handed back so the rail can highlight them.
 *
 * WHY NOT A SUBSTRING FILTER. The rail's old filter was
 * `indexOf(needle) !== -1`, which requires the person to type a
 * contiguous run of the name. Against this corpus that fails on the
 * queries someone actually types: `infra` finds
 * `Infrastructure`, but `cloudecode` does NOT find `CloudeCode` if they
 * typed `cldcode`, and `dvtools` never finds `dev_tools/scripts`
 * although every letter is there in order. Subsequence matching accepts
 * all three. There are 77 projects, so this runs over the whole set on
 * every keystroke and the cost is irrelevant - what matters is that the
 * person is not made to guess an exact prefix.
 *
 * IT IS STILL A FILTER OVER LOADED ROWS AND SAYS SO. Fuzziness does not
 * widen the scope: this searches the rows the rail has fetched, not the
 * corpus. The sentence the rail prints next to it is unchanged, because
 * making a filter cleverer without making it broader is exactly the case
 * where a person is most likely to read it as a search.
 *
 * SCORING, and why each term is there rather than a bare "it matched":
 *
 *   - CONTIGUITY dominates. A run of adjacent characters is what a
 *     person means when they type a fragment, so `media` scoring against
 *     `Media` must beat `.dotfiles/M-e-d-i-a`-style scattered hits.
 *   - WORD-BOUNDARY starts are rewarded. Matching the `d` of `dev_tools`
 *     is worth more than the `d` inside `hidden`. Boundaries here are
 *     the real ones for these strings: `/`, `-`, `_`, `.`, ` ` and a
 *     lower->upper camel transition, so `CloudeCode` yields two.
 *   - EARLINESS breaks ties, because a hit at the front of a name is
 *     more likely the one meant.
 *   - THE FIELD MATTERS. A hit in `display_name` outranks the same hit
 *     in `full_path`: the person is looking at display names, and every
 *     full_path in this corpus starts `-Users-jsugamele-`, so paths
 *     otherwise match almost everything for queries like `user`.
 *
 * MATCHING IS CASE-INSENSITIVE, HIGHLIGHTING IS NOT. Positions are
 * returned as indices into the ORIGINAL string so the rail highlights
 * the real characters and never re-cases what it shows.
 *
 * NO DEPENDENCIES, no build step. Exports window.ArchiveNavFuzzy.
 */

console.log('[ArchiveNavFuzzy Module] Loading...');

(function () {
    'use strict';

    /** Characters after which the next character starts a new "word". */
    var BOUNDARY_CHARS = '/-_. ';

    /**
     * Description: is the character at `i` the start of a word? True at
     *   index 0, after a boundary character, and at a lower->upper camel
     *   transition.
     * Inputs: text (string) - the ORIGINAL, un-lowercased string.
     *         i (number).
     * Output: boolean.
     */
    function isBoundary(text, i) {
        if (i === 0) return true;
        var prev = text.charAt(i - 1);
        if (BOUNDARY_CHARS.indexOf(prev) !== -1) return true;
        var cur = text.charAt(i);
        return prev === prev.toLowerCase() && prev !== prev.toUpperCase()
            && cur === cur.toUpperCase() && cur !== cur.toLowerCase();
    }

    /**
     * Description: match `needle` as a subsequence of `text`, greedily
     *   left to right, and score the run.
     *
     *   The greedy walk is deliberate and its limitation is worth
     *   naming: it takes the FIRST admissible position for each needle
     *   character, which is not always the highest-scoring alignment.
     *   For a 77-row rail and queries of a few characters that
     *   difference is not observable, and an optimal alignment is a
     *   dynamic program nobody here needs. If this ever moves to a set
     *   large enough for the ranking to matter, replace the walk, not
     *   the scoring.
     *
     * Inputs: text (string) - the candidate. needle (string) - the query.
     * Output: {score: number, positions: Array<number>} on a match, or
     *   null when `needle` is not a subsequence of `text`. An EMPTY
     *   needle returns score 0 with no positions - it matches
     *   everything, which is how a cleared filter shows every row.
     * Example: match('dev_tools/scripts', 'dvtools').positions.length // 7
     */
    function match(text, needle) {
        var haystack = String(text === null || text === undefined ? '' : text);
        var query = String(needle === null || needle === undefined ? '' : needle);
        if (!query) return { score: 0, positions: [] };
        if (!haystack) return null;

        var lowerHay = haystack.toLowerCase();
        var lowerNeedle = query.toLowerCase();
        var positions = [];
        var at = 0;
        for (var n = 0; n < lowerNeedle.length; n++) {
            var found = lowerHay.indexOf(lowerNeedle.charAt(n), at);
            if (found === -1) return null;
            positions.push(found);
            at = found + 1;
        }

        var score = 0;
        for (var p = 0; p < positions.length; p++) {
            var idx = positions[p];
            // Contiguity: the dominant term, so an adjacent run wins.
            if (p > 0 && idx === positions[p - 1] + 1) score += 12;
            if (isBoundary(haystack, idx)) score += 8;
        }
        // Earliness, as a tie-break only: bounded so it can never
        // outweigh a genuinely contiguous match further along.
        score += Math.max(0, 10 - positions[0]);
        // A query that covers most of the candidate is a better answer
        // than the same query buried in a much longer string.
        score += Math.round((query.length / haystack.length) * 10);
        return { score: score, positions: positions };
    }

    /**
     * Description: score one row across several fields and keep the best
     *   field's result, so a row is ranked by its strongest evidence and
     *   the rail highlights the field it actually matched.
     * Inputs: row (object). needle (string).
     *         fields (Array<{name: string, weight: number}>).
     * Output: {score, field, positions} or null when no field matched.
     */
    function matchRow(row, needle, fields) {
        var best = null;
        for (var i = 0; i < fields.length; i++) {
            var spec = fields[i];
            var value = row && row[spec.name];
            if (typeof value !== 'string') continue;
            var hit = match(value, needle);
            if (!hit) continue;
            var weighted = hit.score * (typeof spec.weight === 'number' ? spec.weight : 1);
            if (!best || weighted > best.score) {
                best = { score: weighted, field: spec.name, positions: hit.positions };
            }
        }
        return best;
    }

    /**
     * Description: filter and RANK rows fuzzily. The rail's replacement
     *   for a substring filter.
     * Inputs: rows (Array<object>). needle (string) - '' returns every
     *           row, unranked and in order.
     *         fields (Array<{name,weight}>) - fields to search, e.g.
     *           [{name:'display_name',weight:3},{name:'full_path',weight:1}].
     * Output: Array<object> - each `{row, score, field, positions}`,
     *   best first. Ties fall back to the incoming order, so a rail
     *   built from a sorted list stays stable.
     * Example: rank(rows, 'dvtools', F)[0].row.display_name
     */
    function rank(rows, needle, fields) {
        var list = Array.isArray(rows) ? rows : [];
        var query = String(needle === null || needle === undefined ? '' : needle);
        var specs = Array.isArray(fields) && fields.length
            ? fields
            : [{ name: 'display_name', weight: 3 }, { name: 'full_path', weight: 1 }];
        if (!query) {
            return list.map(function (row) {
                return { row: row, score: 0, field: null, positions: [] };
            });
        }
        var out = [];
        for (var i = 0; i < list.length; i++) {
            var hit = matchRow(list[i], query, specs);
            if (hit) {
                out.push({
                    row: list[i], score: hit.score, field: hit.field,
                    positions: hit.positions, order: i
                });
            }
        }
        out.sort(function (a, b) {
            return b.score - a.score || a.order - b.order;
        });
        return out;
    }

    /**
     * Description: build a document fragment with the matched characters
     *   wrapped in <mark>, so highlighting never goes through innerHTML.
     *   Slugs and folder names in this corpus contain real punctuation
     *   and a U+2019; they are text, and they reach the DOM as text.
     *   Returns a SPAN rather than a DocumentFragment on purpose: a
     *   fragment is the tidier DOM object, but the rail's test harness
     *   implements only the element surface, and a renderer that cannot
     *   be tested headlessly is a renderer nobody checks. A span is
     *   equivalent here - it is inserted into the label either way.
     * Inputs: doc (Document), text (string),
     *         positions (Array<number>) - indices into `text`.
     * Output: Element - a span wrapping the marked and unmarked runs.
     */
    function highlight(doc, text, positions) {
        var frag = doc.createElement('span');
        frag.setAttribute('class', 'archive-nav__hilite');
        var source = String(text === null || text === undefined ? '' : text);
        var marks = {};
        var list = Array.isArray(positions) ? positions : [];
        for (var i = 0; i < list.length; i++) marks[list[i]] = true;
        var buffer = '';
        var inMark = false;

        function flush() {
            if (!buffer) return;
            if (inMark) {
                var m = doc.createElement('mark');
                m.setAttribute('class', 'archive-nav__hit');
                m.textContent = buffer;
                frag.appendChild(m);
            } else {
                var t = doc.createElement('span');
                t.textContent = buffer;
                frag.appendChild(t);
            }
            buffer = '';
        }

        for (var c = 0; c < source.length; c++) {
            var wanted = !!marks[c];
            if (wanted !== inMark) { flush(); inMark = wanted; }
            buffer += source.charAt(c);
        }
        flush();
        return frag;
    }

    window.ArchiveNavFuzzy = {
        match: match,
        matchRow: matchRow,
        rank: rank,
        highlight: highlight,
        isBoundary: isBoundary
    };
    console.log('[ArchiveNavFuzzy Module] Exported as window.ArchiveNavFuzzy');
})();
