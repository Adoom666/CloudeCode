/**
 * Draggable, remembered widths for the archive's two pane dividers.
 *
 * THE SIZES LIVE IN TWO CSS CUSTOM PROPERTIES AND NOWHERE ELSE.
 * `--archive-nav-w` and `--archive-list-w` are written on the grid
 * element, and `archive-screen.css` builds `grid-template-columns` out
 * of them. That is the whole coupling: this file never writes a
 * `grid-template-columns` string of its own. Two places computing a
 * template is how the narrow layout and the wide layout end up
 * disagreeing about how many columns exist - the media query below 900px
 * overrides the template to a single column and the variables are simply
 * ignored, which is why the narrow layout needs no code here at all.
 *
 * THE HANDLES ARE `position: absolute`, SO THEY ARE NOT GRID ITEMS.
 * Putting them in the flow would make the grid five columns and every
 * rule that says "the three columns" wrong. Out of flow they can also
 * overlay the pane borders exactly, which is where a person aims.
 *
 * `localStorage` IS THE RIGHT STORE AND ITS FAILURE IS NOT AN ERROR.
 * A pane width is per-viewer, survives a reload, and losing it costs a
 * drag. It is read and written inside try/catch because a private
 * window, blocked site data, or a browser configured to throw on access
 * are all real, and in every one of those the screen must render at the
 * DEFAULTS rather than not render. THREE OUTCOMES, and they are kept
 * apart: `restored` (a usable value was read), `default` (the store
 * answered and held nothing), and `unavailable` (the store could not be
 * consulted at all). The third is reported by `storeState()` rather than
 * being laundered into the second, because "nobody has dragged yet" and
 * "this browser will never remember" are different facts and only one of
 * them is worth telling a person about.
 *
 * A STORED VALUE IS CLAMPED, NOT TRUSTED. It was written against some
 * other viewport - a 1600px window's list width parks the reader off
 * screen at 1024px - so every value is re-clamped against the CURRENT
 * grid width on load AND on resize. A number that fails to parse is
 * discarded rather than coerced: `parseFloat` of a corrupted entry
 * yields NaN, and NaN silently becomes a zero-width pane.
 *
 * Exports window.ArchivePaneResize.
 */

console.log('[ArchivePaneResize Module] Loading...');

