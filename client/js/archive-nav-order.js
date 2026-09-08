/**
 * The ORDER the project rail is in, and the control that changes it.
 *
 * WHY TIME IS THE DEFAULT AND NOT THE NAME. The owner asked for it in
 * those words - "we should be looking at projects in time order but we
 * should be able to change the order" - and the data agrees. Measured on
 * the live corpus 2026-09-02, 77 merged projects: the newest MESSAGE
 * timestamp inside each project spreads across nine months, 2025-12 to
 * 2026-08. The other candidate, when this tool INGESTED the files, puts
 * all 80 project rows on two days in late August, so it would render 56
 * projects as equally recent and call that an ordering. The server
 * therefore bubbles up the message timestamp; see
 * src/core/message_activity.py for both measurements.
 *
 * THREE OUTCOMES, AND THEY SORT DIFFERENTLY FROM EACH OTHER.
 * `activity_status` on each node is one of:
 *
 *   'known'   - a real timestamp. Sorts by it.
 *   'none'    - MEASURED absence. The project's transcripts were read
 *               and not one of their messages carries a timestamp.
 *   'unknown' - NOT MEASURED. The database could not answer.
 *
 * The last two are the ones it is tempting to collapse, and collapsing
 * them is the bug this whole file is arranged around. A project sorted
 * to the bottom because we could not read its date is indistinguishable,
 * from the rail, from one that is genuinely ancient - and the bottom of
 * a most-recent-first list IMPLIES a date. So both undated classes are
 * pulled OUT of the time ordering entirely and parked together at the
 * end in a deliberate block, each carrying a visible marker that says
 * which of the two it is. They are never interleaved with dated rows at
 * a position that would assert a date for them.
 *
 * They keep that placement in BOTH time directions, on purpose. Under
 * "oldest first" the instinct is to move the undated ones to the top,
 * since unknown-and-probably-old belongs there - but that is exactly the
 * inference the rail is not entitled to make. Undated means undated in
 * either direction, so they stay at the end and the end never means a
 * date.
 *
 * A note on comparing timestamps as strings: these are ISO-8601 stamps
 * stored byte-exactly as the producer wrote them, and for that format
 * lexical order IS chronological order. Parsing them into Date objects
 * would invent a precision and a timezone reading the archive never
 * claimed, and Date parsing of a malformed stamp yields NaN, which
 * compares false against everything and would silently scatter rows.
 *
 * PERSISTENCE IS A CONVENIENCE, NOT STATE. The choice lives in
 * localStorage: it is per-viewer, harmless to lose, and never read back
 * by anything but this file. Every read and write is wrapped, because
 * localStorage THROWS rather than returning null in a private window and
 * under some site-data policies - so an unwrapped read is not a missing
 * preference, it is a rail that does not render at all.
 *
 * Exports window.ArchiveNavOrder.
 */

console.log('[ArchiveNavOrder Module] Loading...');

