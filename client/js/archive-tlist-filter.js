/**
 * The transcript list's FILTER HEADER: the compact scheme chooser and
 * the three per-column fuzzy inputs.
 *
 * SUPERSEDED: THE SCHEME CHOOSER IS NOW A REAL `<select>`. This file
 * used to argue for a hand-built trigger-plus-menu over a native
 * `<select>`, on the grounds that the options carried an `aria-pressed`
 * contract a `<select>` cannot grow. The owner overruled it on sight:
 * "i dont like the dropdown its fake and doesnt match." He was right
 * about the thing the argument never addressed - a div dressed as a
 * select inherits none of the platform's control behaviour and none of
 * the app's own form styling, so it reads as a foreign object in its own
 * header no matter how correct its ARIA is.
 *
 * WHAT WAS TRADED, STATED RATHER THAN GLOSSED. `aria-pressed="false"` on
 * the inactive options is gone, because a native `<option>` has no such
 * attribute. What replaces it is the platform's own selected-option
 * semantics, which every screen reader already announces, plus a
 * `data-scheme-active` on the select itself so a test can still assert
 * the active choice without reading `value`. That is a real difference
 * and it is a smaller one than an unstyled fake control.
 *
 * THE TWO FILTERS HAVE DIFFERENT SCOPES AND MUST NEVER BE DESCRIBED IN
 * ONE SENTENCE. The scheme filter is SERVER-side: it re-queries the
 * whole project, so its counts are scope counts. The fuzzy filter is
 * CLIENT-side over the rows ALREADY FETCHED, and it cannot see a row on
 * a page nobody has loaded. Merging the two notes would make one of them
 * a lie in whichever direction the reader happened to guess. They are
 * rendered as two separate lines, each naming its own scope, and the
 * fuzzy line says out loud when more pages exist.
 *
 * NOTHING HERE FETCHES, PAGES OR HOLDS ROWS. It emits changes through
 * two callbacks and renders what it is told. The stateful list owns the
 * rows and the cursor; this owns a header.
 *
 * Exports window.ArchiveTlistFilter.
 */

console.log('[ArchiveTlistFilter Module] Loading...');