(function () {
    'use strict';

    /** localStorage key. Versioned, so a future shape change is a miss
     *  rather than a misparse of the old one. @type {string} */
    var STORE_KEY = 'cloude.archive.panes.v1';

    /** Default widths in px, matching the `17rem 22rem` this replaced at
     *  the app's 16px root. Named here AND in archive-screen.css's
     *  fallbacks; the two must agree. @type {{nav: number, list: number}} */
    var DEFAULTS = { nav: 272, list: 352 };

    /** Smallest each pane may be dragged to, in px. The rail's is set by
     *  its filter input plus a two-line host label; the list's by one
     *  metadata line without wrapping; the reader's by the 90ch body
     *  measure it is built around. @type {object} */
    var MIN = { nav: 160, list: 220, reader: 320 };

    /** Below this width the grid is one column and dragging is
     *  meaningless. Mirrors NARROW_MAX_PX in archive-screen.js and the
     *  900px media query in archive-screen.css. @type {number} */
    var NARROW_MAX_PX = 900;

    /** The two dividers, in column order. `before` is the pane the
     *  handle resizes. @type {Array<object>} */
    var DIVIDERS = [
        { key: 'nav', label: 'Resize the navigation rail' },
        { key: 'list', label: 'Resize the transcript list' }
    ];

    /**
     * Description: read the stored widths. Never throws.
     * Inputs: storage (Storage|null) - localStorage or a stand-in.
     * Output: {state: string, value: object|null} - state is 'restored',
     *   'default' or 'unavailable'; value is the parsed widths or null.
     * Example: readStore(window.localStorage)
     *          // -> {state: 'restored', value: {nav: 300, list: 400}}
     */
    function readStore(storage) {
        var raw;
        try {
            if (!storage) return { state: 'unavailable', value: null };
            raw = storage.getItem(STORE_KEY);
        } catch (err) {
            // Accessing localStorage THROWS outright in some
            // configurations - it is not merely empty. Reported, not
            // swallowed, because it is the difference between "not saved
            // yet" and "will never save".
            console.warn('ArchivePaneResize: localStorage could not be read (' +
                (err && err.message) + '). Rendering at the default widths.');
            return { state: 'unavailable', value: null };
        }
        if (raw === null || raw === undefined || raw === '') {
            return { state: 'default', value: null };
        }
        var parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (err2) {
            console.warn('ArchivePaneResize: stored pane widths are not JSON. ' +
                'Rendering at the default widths.');
            return { state: 'default', value: null };
        }
        var nav = numberOrNull(parsed && parsed.nav);
        var list = numberOrNull(parsed && parsed.list);
        if (nav === null || list === null) return { state: 'default', value: null };
        return { state: 'restored', value: { nav: nav, list: list } };
    }

    /**
     * Description: the width a DRAG is asking for, which is a different
     *   question from whether a STORED value is usable.
     *
     *   THE TWO MUST NOT SHARE A PARSER, and conflating them was a real
     *   bug caught by test_archive_pane_resize.node.mjs: `numberOrNull`
     *   rejects anything <= 0, so a drag all the way to the left edge
     *   (width 0, or negative once past it) read as "no value supplied"
     *   and jumped the pane back to its DEFAULT - the rail visibly
     *   springing from under the cursor to 272px mid-drag. A drag to
     *   zero is a real request for zero; the answer is to pin it at the
     *   MINIMUM, which the caller does. Only a value that is not a number
     *   at all falls back to the default.
     * Inputs: v (*), fallback (number). Output: number - always finite.
     */
    function requested(v, fallback) {
        var n = typeof v === 'number' ? v : parseFloat(v);
        if (typeof n !== 'number' || !isFinite(n)) return fallback;
        return n;
    }

    /**
     * Description: a finite positive number, or null. NaN and Infinity
     *   are rejected rather than coerced - a NaN width collapses a pane
     *   to nothing with no error anywhere. Used for STORED values, where
     *   a zero or a negative is corruption rather than an intention.
     * Inputs: v (*). Output: number|null.
     */
    function numberOrNull(v) {
        var n = typeof v === 'number' ? v : parseFloat(v);
        if (typeof n !== 'number' || !isFinite(n) || n <= 0) return null;
        return n;
    }

    /**
     * Description: persist the widths. Never throws; a failure to write
     *   leaves the on-screen layout exactly as the person dragged it.
     * Inputs: storage (Storage|null), widths ({nav, list}).
     * Output: boolean - whether it was actually stored.
     */
    function writeStore(storage, widths) {
        try {
            if (!storage) return false;
            storage.setItem(STORE_KEY, JSON.stringify({
                nav: Math.round(widths.nav), list: Math.round(widths.list)
            }));
            return true;
        } catch (err) {
            console.warn('ArchivePaneResize: pane widths could not be saved (' +
                (err && err.message) + '). The layout still applies for this view.');
            return false;
        }
    }

    /**
     * Description: force a pair of widths inside the minimums for a grid
     *   of `total` px. PURE, so the rule a drag enforces and the rule a
     *   restore enforces are one function and cannot drift.
     *
     *   The reader's minimum is enforced by shrinking whichever pane the
     *   caller is NOT currently dragging, and the nav's minimum wins over
     *   the reader's if the viewport is too small to satisfy both - a
     *   pane pinned to its floor is recoverable, a pane at zero is not.
     * Inputs: widths ({nav, list}), total (number) - the grid's px width.
     * Output: {nav: number, list: number} - a NEW object.
     * Example: clamp({nav: 50, list: 900}, 1000) // -> {nav:160, list:520}
     */
    function clamp(widths, total) {
        var nav = Math.max(MIN.nav, requested(widths && widths.nav, DEFAULTS.nav));
        var list = Math.max(MIN.list, requested(widths && widths.list, DEFAULTS.list));
        if (!isFinite(total) || total <= 0) return { nav: nav, list: list };
        // The rail never takes more than the space that leaves the other
        // two at their floors.
        nav = Math.min(nav, Math.max(MIN.nav, total - MIN.list - MIN.reader));
        list = Math.min(list, Math.max(MIN.list, total - nav - MIN.reader));
        return { nav: nav, list: list };
    }

    /**
     * Description: build the resizer for one archive grid.
     * Inputs: options (object) -
     *   document (Document), grid (Element) - `.archive-screen__grid`,
     *   rootClass (string), storage (Storage|null|undefined) - defaults
     *     to window.localStorage, read through a try/catch too because
     *     merely REACHING for it throws in some configurations.
     * Output: {apply, reset, widths, storeState, handles, element}
     * Example: ArchivePaneResize.create({document: document, grid: g,
     *              rootClass: 'archive-screen'})
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document;
        var grid = opts.grid;
        if (!doc || !grid) throw new Error('ArchivePaneResize.create needs a document and a grid');
        var rootClass = opts.rootClass || 'archive-screen';
        var storage = null;
        try {
            storage = Object.prototype.hasOwnProperty.call(opts, 'storage')
                ? opts.storage
                : (typeof window !== 'undefined' ? window.localStorage : null);
        } catch (err) {
            storage = null;
        }

        var restored = readStore(storage);
        var storeState = restored.state;
        var widths = clamp(restored.value || DEFAULTS, gridWidth());
        var handles = [];

        /** Description: the grid's px width, or 0 when it cannot be
         *  measured (a vm harness with no layout). Output: number. */
        function gridWidth() {
            if (typeof grid.getBoundingClientRect !== 'function') return 0;
            var r = grid.getBoundingClientRect();
            return r && typeof r.width === 'number' ? r.width : 0;
        }

        /** Description: write the two variables and reposition the
         *  handles. The ONLY place either is written. Output: void. */
        function paint() {
            grid.style.setProperty('--archive-nav-w', widths.nav + 'px');
            grid.style.setProperty('--archive-list-w', widths.list + 'px');
            for (var i = 0; i < handles.length; i++) {
                var h = handles[i];
                var at = h.key === 'nav' ? widths.nav : widths.nav + widths.list;
                h.el.style.left = at + 'px';
                h.el.setAttribute('aria-valuenow', String(Math.round(
                    h.key === 'nav' ? widths.nav : widths.list)));
            }
        }

        /**
         * Description: set both widths, clamped, painted and persisted.
         * Inputs: next ({nav, list}), persist (boolean).
         * Output: {nav, list} - what was actually applied, which is not
         *   necessarily what was asked for.
         */
        function apply(next, persist) {
            widths = clamp(next, gridWidth());
            paint();
            if (persist !== false) writeStore(storage, widths);
            return { nav: widths.nav, list: widths.list };
        }

        /** Description: go back to the shipped defaults and forget the
         *  stored ones. Output: {nav, list}. */
        function reset() {
            try {
                if (storage) storage.removeItem(STORE_KEY);
            } catch (err) { /* Already reported by writeStore's path. */ }
            widths = clamp(DEFAULTS, gridWidth());
            paint();
            return { nav: widths.nav, list: widths.list };
        }

        /**
         * Description: build one divider handle. It is a real focusable
         *   `separator` with arrow-key support, not a bare div: a
         *   pointer-only affordance is unusable by keyboard and
         *   invisible to a screen reader, and `Home` is the discoverable
         *   way back to the default that a double-click alone is not.
         * Inputs: def (object) - a DIVIDERS entry. Output: Element.
         */
        function buildHandle(def) {
            var h = doc.createElement('div');
            h.setAttribute('class', rootClass + '__resizer');
            h.setAttribute('data-resize', def.key);
            h.setAttribute('role', 'separator');
            h.setAttribute('tabindex', '0');
            h.setAttribute('aria-orientation', 'vertical');
            h.setAttribute('aria-label', def.label);
            h.setAttribute('aria-valuemin', String(MIN[def.key]));
            h.setAttribute('title', def.label +
                '. Drag, or use the arrow keys. Double-click or press Home to reset.');
            wirePointer(h, def.key);
            wireKeys(h, def.key);
            h.addEventListener('dblclick', function () { reset(); });
            return h;
        }

        /**
         * Description: pointer dragging, via POINTER CAPTURE. Capture is
         *   what makes a drag survive the pointer crossing an iframe, a
         *   scrollbar or the window edge; a document-level mousemove
         *   listener silently stops tracking at those boundaries and
         *   leaves the handle stuck to the cursor.
         * Inputs: h (Element), key (string). Output: void.
         */
        function wirePointer(h, key) {
            var from = null;
            h.addEventListener('pointerdown', function (ev) {
                from = { x: ev.clientX, nav: widths.nav, list: widths.list };
                if (typeof h.setPointerCapture === 'function' && ev.pointerId !== undefined) {
                    try { h.setPointerCapture(ev.pointerId); } catch (e) { /* not fatal */ }
                }
                h.setAttribute('data-dragging', 'true');
                if (typeof ev.preventDefault === 'function') ev.preventDefault();
            });
            h.addEventListener('pointermove', function (ev) {
                if (!from) return;
                var d = ev.clientX - from.x;
                // Dragging the FIRST divider moves the rail's edge and
                // leaves the list's width alone, so the list does not
                // jump sideways under the cursor. Dragging the SECOND
                // moves the list's edge for the same reason.
                apply(key === 'nav'
                    ? { nav: from.nav + d, list: from.list }
                    : { nav: from.nav, list: from.list + d }, false);
            });
            function end() {
                if (!from) return;
                from = null;
                h.removeAttribute('data-dragging');
                writeStore(storage, widths);
            }
            h.addEventListener('pointerup', end);
            h.addEventListener('pointercancel', end);
        }

        /** Description: arrow keys nudge, Home resets. Inputs: h, key.
         *  Output: void. */
        function wireKeys(h, key) {
            h.addEventListener('keydown', function (ev) {
                var step = ev.shiftKey ? 40 : 8;
                var d = ev.key === 'ArrowLeft' ? -step : ev.key === 'ArrowRight' ? step : 0;
                if (ev.key === 'Home') { reset(); ev.preventDefault(); return; }
                if (d === 0) return;
                apply(key === 'nav'
                    ? { nav: widths.nav + d, list: widths.list }
                    : { nav: widths.nav, list: widths.list + d }, true);
                ev.preventDefault();
            });
        }

        for (var i = 0; i < DIVIDERS.length; i++) {
            var el = buildHandle(DIVIDERS[i]);
            handles.push({ key: DIVIDERS[i].key, el: el });
            grid.appendChild(el);
        }
        paint();

        // A viewport change can invalidate a stored width without any
        // interaction at all, so the clamp is re-run rather than trusted
        // from load time. Not persisted: a temporary narrow window must
        // not overwrite the width the person chose on a wide one.
        if (typeof window !== 'undefined' && window.addEventListener) {
            window.addEventListener('resize', function () {
                if (typeof window.innerWidth === 'number' &&
                        window.innerWidth < NARROW_MAX_PX) return;
                apply(widths, false);
            });
        }

        return {
            element: grid,
            apply: apply,
            reset: reset,
            /** Description: the applied widths. Output: {nav, list}. */
            widths: function () { return { nav: widths.nav, list: widths.list }; },
            /** Description: 'restored', 'default' or 'unavailable' - the
             *  three outcomes of consulting the store, kept apart.
             *  Output: string. */
            storeState: function () { return storeState; },
            /** Description: the handle elements, for tests. Output: Array. */
            handles: function () { return handles.slice(); }
        };
    }

    window.ArchivePaneResize = {
        create: create,
        clamp: clamp,
        requested: requested,
        readStore: readStore,
        writeStore: writeStore,
        STORE_KEY: STORE_KEY,
        DEFAULTS: DEFAULTS,
        MIN: MIN,
        DIVIDERS: DIVIDERS,
        NARROW_MAX_PX: NARROW_MAX_PX
    };
    console.log('[ArchivePaneResize Module] Exported as window.ArchivePaneResize');
})();