(function () {
    'use strict';

    /** Where the choice is remembered. Namespaced like the app's other keys. */
    var STORAGE_KEY = 'cloude.archive.projectOrder';

    /** Status tokens the SERVER sets. Mirrored from message_activity.py. */
    var KNOWN = 'known';
    var NONE = 'none';
    var UNKNOWN = 'unknown';

    /**
     * The orderings, in the order they appear in the control. `id` is
     * what is persisted, so these strings are a stored format and must
     * not be renamed casually - an unrecognised stored id falls back to
     * the default rather than rendering an empty rail.
     *
     * `size` sorts on session_count and NOT transcript_count, because
     * sessions is the number the owner said he cares about and the two
     * differ by roughly 14x (1,451 uuid-scheme against 21,039 total,
     * measured 2026-09-02). A node whose session count could not be
     * established sorts with the undated block for the same reason a
     * missing date does: it has no measured value to place it by.
     */
    var MODES = [
        { id: 'recent', label: 'Recent first', kind: 'time', dir: -1 },
        { id: 'oldest', label: 'Oldest first', kind: 'time', dir: 1 },
        { id: 'name', label: 'Name (A-Z)', kind: 'name', dir: 1 },
        { id: 'size', label: 'Most sessions', kind: 'size', dir: -1 }
    ];

    /** The mode used when nothing is stored, or what is stored is not a mode. */
    var DEFAULT_MODE = 'recent';

    /**
     * Description: is this id one of the modes? Pure. Used instead of a
     *   truthiness check so a stored value from a future build cannot
     *   silently become a comparator nobody wrote.
     * Inputs: id (any).
     * Output: boolean.
     * Example: isMode('recent') -> true
     */
    function isMode(id) {
        for (var i = 0; i < MODES.length; i++) {
            if (MODES[i].id === id) return true;
        }
        return false;
    }

    /**
     * Description: the mode record for an id, or the default's record.
     *   Never returns null, so no caller has to handle a null comparator.
     * Inputs: id (string).
     * Output: object - one of MODES.
     * Example: modeFor('nope').id -> 'recent'
     */
    function modeFor(id) {
        var fallback = MODES[0];
        for (var i = 0; i < MODES.length; i++) {
            if (MODES[i].id === id) return MODES[i];
            if (MODES[i].id === DEFAULT_MODE) fallback = MODES[i];
        }
        return fallback;
    }

    /**
     * Description: the stored choice, or the default. NEVER THROWS.
     *   localStorage can throw on ACCESS (a private window, a browser
     *   set to block site data) and not merely return null, so the
     *   property lookup itself is inside the try - reading it outside
     *   and only wrapping getItem would still take the rail down.
     * Inputs: store (Storage|undefined) - injectable for tests; defaults
     *   to window.localStorage.
     * Output: {mode: string, source: 'stored'|'default'|'unavailable'} -
     *   three outcomes, so a caller can tell "he chose the default" from
     *   "we could not find out what he chose".
     * Example: readMode().mode // 'recent'
     */
    function readMode(store) {
        var raw = null;
        try {
            var s = store !== undefined ? store
                : (typeof window !== 'undefined' ? window.localStorage : null);
            if (!s) return { mode: DEFAULT_MODE, source: 'unavailable' };
            raw = s.getItem(STORAGE_KEY);
        } catch (err) {
            return { mode: DEFAULT_MODE, source: 'unavailable' };
        }
        if (raw === null || raw === undefined) {
            return { mode: DEFAULT_MODE, source: 'default' };
        }
        if (!isMode(raw)) return { mode: DEFAULT_MODE, source: 'default' };
        return { mode: raw, source: 'stored' };
    }

    /**
     * Description: remember the choice. NEVER THROWS - a failed write is
     *   reported as false and nothing else happens, because losing a
     *   preference must not cost the person the click he just made.
     * Inputs: mode (string); store (Storage|undefined).
     * Output: boolean - whether it was actually written.
     * Example: writeMode('name') -> true
     */
    function writeMode(mode, store) {
        if (!isMode(mode)) return false;
        try {
            var s = store !== undefined ? store
                : (typeof window !== 'undefined' ? window.localStorage : null);
            if (!s) return false;
            s.setItem(STORAGE_KEY, mode);
            return true;
        } catch (err) {
            return false;
        }
    }

    /**
     * Description: does this node have a value the chosen mode can sort
     *   on? Pure. This is the ONE place "sortable" is decided, so the
     *   comparator and the marker cannot disagree about which rows are
     *   in the ordered block and which are parked.
     * Inputs: node (object); kind (string) - 'time' | 'name' | 'size'.
     * Output: boolean.
     * Example: hasKey({activity_status: 'none'}, 'time') -> false
     */
    function hasKey(node, kind) {
        var n = node || {};
        if (kind === 'time') {
            return n.activity_status === KNOWN &&
                typeof n.newest_activity_at === 'string' &&
                n.newest_activity_at !== '';
        }
        if (kind === 'size') {
            return n.session_counted !== false &&
                typeof n.session_count === 'number';
        }
        // A name is present for every project whose observed_cwd the
        // server could read; a null display_name is the same class of
        // absence as a null date and is parked the same way.
        return typeof n.display_name === 'string' && n.display_name !== '';
    }

    /**
     * Description: why this node has no sort key, in the words the rail
     *   shows. Three outcomes for time, because 'none' and 'unknown' are
     *   different findings and a person acting on the rail needs to know
     *   which one he is looking at. Pure.
     * Inputs: node (object); kind (string).
     * Output: {short: string, title: string}.
     * Example: unsortedReason({activity_status:'unknown'}, 'time').short
     *   // 'date not established'
     */
    function unsortedReason(node, kind) {
        var n = node || {};
        if (kind === 'time') {
            if (n.activity_status === UNKNOWN) {
                return {
                    short: 'date not established',
                    title: 'This project has no position in a time ordering ' +
                           'because its date could not be established - not ' +
                           'because it is old. It is parked here rather than ' +
                           'sorted to an end that would imply a date.'
                };
            }
            return {
                short: 'no dated messages',
                title: 'Measured: this project\'s transcripts were read and ' +
                       'none of their messages carries a timestamp. That is ' +
                       'an answer, not a failure to look - but it is not a ' +
                       'date, so it is parked rather than sorted.'
            };
        }
        if (kind === 'size') {
            return {
                short: 'session count not established',
                title: 'The number of sessions in this project could not be ' +
                       'measured, so it is parked rather than sorted as zero.'
            };
        }
        return {
            short: 'no name derived',
            title: 'This project\'s observed_cwd is null, so no folder name ' +
                   'could be derived from it and there is nothing to sort by.'
        };
    }

    /**
     * Description: split nodes into the ones the mode can order and the
     *   ones it cannot. Pure, and it returns BOTH halves - a caller that
     *   only got the sortable half would render fewer projects than
     *   exist and have no way to notice.
     * Inputs: nodes (Array); kind (string).
     * Output: {sortable: Array, parked: Array}.
     * Example: partition(nodes, 'time').parked.length
     */
    function partition(nodes, kind) {
        var list = Array.isArray(nodes) ? nodes : [];
        var sortable = [];
        var parked = [];
        for (var i = 0; i < list.length; i++) {
            (hasKey(list[i], kind) ? sortable : parked).push(list[i]);
        }
        return { sortable: sortable, parked: parked };
    }

    /**
     * Description: the comparator for one mode. Pure and TOTAL - it
     *   returns a number for every pair, and falls back to the display
     *   name then the full path so the order is STABLE across repaints
     *   rather than depending on Array.prototype.sort's tie handling.
     * Inputs: mode (object) - a MODES record.
     * Output: function(a, b) -> number.
     */
    function comparatorFor(mode) {
        return function (a, b) {
            var primary = 0;
            if (mode.kind === 'time') {
                var av = a.newest_activity_at, bv = b.newest_activity_at;
                primary = av < bv ? -1 : (av > bv ? 1 : 0);
            } else if (mode.kind === 'size') {
                primary = a.session_count - b.session_count;
            } else {
                var an = String(a.display_name || '').toLowerCase();
                var bn = String(b.display_name || '').toLowerCase();
                primary = an < bn ? -1 : (an > bn ? 1 : 0);
            }
            if (primary !== 0) return primary * mode.dir;
            var at = String(a.display_name || '').toLowerCase();
            var bt = String(b.display_name || '').toLowerCase();
            if (at !== bt) return at < bt ? -1 : 1;
            var ap = String(a.full_path || ''), bp = String(b.full_path || '');
            return ap < bp ? -1 : (ap > bp ? 1 : 0);
        };
    }

    /**
     * Description: order the nodes, appending the ones with no sort key
     *   in a marked block at the end. Pure - it never mutates the input
     *   array, because the rail holds that array as its own state and a
     *   sort in place would make the stored order depend on which
     *   control was touched last.
     * Inputs: nodes (Array); modeId (string).
     * Output: {nodes: Array, ordered: number, parked: Array<object>,
     *          mode: string} - `parked` carries {node, reason} so the
     *   renderer marks each row with the reason that put it there.
     * Example: sortNodes(nodes, 'recent').nodes[0].display_name
     */
    function sortNodes(nodes, modeId) {
        var mode = modeFor(isMode(modeId) ? modeId : DEFAULT_MODE);
        var split = partition(nodes, mode.kind);
        var ordered = split.sortable.slice().sort(comparatorFor(mode));
        // The parked block gets a stable order of its own - by name -
        // so it does not reshuffle between repaints, but that order is
        // NOT the mode's and is not presented as meaningful.
        var parked = split.parked.slice().sort(function (a, b) {
            var an = String(a.display_name || a.full_path || '').toLowerCase();
            var bn = String(b.display_name || b.full_path || '').toLowerCase();
            return an < bn ? -1 : (an > bn ? 1 : 0);
        });
        var reasons = [];
        for (var i = 0; i < parked.length; i++) {
            reasons.push({
                node: parked[i],
                reason: unsortedReason(parked[i], mode.kind)
            });
        }
        return {
            nodes: ordered.concat(parked),
            ordered: ordered.length,
            parked: reasons,
            mode: mode.id
        };
    }


    /**
     * Description: the date cell on a project row. Renders the DAY only,
     *   taken as the first 10 characters of the stored ISO-8601 stamp -
     *   NOT parsed into a Date, because parsing would apply the viewer's
     *   timezone to a stamp the archive stores byte-exactly as its
     *   producer wrote it, and would silently yield 'Invalid Date' for a
     *   stamp that is not the shape we assumed. Three outcomes, matching
     *   the server's activity_status. Pure.
     * Inputs: row (object) - a merged project node.
     * Output: {text: string, title: string, known: boolean} | null when
     *   the row carries no activity fields at all (every non-project
     *   row, and any build whose server predates them).
     * Example: activityCell({activity_status: 'known',
     *   newest_activity_at: '2026-08-30T16:01:02Z'}).text // '2026-08-30'
     */
    function activityCell(row) {
        var r = row || {};
        if (typeof r.activity_status !== 'string') return null;
        if (r.activity_status === KNOWN &&
            typeof r.newest_activity_at === 'string' && r.newest_activity_at) {
            return {
                known: true,
                text: r.newest_activity_at.slice(0, 10),
                title: 'Last worked in ' + r.newest_activity_at +
                       '\n(the newest message timestamp in this project, ' +
                       'not when it was collected)'
            };
        }
        if (r.activity_status === UNKNOWN) {
            return {
                known: false, text: 'no date',
                title: 'The date could not be established for this project. ' +
                       'That is not the same as it being old.'
            };
        }
        return {
            known: false, text: 'undated',
            title: 'Measured: this project has transcripts, and none of ' +
                   'their messages carries a timestamp.'
        };
    }

    /**
     * Description: build the order control. A REAL <select>, not a div
     *   dressed as one - it inherits the platform's keyboard handling,
     *   its focus ring and its screen-reader semantics for free, and the
     *   rail already uses a real <select> for its machine filter, so
     *   this matches what is there rather than imitating it.
     * Inputs: doc (Document); options ({mode, onChange}).
     * Output: {element, select, value, set} - `element` is the labelled
     *   wrapper to append; `select` is the control itself.
     * Example: ArchiveNavOrder.mount(document, {onChange: fn}).element
     */
    function mount(doc, options) {
        var opts = options || {};
        var wrap = doc.createElement('div');
        wrap.className = 'archive-nav__order';

        var label = doc.createElement('label');
        label.className = 'archive-nav__order-label';
        label.setAttribute('for', 'archive-nav-order');
        label.textContent = 'Order';

        var select = doc.createElement('select');
        select.className = 'archive-nav__order-select';
        select.id = 'archive-nav-order';
        select.setAttribute('aria-label', 'Order the project list');
        for (var i = 0; i < MODES.length; i++) {
            var opt = doc.createElement('option');
            opt.setAttribute('value', MODES[i].id);
            opt.textContent = MODES[i].label;
            select.appendChild(opt);
        }
        var initial = isMode(opts.mode) ? opts.mode : readMode().mode;
        select.value = initial;

        select.addEventListener('change', function () {
            var next = isMode(select.value) ? select.value : DEFAULT_MODE;
            writeMode(next);
            if (typeof opts.onChange === 'function') opts.onChange(next);
        });

        wrap.appendChild(label);
        wrap.appendChild(select);
        return {
            element: wrap,
            select: select,
            value: function () {
                return isMode(select.value) ? select.value : DEFAULT_MODE;
            },
            set: function (mode) {
                if (!isMode(mode)) return false;
                select.value = mode;
                return true;
            }
        };
    }

    window.ArchiveNavOrder = {
        mount: mount,
        sortNodes: sortNodes,
        readMode: readMode,
        writeMode: writeMode,
        partition: partition,
        hasKey: hasKey,
        unsortedReason: unsortedReason,
        activityCell: activityCell,
        isMode: isMode,
        MODES: MODES,
        DEFAULT_MODE: DEFAULT_MODE,
        STORAGE_KEY: STORAGE_KEY
    };
    console.log('[ArchiveNavOrder Module] Exported as window.ArchiveNavOrder');
})();
