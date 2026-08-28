/**
 * Copy-output sheet - get text OUT of the terminal on a phone.
 * ----------------------------------------------------------------------
 * On a touch device there is no way to select terminal text: xterm.js has
 * no touch selection, and the long-press flow in touch-select.js is
 * fiddly across a wrapped url. When `/login` prints a sign-in url and a
 * code, a phone user simply cannot get them off the screen.
 *
 * This is the general answer: a button on the terminal screen opens a
 * sheet showing the recent output with
 *   - one-tap copy chips for every url and code-shaped token found
 *     (output-scan.js - it knows nothing about the login flow, so any
 *     url or token in any output gets the same treatment),
 *   - an "open" link beside every http(s) url, because on the device you
 *     are already holding, following a sign-in link beats copying it,
 *   - "copy all" for the whole visible block,
 *   - the raw text in a selectable textarea as the last resort, so even
 *     when both programmatic clipboard tiers fail the user can long-press
 *     and use the OS copy menu.
 *
 * All copying goes through CopyCompat, which is what makes this work at
 * all over plain http where `navigator.clipboard` does not exist.
 */
(function () {
    'use strict';

    /** How many buffer lines the sheet shows and "copy all" copies. */
    var LINE_COUNT = 400;

    /** The open sheet, or null. Only ever one. */
    var sheetEl = null;

    /** Escape handler bound while the sheet is open. */
    var onDocKey = null;

    /** Longest chip label before it is shortened in the middle. */
    var CHIP_LABEL_MAX = 44;

    /**
     * Read the tail of the xterm buffer as plain text, REJOINING lines
     * the pane only wrapped for display.
     *
     * xterm stores one buffer row per VISUAL row and flags continuations
     * with `isWrapped`. Joining every row with a newline therefore cuts a
     * long token at the pane width, which is a correctness bug and not a
     * cosmetic one: measured 2026-08-16, a 30 column pane turned
     * `https://claude.ai/oauth/authorize?...` into
     * `https://claude.ai/oauth/author` and `WDJB-MJHT` into `WDJB-MJH`.
     * That is why the phone showed a stub of the sign-in url while the
     * wide desktop pane showed all of it - same code, different wrap.
     *
     * `isWrapped` alone is NOT enough. It is only set when tmux relies on
     * autowrap. When the emitting program hard-wraps at the pane width
     * itself, which is what claude's own renderer does, tmux writes each
     * row with explicit cursor positioning and EVERY row comes back
     * `isWrapped=false`. Measured 2026-08-16 against the vendored xterm at
     * 43 columns: the rows split at exactly 43 characters and the user got
     * `https://claude.com/cai/oauth/authorize?code`, a 43 character stub.
     * So a row whose LAST CELL is occupied is also treated as continuing,
     * joined with nothing. A short row still ends the logical line, which
     * keeps the `/login` short-code case intact.
     *
     * A row is only right-trimmed when it ENDS a logical line; trimming a
     * continuation would eat characters that belong mid-token.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {number} [lines=LINE_COUNT] - how many buffer rows to read.
     * @returns {string} the joined text, '' when the buffer is unreadable.
     */
    function readRecentOutput(term, lines) {
        var want = typeof lines === 'number' ? lines : LINE_COUNT;
        if (!term) return '';
        try {
            var buf = term.buffer.active;
            var end = buf.length;
            var start = Math.max(0, end - want);
            var cols = typeof term.cols === 'number' ? term.cols : 0;
            var rows = [];
            var current = null;
            var prevFilled = false;
            for (var i = start; i < end; i++) {
                var line = buf.getLine(i);
                var raw = line ? line.translateToString(false) : '';
                var continues = (line && line.isWrapped) || prevFilled;
                prevFilled = cols > 0 && raw.length >= cols &&
                    raw.charAt(cols - 1) !== ' ';
                if (continues && current !== null) {
                    current += raw;
                    continue;
                }
                if (current !== null) rows.push(current.replace(/\s+$/, ''));
                current = raw;
            }
            if (current !== null) rows.push(current.replace(/\s+$/, ''));
            while (rows.length && rows[0].trim() === '') rows.shift();
            while (rows.length && rows[rows.length - 1].trim() === '') rows.pop();
            return rows.join('\n');
        } catch (err) {
            console.warn('CopyOutput: buffer read failed', err);
            return '';
        }
    }

    /**
     * Shorten a chip LABEL from the middle so both ends stay readable and
     * the fact that something was removed is visible.
     *
     * The label is display only. Every copy path uses the full value, and
     * a test asserts that specifically, because a chip that copies what it
     * shows instead of what it holds would hand out a broken url.
     *
     * @param {string} value - the full token.
     * @param {number} [max=CHIP_LABEL_MAX] - longest label to allow.
     * @returns {string} value itself, or head + ellipsis + tail.
     */
    function shortenForChip(value, max) {
        var limit = typeof max === 'number' ? max : CHIP_LABEL_MAX;
        var text = value || '';
        if (limit < 8 || text.length <= limit) return text;
        var keep = limit - 3;
        var head = Math.ceil(keep / 2);
        var tail = keep - head;
        return text.slice(0, head) + '...' + text.slice(text.length - tail);
    }

    /** The sheet's inline status line, or null when the sheet is closed. */
    var statusEl = null;

    /** The sheet's selectable textarea, or null. */
    var textEl = null;

    /**
     * Say what just happened, INSIDE the sheet.
     *
     * The terminal status pill sits at z-index 70 and the sheet at 10000,
     * so a pill raised while the sheet is open is painted underneath it
     * and the user sees nothing at all. Feedback that only exists behind
     * an overlay is the same as no feedback, so the sheet carries its own
     * line and the pill is a bonus for after it closes.
     *
     * @param {string} message - lowercase, user facing.
     * @param {string} kind - 'success' | 'error' | 'info'.
     * @returns {void}
     */
    function setStatus(message, kind) {
        if (!statusEl) return;
        statusEl.textContent = message;
        statusEl.dataset.kind = kind || 'info';
    }

    /**
     * Select a token inside the raw-output textarea so the OS long-press
     * menu lands on it, for when no programmatic tier worked.
     *
     * @param {string} value - the token to highlight.
     * @returns {boolean} true when it was found and selected.
     */
    function selectInRawText(value) {
        if (!textEl || !value) return false;
        var at = textEl.value.indexOf(value);
        if (at < 0) return false;
        try {
            textEl.focus({ preventScroll: true });
            textEl.setSelectionRange(at, at + value.length);
            return true;
        } catch (err) {
            console.warn('CopyOutput: could not preselect raw text', err);
            return false;
        }
    }

    /**
     * Copy handler shared by every control in the sheet. Runs inside the
     * click gesture so CopyCompat's execCommand tier stays usable.
     *
     * Success does NOT close the sheet: `/login` prints a url AND a code
     * and the user needs both, so closing after the first one made the
     * second a second trip.
     *
     * @param {object} termWrapper - the Terminal wrapper (status pill).
     * @param {string} text - the FULL text to copy, never the chip label.
     * @param {string} label - what to name it in the confirmation.
     * @param {HTMLElement} [source] - the control that was tapped, marked
     *   copied so the confirmation is where the finger already is.
     * @returns {Promise} resolves once the result has been reported.
     */
    function copyAndReport(termWrapper, text, label, source) {
        return window.CopyCompat.copyText(text).then(function (result) {
            if (result.ok) {
                setStatus('copied ' + label + ' to clipboard', 'success');
                if (source) {
                    source.classList.add('is-copied');
                    source.setAttribute('data-copied', 'copied');
                }
                if (termWrapper && termWrapper._showStatusPill) {
                    termWrapper._showStatusPill('copied ' + label, 'success');
                }
            } else {
                // Nothing programmatic worked. Never claim otherwise, and
                // never leave a dead tap: put the OS selection on the text
                // so the long-press menu is one gesture away.
                var picked = selectInRawText(text);
                setStatus(
                    picked
                        ? 'copy blocked by the browser. the ' + label +
                          ' is selected below - long press it and choose copy'
                        : 'copy blocked by the browser. long press the text ' +
                          'below and choose copy',
                    'error'
                );
            }
            return result;
        });
    }

    /**
     * Build one tap-to-copy chip.
     *
     * The label may be shortened; `item.value` is what gets copied.
     *
     * @param {object} termWrapper
     * @param {{kind: string, value: string}} item
     * @returns {HTMLButtonElement}
     */
    function buildChip(termWrapper, item) {
        var chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'cloude-copy-chip cloude-copy-chip--' + item.kind;
        chip.title = item.value;
        chip.setAttribute('aria-label', 'copy ' + item.kind + ' ' + item.value);
        // Read by the tests and by anything that needs the whole token
        // back off the DOM. The label is not a reliable source for it.
        chip.dataset.value = item.value;

        var kind = document.createElement('span');
        kind.className = 'cloude-copy-chip__kind';
        kind.textContent = item.kind;

        var value = document.createElement('span');
        value.className = 'cloude-copy-chip__value';
        value.textContent = shortenForChip(item.value);

        chip.appendChild(kind);
        chip.appendChild(value);

        if (item.value.length > CHIP_LABEL_MAX) {
            // Otherwise a shortened label is indistinguishable from a
            // short token, which is exactly the doubt the phone raised.
            var size = document.createElement('span');
            size.className = 'cloude-copy-chip__size';
            size.textContent = item.value.length + ' chars';
            chip.appendChild(size);
        }

        chip.addEventListener('click', function () {
            copyAndReport(termWrapper, item.value, item.kind, chip);
        });
        return chip;
    }

    /**
     * True when a value is safe to put in a real `href` that the user can
     * follow.
     *
     * ONLY http and https. This is an allow-list, not a deny-list: a code
     * token is not a url and must never render as a link, and the hostile
     * schemes (`javascript:`, `data:`, `vbscript:`) are excluded by the
     * simple fact that they are not http. The whitespace and control-char strip
     * before the test is the obfuscation defence copied from
     * `MarkdownLite.isSafeUrl` in client/js/markdown-lite.js - a browser
     * ignores leading control characters and interior whitespace in a
     * scheme, so `java\nscript:x` is a live javascript url, and a naive
     * `startsWith('http')` on the raw string would also pass
     * `javascript:x?http://y`.
     *
     * @param {string} value - the candidate url.
     * @returns {boolean} true only for http:// and https://.
     */
    function isHttpUrl(value) {
        var stripped = String(value == null ? '' : value)
            .replace(/[\s -]+/g, '')
            .toLowerCase();
        return /^https?:\/\/[^/]/.test(stripped);
    }

    /**
     * Build one result row: the copy chip, plus an "open" link when the
     * item is a real http(s) url.
     *
     * WHY A REAL ANCHOR. Copying is not what a user wants from a sign-in
     * link on the device they are already holding - they want to follow
     * it. `window.open()` from a handler is what a popup blocker eats, so
     * this is a genuine `<a target="_blank" rel="noopener noreferrer">`
     * that the browser treats as a user-initiated navigation. `noopener`
     * also severs `window.opener`, so the opened page cannot navigate the
     * terminal tab.
     *
     * The link is NOT rendered for a code token and NOT rendered for
     * anything that is not http(s). See isHttpUrl().
     *
     * @param {object} termWrapper - the Terminal wrapper.
     * @param {{kind: string, value: string}} item
     * @returns {HTMLElement} the row.
     */
    function buildRow(termWrapper, item) {
        var row = document.createElement('div');
        row.className = 'cloude-copy-row';
        row.appendChild(buildChip(termWrapper, item));

        if (item.kind === 'url' && isHttpUrl(item.value)) {
            var link = document.createElement('a');
            link.className = 'cloude-copy-open';
            link.href = item.value;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = 'open';
            link.setAttribute('aria-label', 'open ' + item.value + ' in a new tab');
            link.title = 'open in a new tab';
            row.appendChild(link);
        }
        return row;
    }

    /**
     * Open the sheet for the current terminal contents.
     *
     * @param {object} termWrapper - the Terminal wrapper instance.
     * @returns {void}
     */
    function open(termWrapper) {
        close();
        if (!termWrapper || !termWrapper.term) return;

        var text = readRecentOutput(termWrapper.term);
        var items = window.OutputScan ? window.OutputScan.scan(text) : [];

        sheetEl = document.createElement('div');
        sheetEl.className = 'cloude-copy-sheet';
        sheetEl.setAttribute('role', 'dialog');
        sheetEl.setAttribute('aria-label', 'copy output');

        var panel = document.createElement('div');
        panel.className = 'cloude-copy-sheet__panel';

        var head = document.createElement('div');
        head.className = 'cloude-copy-sheet__head';

        var title = document.createElement('span');
        title.className = 'cloude-copy-sheet__title';
        title.textContent = 'copy output';

        var closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'cloude-copy-sheet__close';
        closeBtn.setAttribute('aria-label', 'close');
        closeBtn.textContent = 'x';
        closeBtn.addEventListener('click', close);

        head.appendChild(title);
        head.appendChild(closeBtn);
        panel.appendChild(head);

        if (items.length) {
            var chips = document.createElement('div');
            chips.className = 'cloude-copy-sheet__chips';
            items.forEach(function (item) {
                chips.appendChild(buildRow(termWrapper, item));
            });
            panel.appendChild(chips);
        } else {
            var none = document.createElement('p');
            none.className = 'cloude-copy-sheet__empty';
            none.textContent = 'no links or codes found in recent output';
            panel.appendChild(none);
        }

        // Above the raw text so a failure message and the text it points
        // at are visible together without scrolling.
        statusEl = document.createElement('p');
        statusEl.className = 'cloude-copy-status';
        statusEl.setAttribute('role', 'status');
        statusEl.setAttribute('aria-live', 'polite');
        statusEl.dataset.kind = 'info';
        statusEl.textContent = items.length
            ? 'tap an item to copy it'
            : 'nothing to tap - copy the raw text below';
        panel.appendChild(statusEl);

        var area = document.createElement('textarea');
        area.className = 'cloude-copy-sheet__text';
        area.setAttribute('readonly', '');
        area.setAttribute('aria-label', 'recent terminal output');
        area.value = text;
        panel.appendChild(area);
        textEl = area;

        var foot = document.createElement('div');
        foot.className = 'cloude-copy-sheet__foot';

        var copyAll = document.createElement('button');
        copyAll.type = 'button';
        copyAll.className = 'cloude-copy-sheet__action';
        copyAll.textContent = 'copy all';
        copyAll.addEventListener('click', function () {
            copyAndReport(termWrapper, text, 'output', copyAll);
        });

        var hint = document.createElement('span');
        hint.className = 'cloude-copy-sheet__hint';
        hint.textContent = window.CopyCompat.hasAsyncClipboard()
            ? 'or select the text above'
            : 'insecure connection - long press the text above if a copy fails';

        foot.appendChild(copyAll);
        foot.appendChild(hint);
        panel.appendChild(foot);

        sheetEl.appendChild(panel);
        document.body.appendChild(sheetEl);

        // Backdrop tap closes; taps inside the panel must not.
        sheetEl.addEventListener('click', function (e) {
            if (e.target === sheetEl) close();
        });
        onDocKey = function (e) {
            if (e.key === 'Escape') close();
        };
        document.addEventListener('keydown', onDocKey, true);
    }

    /**
     * Close the sheet if open. Safe to call when it is not.
     *
     * @returns {void}
     */
    function close() {
        if (onDocKey) {
            document.removeEventListener('keydown', onDocKey, true);
            onDocKey = null;
        }
        if (sheetEl) {
            sheetEl.remove();
            sheetEl = null;
        }
        statusEl = null;
        textEl = null;
    }

    /**
     * Wire the terminal-screen copy button.
     *
     * @param {object} termWrapper - the Terminal wrapper instance.
     * @param {HTMLElement} btn - the button element.
     * @returns {void}
     */
    function wireButton(termWrapper, btn) {
        if (!btn || btn._copyOutputWired) return;
        btn._copyOutputWired = true;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (sheetEl) {
                close();
            } else {
                open(termWrapper);
            }
        });
    }

    window.CopyOutput = {
        open: open,
        close: close,
        wireButton: wireButton,
        readRecentOutput: readRecentOutput,
        shortenForChip: shortenForChip,
        buildChip: buildChip,
        buildRow: buildRow,
        isHttpUrl: isHttpUrl
    };
})();
