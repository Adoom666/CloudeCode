/**
 * Shared machinery for a small menu hung off a floating action button.
 * ----------------------------------------------------------------------
 * TWO CONTROLS, ONE MECHANISM. The terminal screen carries two
 * session-scoped FAB menus and they are deliberately NOT the same menu:
 *
 *   - #terminalToolsBtn  "tools"          getting content in and out of
 *                                          the terminal: copy output,
 *                                          paste from clipboard, attach
 *                                          image (terminal-tools-menu.js)
 *   - #sessionEditorBtn  "session editor" configuring the session
 *                                          itself: theme and music
 *                                          (session-editor-menu.js)
 *
 * What they share is only the plumbing: build a 16x16 icon, build a 44px
 * row, open anchored to the trigger, close on Escape or an outside
 * pointerdown, wire the trigger once. That plumbing lived twice and the
 * copies drifted - the same failure the FAB geometry tokens exist to
 * stop. It lives here once, and each menu declares only its own rows.
 *
 * ROWS ARE BUILT FRESH ON EVERY OPEN so a row that reflects live state
 * (the music opt-in) reports the current value rather than one captured
 * at wire time.
 *
 * FEEDBACK THAT CANNOT HIDE. A row that reports a result used to raise
 * the terminal status pill, which is `position: fixed; top: 16px;
 * z-index: 70`. Two things paint over it: the sticky app header
 * (z-index 1000), which occupies exactly that top band, and any overlay
 * a row opens (the copy sheet at 10000, either fab menu at 10001). So a
 * row could report "paste unavailable on this connection" and the user
 * would see nothing at all - which is what the paste row did over plain
 * http. copy-output.js already grew its own in-sheet status line for the
 * same reason; the lesson never reached the menu rows.
 *
 * notify() below is the fix, and it lives here rather than in any one
 * caller so the next row added cannot reintroduce the bug. It paints at
 * NOTICE_Z, declared in JS because the invariant "above every chrome
 * layer" is what matters, not the number, and a test can only assert it
 * where it is written once.
 */

console.log('[FabMenu Module] Loading...');

