/**
 * Archive keyboard map.
 *
 * ONE PURE FUNCTION DOES THE DECIDING. `resolve(event, context)` takes a
 * key event and the screen's current shape and returns an ACTION NAME or
 * null. It touches no DOM, dispatches nothing and reads no globals, so
 * every branch of the map is testable under plain Node and the binding
 * table is one thing rather than a handler spread across five files.
 *
 * THE ESCAPE LADDER IS ORDERED, AND THE ORDER IS THE WHOLE POINT.
 * Escape means "back out of the innermost thing", and the innermost
 * thing is not always the same:
 *
 *   1. a modal is open        -> the modal owns it. This function
 *                                returns null and leaves it to
 *                                modal-stack.js, which already routes
 *                                Escape to the top overlay. Two owners
 *                                for one key is how a modal closes and
 *                                the screen behind it also navigates.
 *   2. the filter has text    -> clear the filter
 *   3. a search is showing    -> dismiss the search results
 *   4. on a narrow viewport   -> go back one pane
 *   5. otherwise              -> nothing. Escape does NOT leave the
 *                                archive screen, because an accidental
 *                                Escape throwing away a 3,416-row
 *                                paging position is a hostile default.
 *
 * A KEY IS NEVER CLAIMED WHILE A TEXT FIELD HAS FOCUS, except Escape and
 * Enter. `context.inTextField` is the caller's measurement, not a guess
 * made here: a single-letter binding that fires while somebody is typing
 * into the filter is the most common way a keyboard map becomes
 * something people turn off.
 *
 * WHAT IN HERE IS PURE, STATED HONESTLY. `resolve()`, `resolveEscape()`,
 * `bindings()` and `createSelection()` are pure and DOM-free, so every
 * branch is testable under plain Node. `openHelp()` is the ONE DOM
 * function in this file, and it lives here deliberately rather than in a
 * help-panel module: the panel renders `bindings()` and nothing else, and
 * a second file is how a key map and the help describing it drift into
 * two tables that disagree. A help panel that lies is worse than none.
 *
 * `createSelection()` is the cursor the composition root drives j/k with.
 * It holds a COUNT and an INDEX and no rows at all, which is what lets a
 * selection survive its row leaving a virtualized render window: there is
 * no element for the selection to lose.
 */

console.log('[ArchiveKeys Module] Loading...');

