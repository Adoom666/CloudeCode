/**
 * Paste fallback - the user hands us the clipboard when the page cannot
 * read it.
 * ----------------------------------------------------------------------
 * WHY THIS EXISTS. navigator.clipboard.read() and readText() are gated on
 * a SECURE CONTEXT. `localhost` is exempt; a LAN address like
 * http://10.0.1.150:8000/ is not, and neither is a plain-http hostname.
 * On those origins `navigator.clipboard` is simply undefined in Chrome
 * and Firefox, and no browser offers a read fallback - not execCommand,
 * not a permission prompt, nothing. This is a platform rule, not a bug we
 * can code around, so the "paste from clipboard" row over http had
 * nowhere to go and reported an error the header then painted over.
 *
 * THE INVERSION. Reading the clipboard is forbidden; RECEIVING a paste is
 * not. A `paste` event fired into a focused, editable element carries
 * `event.clipboardData` on any origin, because the user performing the
 * paste gesture IS the permission. So we stop asking for the clipboard
 * and give the user somewhere to put it.
 *
 * ONE INJECTION PATH. Nothing here writes to the websocket. The caller
 * passes clipboard.js's own injectText() in, so the bracketed-paste
 * handling and the single-frame send are identical to the secure-context
 * path by construction rather than by two implementations agreeing.
 *
 * IMAGES TOO. `event.clipboardData.items` carries image blobs on an
 * insecure origin, which navigator.clipboard.read() cannot. The paste
 * listener below routes the first image item into the same
 * Terminal#_uploadAndInjectFile() the secure path and the attach-file
 * row use, so pasting a screenshot over plain http works rather than
 * silently arriving as empty text.
 */

console.log('[PasteFallback Module] Loading...');