(function () {
    'use strict';

    /**
     * The three fuzzy columns, in the order they are drawn. `key` is the
     * name a caller reads a row's value by; nothing here knows the row
     * shape beyond passing this string back.
     * @type {Array<{key: string, label: string, placeholder: string}>}
     */
    var COLUMNS = [
        { key: 'title', label: 'Name', placeholder: 'filter by name' },
        { key: 'ref', label: 'Ref', placeholder: 'filter by ref' },
        { key: 'date', label: 'Date', placeholder: 'filter by date' }
    ];

    /**
     * Description: build the filter header.
     * Inputs: options (object) -
     *   document (Document), rootClass (string),
     *   schemeDefs (Array<{v, label, hint}>), scheme (string) - the one
     *     active at build time, onScheme (function(value)),
     *   onQuery (function(queries)) - called on every keystroke with the
     *     full column -> text map.
     * Output: {element, setScheme, queries, setNote, clearQueries}
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document;
        if (!doc) throw new Error('ArchiveTlistFilter.create needs a document');
        var rootClass = opts.rootClass || 'archive-tlist';
        var defs = opts.schemeDefs || [];
        var scheme = opts.scheme;
        var onScheme = typeof opts.onScheme === 'function' ? opts.onScheme : function () {};
        var onQuery = typeof opts.onQuery === 'function' ? opts.onQuery : function () {};

        var queries = {};
        var optionButtons = [];
        var root = el('div', rootClass + '__filters', null);
        var select = null;
        var note = el('p', rootClass + '__fuzzy-note', null);

        /** Description: element with a class and optional text.
         *  Inputs: tag, cls, text. Output: Element. */
        function el(tag, cls, text) {
            var n = doc.createElement(tag);
            if (cls) n.setAttribute('class', cls);
            if (text !== null && text !== undefined) n.textContent = String(text);
            return n;
        }

        /** Description: the label of the active scheme.
         *  Inputs: none. Output: string. */
        function activeLabel() {
            for (var i = 0; i < defs.length; i++) {
                if (defs[i].v === scheme) return defs[i].label;
            }
            // An unrecognised value is NAMED, not silently shown as the
            // first option - a control that displays a choice nobody made
            // is how a filter becomes untrustworthy.
            return 'UNKNOWN FILTER (' + String(scheme) + ')';
        }

        /** Description: write the active scheme onto the select, its
         *  data attribute and its options. Inputs: none. Output: void. */
        function paintPressed() {
            if (!select) return;
            // `value` is the browser's own record of the choice;
            // `data-scheme-active` is this view's, and they are written
            // together so a test never has to read one and trust the
            // other. An UNRECOGNISED scheme leaves `value` unset by the
            // platform - a <select> cannot hold a value no <option>
            // carries - so the attribute is the only place that fact
            // survives, and activeLabel() names it in the title.
            select.value = String(scheme);
            select.setAttribute('data-scheme-active', String(scheme));
            select.setAttribute('title',
                'Showing: ' + activeLabel() + '. Changes which kind of ' +
                'transcript this list asks the server for.');
            for (var i = 0; i < optionButtons.length; i++) {
                var o = optionButtons[i];
                var on = o.getAttribute('data-scheme-filter') === scheme;
                if (on) o.setAttribute('selected', 'selected');
                else o.removeAttribute('selected');
            }
        }

        /** Description: build the scheme chooser as a REAL form control,
         *  so it inherits the platform's behaviour and the app's own
         *  control styling instead of imitating both.
         *  Inputs: none. Output: Element. */
        function buildScheme() {
            var box = el('div', rootClass + '__scheme-box', null);
            var lab = el('label', rootClass + '__scheme-label', 'Showing');
            lab.setAttribute('for', rootClass + '-scheme');
            select = doc.createElement('select');
            select.setAttribute('class', rootClass + '__scheme');
            select.setAttribute('id', rootClass + '-scheme');
            select.setAttribute('aria-label', 'Session reference scheme filter');
            for (var i = 0; i < defs.length; i++) {
                select.appendChild(buildOption(defs[i]));
            }
            select.addEventListener('change', function () {
                var v = select.value;
                setScheme(v);
                onScheme(v);
            });
            box.appendChild(lab);
            box.appendChild(select);
            return box;
        }

        /** Description: one <option>. The hint stays as its title, which
         *  is where it already was; a native option cannot hold more.
         *  Inputs: def. Output: Element. */
        function buildOption(def) {
            var o = el('option', rootClass + '__scheme-option', def.label);
            o.setAttribute('value', def.v);
            o.setAttribute('data-scheme-filter', def.v);
            if (def.hint) o.setAttribute('title', def.hint);
            optionButtons.push(o);
            return o;
        }

        /** Description: build the three fuzzy inputs. Inputs: none.
         *  Output: Element. */
        function buildFuzzy() {
            var box = el('div', rootClass + '__fuzzy', null);
            box.setAttribute('role', 'group');
            box.setAttribute('aria-label',
                'Filter the rows already loaded, by name, ref or date');
            for (var i = 0; i < COLUMNS.length; i++) {
                box.appendChild(buildInput(COLUMNS[i]));
            }
            return box;
        }

        /** Description: one column input. Inputs: col. Output: Element. */
        function buildInput(col) {
            var input = el('input', rootClass + '__fuzzy-input', null);
            input.setAttribute('type', 'search');
            input.setAttribute('data-fuzzy-column', col.key);
            input.setAttribute('placeholder', col.placeholder);
            input.setAttribute('title',
                'Fuzzy filter on ' + col.label.toLowerCase() +
                '. Matches characters in order, not as one block, and only' +
                ' across the rows already loaded into this list.');
            input.setAttribute('aria-label',
                'Filter loaded rows by ' + col.label.toLowerCase());
            input.addEventListener('input', function () {
                queries[col.key] = input.value || '';
                onQuery(readQueries());
            });
            return input;
        }

        /** Description: a copy of the typed queries. Inputs: none.
         *  Output: Object<string,string>. */
        function readQueries() {
            var out = {};
            for (var i = 0; i < COLUMNS.length; i++) {
                out[COLUMNS[i].key] = queries[COLUMNS[i].key] || '';
            }
            return out;
        }

        /** Description: record a scheme without emitting a change, so
         *  the caller can reflect a value it set itself. Inputs: value.
         *  Output: void. */
        function setScheme(value) {
            scheme = value;
            paintPressed();
        }

        /**
         * Description: write the fuzzy filter's OWN honesty line. It
         *   describes the rows fetched so far and NOTHING else, and it
         *   states the unknown case as an unknown: `hasMore === true`
         *   means there are more pages, `null` means the server did not
         *   say, and those are three different sentences.
         * Inputs: shown (number) - rows after filtering, loaded (number)
         *   - rows fetched, hasMore (boolean|null).
         * Output: void.
         */
        function setNote(shown, loaded, hasMore) {
            if (!isActive()) {
                note.textContent = '';
                return;
            }
            var line = 'Name/ref/date filter: ' + shown + ' of the ' + loaded +
                ' rows LOADED SO FAR match. This filter runs in the browser over' +
                ' the rows already fetched - it does not ask the server, and it' +
                ' cannot see a row on a page nobody has loaded.';
            if (hasMore === true) {
                line += ' There ARE more pages in this scope; load them to filter them.';
            } else if (hasMore !== false) {
                line += ' Whether more pages exist: NOT KNOWN, so rows may be' +
                    ' missing from what this filter can reach.';
            }
            note.textContent = line;
        }

        /** Description: is any column being filtered? Inputs: none.
         *  Output: boolean. */
        function isActive() {
            return !!(window.ArchiveFuzzy && window.ArchiveFuzzy.isActive(readQueries()));
        }

        root.appendChild(buildScheme());
        root.appendChild(buildFuzzy());
        root.appendChild(note);
        paintPressed();

        return {
            element: root,
            setScheme: setScheme,
            setNote: setNote,
            isActive: isActive,
            /** Description: the typed queries. Output: Object. */
            queries: readQueries,
            /** Description: the <option> elements, for tests. Output: Array. */
            options: function () { return optionButtons.slice(); },
            /** Description: the scheme <select>, for tests. `trigger` is
             *  kept as its name because every existing caller and test
             *  holds it, and renaming it would have been a second change
             *  riding along on this one. Output: Element. */
            trigger: function () { return select; },
            /** Description: the scheme <select>. Output: Element. */
            select: function () { return select; },
            /** Description: the note element, for tests. Output: Element. */
            note: function () { return note; }
        };
    }

    window.ArchiveTlistFilter = { create: create, COLUMNS: COLUMNS };
    console.log('[ArchiveTlistFilter Module] Exported as window.ArchiveTlistFilter');
})();