(function () {
    'use strict';

    /**
     * Every action this map can produce. Exported so the composition
     * root binds against names rather than string literals, and so a
     * test can assert the set has not silently grown.
     * @type {Object<string,string>}
     */
    var ACTIONS = {
        NEXT_ROW: 'next-row',
        PREV_ROW: 'prev-row',
        OPEN_ROW: 'open-row',
        BACK_PANE: 'back-pane',
        FOCUS_FILTER: 'focus-filter',
        FOCUS_SEARCH: 'focus-search',
        CLEAR_FILTER: 'clear-filter',
        DISMISS_SEARCH: 'dismiss-search',
        LOAD_MORE: 'load-more',
        OPEN_EXPORT: 'open-export',
        TOGGLE_SCHEME: 'toggle-scheme',
        TOGGLE_VIEW: 'toggle-view',
        OPEN_HELP: 'open-help'
    };

    /**
     * Single-character bindings, active only when no text field has
     * focus. Kept as data rather than as a switch so the whole map is
     * one readable table and a duplicate binding is visible.
     * @type {Object<string,string>}
     */
    var PLAIN_KEYS = {
        'j': ACTIONS.NEXT_ROW,
        'k': ACTIONS.PREV_ROW,
        '/': ACTIONS.FOCUS_FILTER,
        's': ACTIONS.FOCUS_SEARCH,
        'm': ACTIONS.LOAD_MORE,
        'e': ACTIONS.OPEN_EXPORT,
        't': ACTIONS.TOGGLE_SCHEME,
        'v': ACTIONS.TOGGLE_VIEW,
        // '?' is Shift+/ and `event.key` reports the CHARACTER PRODUCED,
        // so the browser hands us a literal '?' while '/' arrives only
        // unshifted. Binding the character is the whole implementation:
        // no shiftKey branch, '/' still resolves to FOCUS_FILTER, and
        // hasCommandModifier already excludes Shift.
        '?': ACTIONS.OPEN_HELP
    };

    /**
     * Named keys, active regardless of the single-character rule where
     * noted in resolve().
     * @type {Object<string,string>}
     */
    var NAMED_KEYS = {
        'ArrowDown': ACTIONS.NEXT_ROW,
        'ArrowUp': ACTIONS.PREV_ROW,
        'Enter': ACTIONS.OPEN_ROW
    };

    /**
     * Description: whether an event carries a modifier that means it
     *   belongs to the browser or the OS rather than to this screen.
     *   Shift is deliberately NOT in this set: Shift+letter is still a
     *   letter, and claiming it would break capitalised typing nowhere
     *   while blocking nothing useful here.
     * Inputs: event (object) - {ctrlKey, metaKey, altKey}.
     * Output: boolean.
     */
    function hasCommandModifier(event) {
        return !!(event && (event.ctrlKey || event.metaKey || event.altKey));
    }

    /**
     * Description: resolve the Escape ladder. Separated out because its
     *   ORDER is the contract and it deserves to be read in one place.
     * Inputs: context (object) - see resolve().
     * Output: string|null - an ACTIONS value, or null when Escape
     *   belongs to something else (a modal) or means nothing here.
     */
    function resolveEscape(context) {
        var c = context || {};
        // Rung 1. A modal owns Escape outright. Returning an action here
        // would close the modal AND navigate the screen behind it.
        if (c.modalOpen) return null;
        if (c.filterText) return ACTIONS.CLEAR_FILTER;
        if (c.searchOpen) return ACTIONS.DISMISS_SEARCH;
        if (c.narrow && c.canGoBack) return ACTIONS.BACK_PANE;
        // Rung 5. Deliberately nothing. Escape does not leave the screen.
        return null;
    }

    /**
     * Description: map one key event to an action name.
     * Inputs: event (object) - {key, ctrlKey, metaKey, altKey}. A real
     *           KeyboardEvent works; so does a literal, which is what
     *           the tests pass.
     *         context (object) -
     *           inTextField (boolean) - focus is in an input/textarea.
     *           modalOpen (boolean)   - a modal is registered on top.
     *           filterText (string)   - current filter contents.
     *           searchOpen (boolean)  - search results are showing.
     *           narrow (boolean)      - viewport is below the one-pane
     *                                   breakpoint.
     *           canGoBack (boolean)   - a previous pane exists.
     * Output: string|null - an ACTIONS value, or null when this map
     *   claims nothing. Null is a real answer: it means the key belongs
     *   to the browser, to a text field, or to a modal, and swallowing
     *   it would be a bug rather than a no-op.
     * Example:
     *   resolve({key: 'j'}, {})                       // -> 'next-row'
     *   resolve({key: 'j'}, {inTextField: true})      // -> null
     *   resolve({key: 'Escape'}, {modalOpen: true})   // -> null
     */
    function resolve(event, context) {
        var e = event || {};
        var c = context || {};
        var key = e.key;
        if (typeof key !== 'string' || !key) return null;

        if (key === 'Escape') return resolveEscape(c);

        // A modal that is open owns every key except Escape, which the
        // branch above already handed to it. Claiming j/k under a modal
        // scrolls the list underneath while somebody reads a dialog.
        if (c.modalOpen) return null;

        if (hasCommandModifier(e)) return null;

        // Enter inside a text field submits that field; the composition
        // root decides what that means for the filter or the search box,
        // and this map does not second-guess it.
        if (key === 'Enter') {
            return c.inTextField ? null : ACTIONS.OPEN_ROW;
        }

        // Arrows work while typing: moving a selection is not typing,
        // and a person filtering a list expects to arrow into the
        // results without leaving the field.
        if (Object.prototype.hasOwnProperty.call(NAMED_KEYS, key)) {
            return NAMED_KEYS[key];
        }

        if (c.inTextField) return null;

        if (Object.prototype.hasOwnProperty.call(PLAIN_KEYS, key)) {
            return PLAIN_KEYS[key];
        }
        return null;
    }

    /**
     * Description: the human-readable binding table, for a help panel or
     *   a test that asserts every action is reachable.
     * Inputs: none.
     * Output: Array<{keys: string, action: string, note: string}>
     */
    function bindings() {
        return [
            { keys: 'j / ArrowDown', action: ACTIONS.NEXT_ROW, note: 'next row' },
            { keys: 'k / ArrowUp', action: ACTIONS.PREV_ROW, note: 'previous row' },
            { keys: 'Enter', action: ACTIONS.OPEN_ROW, note: 'open the selected row' },
            { keys: '/', action: ACTIONS.FOCUS_FILTER, note: 'focus the filter' },
            { keys: 's', action: ACTIONS.FOCUS_SEARCH, note: 'focus the search box' },
            { keys: 'm', action: ACTIONS.LOAD_MORE, note: 'load the next page' },
            { keys: 'e', action: ACTIONS.OPEN_EXPORT, note: 'open the export modal' },
            { keys: 't', action: ACTIONS.TOGGLE_SCHEME, note: 'cycle the scheme split' },
            { keys: 'v', action: ACTIONS.TOGGLE_VIEW, note: 'conversation view / raw view' },
            { keys: '?', action: ACTIONS.OPEN_HELP, note: 'show this key list' },
            { keys: 'Escape', action: ACTIONS.CLEAR_FILTER,
              note: 'clear the filter, then dismiss search, then go back one pane' }
        ];
    }

    /**
     * The index meaning "nothing is selected" - a real state with its own
     * behaviour in move(), not merely a sentinel for "empty".
     * @type {number}
     */
    var NOTHING_SELECTED = -1;

    /**
     * Description: a PURE index cursor for a virtualized list. It holds a
     *   COUNT and an INDEX and no rows whatsoever, which is what makes a
     *   selection survive its row scrolling out of the render window:
     *   there is no element for the cursor to lose. No DOM, no globals,
     *   no reference to the data it indexes.
     * Inputs: none. Output: object -
     *   count(): number, setCount(n): void, index(): number (-1 when
     *   nothing is selected), select(i): number, move(delta): number,
     *   has(): boolean.
     * Example: sel.setCount(3); sel.move(-1) -> 2, k on a fresh list
     *   selects the END; sel.setCount(9) leaves the index at 2, because
     *   paging appended rows and must not move a selection.
     */
    function createSelection() {
        var count = 0;
        var index = NOTHING_SELECTED;

        /**
         * Description: coerce a caller-supplied number, treating anything
         *   non-finite as 0 rather than letting NaN poison the cursor.
         * Inputs: value (*) - anything. Output: number - a finite integer.
         */
        function toInt(value) {
            var n = Number(value);
            return isFinite(n) ? (n < 0 ? Math.ceil(n) : Math.floor(n)) : 0;
        }

        /**
         * Description: the MOVEMENT clamp - an index into [0, count-1], or
         *   NOTHING_SELECTED when the list is empty. A negative lands on 0,
         *   because moving up past the first row means "stay there".
         * Inputs: i (number). Output: number.
         */
        function clampIntoRange(i) {
            if (count <= 0) return NOTHING_SELECTED;
            if (i < 0) return 0;
            if (i >= count) return count - 1;
            return i;
        }

        /**
         * Description: the SELECTION clamp - clampIntoRange except that a
         *   negative is an explicit clear, select(-1) being the documented
         *   way to select nothing. The two genuinely differ, so they are
         *   two functions rather than one with a flag.
         * Inputs: i (number). Output: number.
         */
        function clampOrClear(i) {
            if (i < 0) return NOTHING_SELECTED;
            return clampIntoRange(i);
        }

        return {
            /** How many rows the cursor indexes. Output: number. */
            count: function () { return count; },

            /**
             * Description: tell the cursor how many rows exist now.
             *   GROWING PRESERVES THE INDEX UNCHANGED - paging appends rows
             *   and must not move somebody's selection. Shrinking below the
             *   cursor clamps to the last index; a count of 0 clears to -1.
             * Inputs: n (number) - new row count; negatives read as 0.
             * Output: void.
             */
            setCount: function (n) {
                var next = toInt(n);
                count = next > 0 ? next : 0;
                if (count === 0) {
                    index = NOTHING_SELECTED;
                } else if (index >= count) {
                    index = count - 1;
                }
                // NOTHING_SELECTED stays: a list that gains rows from
                // empty has still had nothing selected.
            },

            /** The selected index. Output: number, -1 when none. */
            index: function () { return index; },

            /**
             * Description: select an index. Out of range clamps into range;
             *   anything negative (including -1) clears.
             * Inputs: i (number). Output: number - the index selected.
             */
            select: function (i) {
                index = clampOrClear(toInt(i));
                return index;
            },

            /**
             * Description: move the cursor by delta and return where it
             *   landed. From NOTHING_SELECTED a POSITIVE delta selects the
             *   first row and a NEGATIVE delta selects the LAST, so k on a
             *   fresh list selects the end rather than doing nothing.
             *   Clamps at both ends: NO wraparound, because wrapping from
             *   the last line of a 30,805-line transcript to the first is
             *   a hostile surprise.
             * Inputs: delta (number), may be negative.
             * Output: number - the new index.
             */
            move: function (delta) {
                var d = toInt(delta);
                if (count <= 0) return (index = NOTHING_SELECTED);
                if (d === 0) return index;
                if (index === NOTHING_SELECTED) {
                    index = d > 0 ? 0 : count - 1;
                    return index;
                }
                index = clampIntoRange(index + d);
                return index;
            },

            /** Whether anything is selected. Output: boolean. */
            has: function () { return index !== NOTHING_SELECTED; }
        };
    }

    window.ArchiveKeys = {
        resolve: resolve,
        resolveEscape: resolveEscape,
        bindings: bindings,
        createSelection: createSelection,
        // THE HELP MODAL LIVES IN archive-keys-help.js AND IS RE-EXPORTED
        // HERE. It was moved out when this file crossed the 500-line cap;
        // the DOM half of a module whose header says "pure decision
        // logic" was the honest thing to cut. Every existing caller and
        // test holds `ArchiveKeys.openHelp`, so the name stays, and the
        // delegation NAMES the missing module rather than throwing
        // "openHelp is not a function", which would report the wrong
        // cause. See the note in archive-keys-help.js.
        /**
         * Description: open the keyboard help modal.
         * Inputs: options (object) - {document, onClose}.
         * Output: {overlay, close} - see archive-keys-help.js.
         */
        openHelp: function (options) {
            if (!window.ArchiveKeysHelp) {
                throw new ReferenceError('archive-keys-help.js is not loaded, ' +
                    'so the keyboard help cannot open. Fix the script order ' +
                    'in client/index.html.');
            }
            return window.ArchiveKeysHelp.openHelp(options);
        },
        get HELP_MODAL_ATTR() {
            return window.ArchiveKeysHelp ? window.ArchiveKeysHelp.HELP_MODAL_ATTR : null;
        },
        get HELP_MODAL_NAME() {
            return window.ArchiveKeysHelp ? window.ArchiveKeysHelp.HELP_MODAL_NAME : null;
        },
        hasCommandModifier: hasCommandModifier,
        ACTIONS: ACTIONS,
        PLAIN_KEYS: PLAIN_KEYS,
        NAMED_KEYS: NAMED_KEYS
    };
    console.log('[ArchiveKeys Module] Exported as window.ArchiveKeys');
})();
