/**
 * Slash Command Filter
 *
 * Live-filter + keyboard-navigation state for the slash-commands modal
 * (Task 4). Split out of slash-commands.js to keep that file under the
 * project's 500-line cap - this module owns ONLY the filter/keyboard
 * bookkeeping over an already-rendered DOM; it doesn't fetch data, build
 * markup, or know about open/close behavior.
 */

console.log('[SlashCommandFilter Module] Loading...');

class SlashCommandFilter {
    constructor() {
        this._items = [];        // [{element, command, searchText}]
        this._visibleItems = [];
        this._activeIndex = -1;  // index into _visibleItems, -1 = none highlighted
    }

    /**
     * Description: (re)index every `.command-item` under `root` into the
     *   flat searchable list. Call once after the modal's HTML is
     *   rendered - the DOM doesn't change shape again while open, only
     *   visibility toggles, so this doesn't need to re-run per keystroke.
     * Inputs: root (Element) - the modal (or any ancestor of the
     *   `.command-item` rows).
     * Output: void.
     */
    index(root) {
        const items = root ? Array.from(root.querySelectorAll('.command-item')) : [];
        this._items = items.map(element => {
            const command = element.dataset.command || '';
            // THE FULL description, from `data-description` - NEVER the
            // rendered `.command-description` text, which is a shortened
            // DISPLAY form (client/js/command-description.js). Searching
            // the rendered string would silently drop every word in the
            // truncated-away tail, so typing a real word from a command's
            // own docs would return nothing. Fall back to the rendered
            // text only for a row that carries no data attribute at all.
            const descEl = element.querySelector('.command-description');
            const hasFull = element.dataset.description != null;
            const description = hasFull
                ? element.dataset.description
                : (descEl ? descEl.textContent : '');
            const searchText = (command + ' ' + description).toLowerCase();
            return { element, command, searchText };
        });
        this._visibleItems = this._items.slice();
        this._activeIndex = -1;
    }

    /**
     * Description: apply a live-filter query - matches command name and
     *   description, case-insensitive substring. Toggles `.filter-hidden`
     *   on non-matching `.command-item` rows and on any `.command-category`
     *   whose every child item was hidden, per Task 4 ("group headings
     *   visible only for groups with surviving matches"). Clears any
     *   keyboard highlight, since the result set just changed.
     * Inputs: query (string) - raw filter-input value.
     * Output: void.
     */
    apply(query) {
        const q = (query || '').trim().toLowerCase();
        this.clearActiveHighlight();
        this._activeIndex = -1;
        this._visibleItems = [];

        const categoriesWithMatch = new Set();
        for (const item of this._items) {
            const matches = !q || item.searchText.includes(q);
            item.element.classList.toggle('filter-hidden', !matches);
            if (matches) {
                this._visibleItems.push(item);
                const categoryEl = item.element.closest('.command-category');
                if (categoryEl) categoriesWithMatch.add(categoryEl);
            }
        }

        const allCategories = new Set(this._items.map(i => i.element.closest('.command-category')).filter(Boolean));
        allCategories.forEach(categoryEl => {
            categoryEl.classList.toggle('filter-hidden', !categoriesWithMatch.has(categoryEl));
        });
    }

    /** Remove the keyboard-active highlight from whichever item has it. */
    clearActiveHighlight() {
        if (this._activeIndex >= 0 && this._visibleItems[this._activeIndex]) {
            this._visibleItems[this._activeIndex].element.classList.remove('keyboard-active');
        }
    }

    /**
     * Description: move the keyboard-selection highlight within the
     *   currently visible (post-filter) results, wrapping the initial
     *   press to the first/last item and clamping thereafter.
     * Inputs: delta (number) - +1 (down) or -1 (up).
     * Output: void.
     */
    moveActive(delta) {
        if (this._visibleItems.length === 0) return;
        this.clearActiveHighlight();
        const next = this._activeIndex < 0
            ? (delta > 0 ? 0 : this._visibleItems.length - 1)
            : Math.min(Math.max(this._activeIndex + delta, 0), this._visibleItems.length - 1);
        this._activeIndex = next;
        const el = this._visibleItems[next].element;
        el.classList.add('keyboard-active');
        el.scrollIntoView({ block: 'nearest' });
    }

    /**
     * Description: the item Enter should select - the keyboard-highlighted
     *   one if any, otherwise the first visible result (so pressing Enter
     *   right after typing, with no arrow-key press yet, still does
     *   something useful).
     * Inputs: none.
     * Output: {element, command, searchText}|undefined.
     */
    getEnterTarget() {
        return this._activeIndex >= 0 ? this._visibleItems[this._activeIndex] : this._visibleItems[0];
    }
}

// Export singleton-per-modal-instance class (SlashCommandsModal owns one).
window.SlashCommandFilter = SlashCommandFilter;
console.log('[SlashCommandFilter Module] Exported as window.SlashCommandFilter');