(function () {
    'use strict';

    /**
     * Stacking order for the shared notice, and the reason it is a
     * constant in JS rather than only a CSS declaration: every other
     * layer in the app is below it and a test asserts that, so the
     * number has to be readable from one place.
     *
     * Above .fab-menu (10001), the copy sheet (10000), the paste
     * fallback (10002) and the sticky header (1000).
     *
     * @type {number}
     */
    var NOTICE_Z = 10005;

    /** How long a notice stays up, by kind, in milliseconds. */
    var NOTICE_MS = { error: 5000, info: 3000, success: 3000 };

    /** The live notice element, or null. Only ever one. */
    var noticeEl = null;

    /** Timer id for the pending auto-dismiss, or null. */
    var noticeTimer = null;

    /**
     * Report a row's result somewhere the user can actually see it,
     * whether or not the menu that owns the row is still open and
     * whether or not the row opened an overlay of its own.
     *
     * Prefer this over the terminal status pill for ANY message raised
     * from a menu row. The pill is fine for terminal-initiated messages
     * (an upload finishing) because nothing is covering it then.
     *
     * @param {string} message - lowercase user-facing text.
     * @param {string} [kind] - 'info' (default), 'success' or 'error'.
     * @returns {HTMLElement|null} the notice, or null with no document.
     */
    function notify(message, kind) {
        if (!message) return null;
        var level = NOTICE_MS[kind] ? kind : 'info';

        if (!noticeEl || !noticeEl.parentNode) {
            noticeEl = document.createElement('div');
            noticeEl.setAttribute('id', 'fabMenuNotice');
            noticeEl.className = 'fab-menu-notice';
            noticeEl.setAttribute('role', 'status');
            noticeEl.setAttribute('aria-live', 'polite');
            document.body.appendChild(noticeEl);
        }
        // Written inline as well as in the stylesheet. The stylesheet is
        // the styling; this is the invariant, and it must hold even if a
        // later rule forgets it.
        noticeEl.style.zIndex = String(NOTICE_Z);
        noticeEl.textContent = message;
        noticeEl.setAttribute('data-kind', level);
        noticeEl.classList.add('visible');

        if (noticeTimer) clearTimeout(noticeTimer);
        noticeTimer = setTimeout(function () {
            noticeTimer = null;
            if (noticeEl) noticeEl.classList.remove('visible');
        }, NOTICE_MS[level]);
        return noticeEl;
    }

    /**
     * Take the notice down now, for a caller that has replaced it with
     * feedback of its own (the paste fallback does this when it opens).
     *
     * @returns {void}
     */
    function dismissNotice() {
        if (noticeTimer) {
            clearTimeout(noticeTimer);
            noticeTimer = null;
        }
        if (noticeEl) noticeEl.classList.remove('visible');
    }

    /**
     * Build one 16x16 icon in the shared terminal-tool geometry.
     *
     * The stroke width is declared per path by the caller's icon body, on
     * purpose. A presentation attribute on the path beats a `stroke-width`
     * rule that targets the `svg`, so a stylesheet cannot be trusted to
     * normalise these - the value has to be right in the markup.
     *
     * @param {Object<string, string>} icons - name to inner SVG markup.
     * @param {string} name - a key of `icons`.
     * @returns {SVGElement|null} the icon, or null for an unknown name or
     *   a document with no createElementNS.
     */
    function buildIcon(icons, name) {
        var body = icons ? icons[name] : null;
        if (!body) return null;
        if (typeof document.createElementNS !== 'function') return null;
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', '16');
        svg.setAttribute('height', '16');
        svg.setAttribute('viewBox', '0 0 16 16');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('aria-hidden', 'true');
        svg.innerHTML = body;
        return svg;
    }

    /**
     * Build one menu row: icon, label, and a click that closes first.
     *
     * @param {object} spec
     * @param {string} spec.id - the row's stable id, read by the tests.
     * @param {SVGElement|null} spec.icon - a built icon, or null.
     * @param {string} spec.label - lowercase user-facing text.
     * @param {Function} spec.onPick - invoked after the menu closes, with
     *   notify() as its only argument. Any result the row reports MUST
     *   go through that rather than the terminal status pill, which the
     *   header and every overlay paint over.
     * @param {Function} spec.close - the owning menu's close().
     * @returns {HTMLButtonElement}
     */
    function buildRow(spec) {
        var item = document.createElement('button');
        item.type = 'button';
        item.setAttribute('id', spec.id);
        item.className = 'fab-menu__item';
        item.setAttribute('role', 'menuitem');

        if (spec.icon) item.appendChild(spec.icon);

        var text = document.createElement('span');
        text.className = 'fab-menu__label';
        text.textContent = spec.label;
        item.appendChild(text);

        item.addEventListener('click', function (e) {
            e.stopPropagation();
            spec.close();
            spec.onPick(notify);
        });
        return item;
    }

    /**
     * Create one FAB menu controller. Each call owns its own trigger,
     * its own element and its own dismiss handlers, so two controllers
     * never fight over one another's state.
     *
     * @param {object} config
     * @param {string} config.menuId - the id given to the open element.
     * @param {string} config.menuClass - an extra class beside `fab-menu`.
     * @param {string} config.ariaLabel - the menu's accessible name.
     * @param {Function} config.rows - called per open with the controller
     *   and expected to return HTMLButtonElement[].
     * @returns {object} {open, close, isOpen, wire, trigger, item}
     */
    function create(config) {
        /** The open menu element, or null. Only ever one per controller. */
        var menuEl = null;

        /** The trigger button, or null until wire() runs. */
        var triggerEl = null;

        /** Document-level dismiss handlers, bound only while open. */
        var onDocPointer = null;
        var onDocKey = null;

        /**
         * True while this menu is on screen.
         * @returns {boolean}
         */
        function isOpen() {
            return menuEl !== null;
        }

        /**
         * Close the menu if open. Safe to call when it is not.
         * @returns {void}
         */
        function close() {
            if (onDocPointer) {
                document.removeEventListener('pointerdown', onDocPointer, true);
                onDocPointer = null;
            }
            if (onDocKey) {
                document.removeEventListener('keydown', onDocKey, true);
                onDocKey = null;
            }
            if (menuEl) {
                menuEl.remove();
                menuEl = null;
            }
            if (triggerEl) triggerEl.setAttribute('aria-expanded', 'false');
        }

        /**
         * Build one row of this menu.
         *
         * @param {string} id - the row's stable id.
         * @param {SVGElement|null} icon - a built icon, or null.
         * @param {string} label - lowercase user-facing text.
         * @param {Function} onPick - invoked after the menu closes.
         * @returns {HTMLButtonElement}
         */
        function item(id, icon, label, onPick) {
            return buildRow({
                id: id, icon: icon, label: label, onPick: onPick, close: close
            });
        }

        /**
         * Open the menu anchored to the trigger. A second call re-opens it
         * with fresh state rather than stacking two menus.
         * @returns {void}
         */
        function open() {
            close();
            if (!triggerEl) return;

            menuEl = document.createElement('div');
            menuEl.className = 'fab-menu ' + config.menuClass;
            menuEl.setAttribute('id', config.menuId);
            menuEl.setAttribute('role', 'menu');
            menuEl.setAttribute('aria-label', config.ariaLabel);
            config.rows(controller).forEach(function (r) {
                menuEl.appendChild(r);
            });

            document.body.appendChild(menuEl);
            if (window.AnchorPopover) window.AnchorPopover.place(menuEl, triggerEl);
            triggerEl.setAttribute('aria-expanded', 'true');

            // Deferred one tick so the tap that opened the menu does not
            // immediately close it. Taps on the trigger are excluded
            // because its own click handler already toggles.
            onDocPointer = function (e) {
                if (!menuEl) return;
                if (menuEl.contains(e.target)) return;
                if (triggerEl && triggerEl.contains(e.target)) return;
                close();
            };
            onDocKey = function (e) {
                if (e.key === 'Escape') close();
            };
            setTimeout(function () {
                document.addEventListener('pointerdown', onDocPointer, true);
                document.addEventListener('keydown', onDocKey, true);
            }, 0);
        }

        /**
         * Wire the trigger. Idempotent: the click handler is attached
         * once, so a session swap re-calling this cannot double-bind and
         * toggle twice.
         *
         * @param {HTMLElement|null} btn - the FAB.
         * @returns {void}
         */
        function wire(btn) {
            if (!btn) return;
            triggerEl = btn;
            if (btn._fabMenuWired) return;
            btn._fabMenuWired = true;
            btn.setAttribute('aria-expanded', 'false');
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (isOpen()) {
                    close();
                } else {
                    open();
                }
            });
        }

        /**
         * The trigger this controller is wired to, or null.
         * @returns {HTMLElement|null}
         */
        function trigger() {
            return triggerEl;
        }

        var controller = {
            open: open,
            close: close,
            isOpen: isOpen,
            wire: wire,
            item: item,
            trigger: trigger,
            notify: notify
        };
        return controller;
    }

    window.FabMenu = {
        create: create,
        buildIcon: buildIcon,
        notify: notify,
        dismissNotice: dismissNotice,
        NOTICE_Z: NOTICE_Z
    };
})();

console.log('[FabMenu Module] Exported as window.FabMenu');
