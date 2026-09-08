/**
 * The reader's DOM SHELL: the element helper, the skeleton mount()
 * builds, the ONE delegated click listener, and teardown.
 *
 * WHY THIS IS ITS OWN FILE. archive-reader.js owns the rAF loop and the
 * paint pipeline - what gets DRAWN, and when. This file owns what the
 * drawing happens INSIDE: the five nested elements, which of them
 * carries the scrollbar, which carries the honest height, and which one
 * the single event listener is bound to. Those are structural facts
 * that change for entirely different reasons than the paint logic does,
 * and keeping them here means the shell can be read without wading
 * through element construction.
 *
 * NO IntersectionObserver, AND NO PER-ROW LISTENERS. Rows are recycled
 * on every paint, so a listener bound to one would leak and go stale
 * within a frame. ONE delegated listener sits on the render window and
 * resolves its target out of the DOM. If you are ever tempted to add a
 * second listener to a row, that is the bug this file exists to
 * prevent.
 *
 * A CLICK WHOSE TARGET ROW CANNOT BE RESOLVED IS A THIRD OUTCOME, and
 * it is reported rather than guessed. Toggling a guessed index would
 * silently open somebody else's row, which looks like a feature working
 * and is not.
 *
 * TEXT NODES ONLY, NEVER innerHTML. See archive-line-render.js's header
 * for why: transcript content is arbitrary text from the corpus and
 * must never be parsed as markup.
 *
 * Exports window.ArchiveReaderDom.
 */

console.log('[ArchiveReaderDom Module] Loading...');

(function () {
    'use strict';

    /**
     * Fallback viewport height when the scroller reports clientHeight 0,
     * which happens while detached. A zero viewport would render one row
     * and look broken rather than unmounted, so the detached case is
     * NAMED with a usable number instead of silently producing one.
     * @type {number}
     */
    var FALLBACK_VIEWPORT_PX = 600;

    /**
     * Build the DOM shell helpers for one reader instance.
     *
     * @param {object} ctx the reader's own handles:
     *   `document` the owning document;
     *   `rootClass` the reader's BEM root class;
     *   `actionLoadMore` / `actionExpand` / `actionCollapse` the three
     *     data-action values the delegated listener answers to;
     *   `requestMoreLines()` the pager's single entry point;
     *   `setProgressExpanded(index, on)` toggles one progress run;
     *   `schedule()` queues a repaint (bound to the scroll event).
     *
     * NOTE mount() deliberately does NOT paint. The caller owns the
     * handles this returns and must copy them out BEFORE the first
     * render, or that render sees a null root and silently draws
     * nothing - which looks exactly like a reader with no rows.
     * @returns {object} {el, mount, onWindowClick, handles, destroy}
     *   where `handles()` returns the built elements and `destroy()`
     *   unbinds and detaches.
     * @example
     *   var dom = ArchiveReaderDom.createShell(ctx);
     *   var root = dom.mount(hostElement);
     *   var scroller = dom.handles().scroller;
     */
    function createShell(ctx) {
        var doc = ctx.document;
        var ROOT_CLASS = ctx.rootClass;

        // All null until mount(). `window_` is the translated render
        // window; `spacer` carries the honest height.
        var root = null, scroller = null, spacer = null, window_ = null;
        var statusEl = null;

        /**
         * Create an element. Text nodes only, never innerHTML; see
         * archive-line-render.js's header for why.
         * @param {string} tag @param {?string} cls @param {?string} text
         * @returns {Element}
         */
        function el(tag, cls, text) {
            var e = doc.createElement(tag);
            if (cls) e.className = cls;
            if (text !== null && text !== undefined) {
                e.appendChild(doc.createTextNode(String(text)));
            }
            return e;
        }

        /**
         * Viewport height, with the detached case named rather than
         * silently producing a one-row window.
         * @returns {number} pixels
         */
        function viewportHeight() {
            var h = scroller && scroller.clientHeight;
            return Number.isFinite(h) && h > 0 ? h : FALLBACK_VIEWPORT_PX;
        }

        /**
         * Current scroll offset, 0 while detached.
         * @returns {number} pixels
         */
        function scrollTop() {
            var t = scroller && scroller.scrollTop;
            return Number.isFinite(t) ? t : 0;
        }

        /**
         * THE ONLY CLICK LISTENER IN THE READER. Resolves the action and
         * the owning row from the DOM, because the rows themselves are
         * recycled and cannot safely carry listeners.
         * @param {object} ev a click event @returns {void}
         */
        function onWindowClick(ev) {
            var target = ev && ev.target;
            if (!target || typeof target.closest !== 'function') return;
            var hit = target.closest('[data-action]');
            if (!hit) return;
            var action = hit.getAttribute('data-action');
            if (action === ctx.actionLoadMore) { ctx.requestMoreLines(); return; }
            if (action !== ctx.actionExpand && action !== ctx.actionCollapse) return;

            var row = hit.closest('[data-index]');
            var raw = row ? row.getAttribute('data-index') : null;
            var idx = parseInt(raw, 10);
            if (!Number.isInteger(idx)) {
                // COULD NOT EVALUATE which run was meant. Toggling a
                // guessed index would move somebody else's row.
                console.error('[ArchiveReader] ' + action + ' clicked but the ' +
                    'owning row carries no usable data-index (saw: ' +
                    String(raw) + '). Nothing toggled.');
                return;
            }
            ctx.setProgressExpanded(idx, action === ctx.actionExpand);
        }

        /**
         * Attach the reader to a host element.
         * @param {Element} host @returns {Element} the reader root
         */
        function mount(host) {
            root = el('section', ROOT_CLASS, null);
            root.setAttribute('data-reader', 'archive');
            root.appendChild(el('div', ROOT_CLASS + '__header', null));

            statusEl = el('div', ROOT_CLASS + '__status', null);
            root.appendChild(statusEl);

            scroller = el('div', ROOT_CLASS + '__scroller', null);
            scroller.setAttribute('tabindex', '0');
            spacer = el('div', ROOT_CLASS + '__spacer', null);
            window_ = el('div', ROOT_CLASS + '__window', null);
            spacer.appendChild(window_);
            scroller.appendChild(spacer);
            root.appendChild(scroller);

            scroller.addEventListener('scroll', ctx.schedule);
            window_.addEventListener('click', onWindowClick);
            host.appendChild(root);
            return root;
        }

        return {
            el: el,
            mount: mount,
            viewportHeight: viewportHeight,
            scrollTop: scrollTop,
            onWindowClick: onWindowClick,
            /**
             * The built elements. Every field is null before mount().
             * @returns {object} {root, scroller, spacer, window_, statusEl}
             */
            handles: function () {
                return { root: root, scroller: scroller, spacer: spacer,
                    window_: window_, statusEl: statusEl };
            },
            /**
             * Unbind both listeners and detach the root. Idempotent.
             * @returns {void}
             */
            destroy: function () {
                if (scroller) scroller.removeEventListener('scroll', ctx.schedule);
                if (window_) window_.removeEventListener('click', onWindowClick);
                if (root && root.parentNode) root.parentNode.removeChild(root);
                root = null;
            }
        };
    }

    window.ArchiveReaderDom = {
        createShell: createShell,
        FALLBACK_VIEWPORT_PX: FALLBACK_VIEWPORT_PX
    };
    console.log('[ArchiveReaderDom Module] Exported as window.ArchiveReaderDom');
})();