(function () {
    'use strict';

    /**
     * Stacking order. Above the fab menus (10001) and the copy sheet
     * (10000); below FabMenu.NOTICE_Z (10005) so a result reported while
     * this is open still lands on top of it.
     */
    var OVERLAY_Z = 10002;

    /** The open overlay, or null. Only ever one. */
    var overlayEl = null;

    /** The textarea inside the open overlay, or null. */
    var inputEl = null;

    /** Escape handler bound only while open. */
    var onDocKey = null;

    /**
     * True while the fallback is on screen.
     * @returns {boolean}
     */
    function isOpen() {
        return overlayEl !== null;
    }

    /**
     * Take the overlay down without injecting anything. Safe to call
     * when it is not open. Both Escape and the cancel button land here,
     * and so does a successful insert once it has already injected.
     *
     * @returns {void}
     */
    function close() {
        if (onDocKey) {
            document.removeEventListener('keydown', onDocKey, true);
            onDocKey = null;
        }
        if (overlayEl) {
            overlayEl.remove();
            overlayEl = null;
        }
        inputEl = null;
    }

    /**
     * Report a result where it can be seen. Routed through the shared
     * menu notice rather than the terminal status pill for the reason
     * documented in fab-menu.js.
     *
     * @param {string} message - lowercase user-facing text.
     * @param {string} kind - 'info', 'success' or 'error'.
     * @returns {void}
     */
    function report(message, kind) {
        if (window.FabMenu && typeof window.FabMenu.notify === 'function') {
            window.FabMenu.notify(message, kind);
        }
    }

    /**
     * Build one button in the overlay's action row.
     *
     * @param {string} id - stable id, read by the tests.
     * @param {string} label - lowercase user-facing text.
     * @param {string} variant - extra modifier class suffix.
     * @param {Function} onClick - click handler.
     * @returns {HTMLButtonElement}
     */
    function buildButton(id, label, variant, onClick) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute('id', id);
        btn.className = 'paste-fallback__btn paste-fallback__btn--' + variant;
        btn.textContent = label;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            onClick();
        });
        return btn;
    }

    /**
     * Build the textarea the user pastes into.
     *
     * iOS is the constraint that shapes every attribute here. It ignores
     * select() on a readonly input, refuses to focus a zero-size or
     * `user-select: none` element, and zooms the whole page in when a
     * focused field's font-size is under 16px - so the field is a real,
     * writable, full-width textarea and the stylesheet pins 16px. The
     * autocorrect family is off because a pasted url or token must not
     * be "helpfully" capitalised on its way in.
     *
     * @returns {HTMLTextAreaElement}
     */
    function buildInput() {
        var ta = document.createElement('textarea');
        ta.setAttribute('id', 'pasteFallbackInput');
        ta.className = 'paste-fallback__input';
        ta.setAttribute('rows', '4');
        ta.setAttribute('autocapitalize', 'off');
        ta.setAttribute('autocorrect', 'off');
        ta.setAttribute('autocomplete', 'off');
        ta.setAttribute('spellcheck', 'false');
        ta.setAttribute('aria-label', 'paste target');
        return ta;
    }

    /**
     * The first FILE item on a paste event, or null.
     *
     * Widened from images 2026-08-16 to match the terminal's own paste
     * interceptor: `kind === 'file'` is the test that separates a file
     * paste from a text paste, and a pasted pdf is as useful to hand
     * Claude as a screenshot is. Text still falls through and lands in
     * the textarea as usual.
     *
     * `kind` is checked first and the type test is only a fallback, since
     * some browsers leave `kind` undefined on a synthesised event.
     *
     * @param {object} e - the ClipboardEvent.
     * @returns {{blob: Blob, name: string}|null}
     */
    function firstFile(e) {
        var items = (e && e.clipboardData && e.clipboardData.items) || [];
        for (var i = 0; i < items.length; i++) {
            var type = items[i].type || '';
            var isFile = items[i].kind === 'file' || type.indexOf('image/') === 0;
            if (!isFile) continue;
            var blob = typeof items[i].getAsFile === 'function'
                ? items[i].getAsFile()
                : null;
            if (blob) return { blob: blob, name: blob.name || '' };
        }
        return null;
    }

    /**
     * Open the fallback.
     *
     * @param {object} term - the Terminal wrapper, for image upload.
     * @param {Function} inject - clipboard.js's injectText(term, text).
     *   Passed in rather than imported so there is exactly one injection
     *   path in the app.
     * @returns {HTMLElement|null} the overlay, or null without a term.
     */
    function open(term, inject) {
        if (!term || typeof inject !== 'function') return null;
        close();
        // The overlay IS the feedback now, so a stale notice sitting on
        // top of it would be describing the previous attempt.
        if (window.FabMenu && typeof window.FabMenu.dismissNotice === 'function') {
            window.FabMenu.dismissNotice();
        }

        overlayEl = document.createElement('div');
        overlayEl.className = 'paste-fallback';
        overlayEl.setAttribute('id', 'pasteFallback');
        overlayEl.setAttribute('role', 'dialog');
        overlayEl.setAttribute('aria-modal', 'true');
        overlayEl.setAttribute('aria-label', 'paste from clipboard');
        overlayEl.style.zIndex = String(OVERLAY_Z);

        var panel = document.createElement('div');
        panel.className = 'paste-fallback__panel';

        var title = document.createElement('h2');
        title.className = 'paste-fallback__title';
        title.textContent = 'paste from clipboard';
        panel.appendChild(title);

        var hint = document.createElement('p');
        hint.className = 'paste-fallback__hint';
        hint.textContent = 'this connection is not a secure context, so the '
            + 'browser will not let the page read your clipboard. paste into '
            + 'the box below with cmd+v or ctrl+v, or long press and choose '
            + 'paste, then press insert. images work here too.';
        panel.appendChild(hint);

        inputEl = buildInput();
        panel.appendChild(inputEl);

        var actions = document.createElement('div');
        actions.className = 'paste-fallback__actions';
        actions.appendChild(
            buildButton('pasteFallbackCancel', 'cancel', 'ghost', close));
        actions.appendChild(
            buildButton('pasteFallbackInsert', 'insert', 'primary', function () {
                submit(term, inject);
            }));
        panel.appendChild(actions);

        overlayEl.appendChild(panel);
        document.body.appendChild(overlayEl);

        inputEl.addEventListener('paste', function (e) {
            var picked = firstFile(e);
            if (!picked) return; // text lands in the field as usual
            if (typeof e.preventDefault === 'function') e.preventDefault();
            close();
            term._uploadAndInjectFile(picked.blob, picked.name);
        });

        onDocKey = function (e) {
            if (e.key === 'Escape') {
                if (typeof e.stopPropagation === 'function') e.stopPropagation();
                close();
            }
        };
        document.addEventListener('keydown', onDocKey, true);

        focusInput();
        return overlayEl;
    }

    /**
     * Put the caret in the field. Guarded because iOS throws on
     * setSelectionRange for a field it has not laid out yet, and a throw
     * here would leave the overlay up with no caret and no error.
     *
     * @returns {void}
     */
    function focusInput() {
        if (!inputEl) return;
        try {
            inputEl.focus({ preventScroll: true });
            if (typeof inputEl.setSelectionRange === 'function') {
                inputEl.setSelectionRange(0, 0);
            }
        } catch (err) {
            console.warn('PasteFallback: could not focus the paste field', err);
        }
    }

    /**
     * Inject whatever is in the field and close. Newlines are preserved
     * exactly: the value goes through untouched, as one payload, so a
     * multi-line paste reaches the pty the same way the secure-context
     * path sends it.
     *
     * @param {object} term - the Terminal wrapper.
     * @param {Function} inject - clipboard.js's injectText.
     * @returns {void}
     */
    function submit(term, inject) {
        var text = inputEl ? String(inputEl.value || '') : '';
        if (!text) {
            report('nothing to paste - the box is empty', 'info');
            focusInput();
            return;
        }
        close();
        inject(term, text);
    }

    window.PasteFallback = {
        open: open,
        close: close,
        isOpen: isOpen,
        OVERLAY_Z: OVERLAY_Z
    };
})();

console.log('[PasteFallback Module] Exported as window.PasteFallback');
