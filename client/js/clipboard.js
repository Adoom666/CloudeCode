/**
 * Clipboard Tools (clipboard paste + terminal copy chord)
 * ----------------------------------------------------------------------
 * Two features, wired into terminal.js through two hook points (this
 * file is loaded right AFTER terminal.js in index.html; terminal.js
 * only calls in at runtime, after the async xterm CDN wait, so
 * window.ClipboardTools is always defined by then):
 *
 *  1. PASTE FROM CLIPBOARD - reads the LOCAL device clipboard via
 *     navigator.clipboard.read(): the first image/* item routes through
 *     the uploadAndInject() flow below; otherwise
 *     text/plain (or the readText() fallback) is injected via
 *     Terminal#insertText() - the same binary WebSocket path as
 *     term.onData - sent as ONE frame so the server's >256B
 *     bracketed-paste heuristic in tmux_backend.py treats large pastes
 *     correctly.
 *
 *     Clipboard API realities: read()/readText() require a secure
 *     context + permission. On LAN http (non-localhost) the API can be
 *     entirely undefined, and where present it can reject with
 *     NotAllowedError. Every path degrades to the existing status pill
 *     pointing at the keyboard paste path - nothing throws unhandled.
 *     pasteFromClipboard() is only ever invoked from a menu tap, i.e.
 *     inside a user gesture, which the permission model requires.
 *
 *     THE MENU THAT USED TO LIVE HERE IS GONE. The paperclip FAB owned a
 *     two-item popup (paste / attach file) while a second folded strip
 *     over the terminal's top-right corner owned copy / theme / music.
 *     Both are now rows of the single session tools menu in
 *     client/js/terminal-tools-menu.js, which calls the two functions
 *     exported below. This file no longer builds or positions any UI.
 *
 *  2. COPY CHORD - Terminal#_applyKeyHandlers()' xterm custom key
 *     handler calls handleCopyChord() first for every key event.
 *     Cmd+C (mac) / Ctrl+Shift+C (win/linux) WITH an active xterm
 *     selection writes the selection to the system clipboard and
 *     swallows the event. Bare Ctrl+C is NEVER intercepted - it must
 *     reach the pty as SIGINT (0x03). The selection is left in place
 *     after copy (macOS Terminal behavior: selection stays).
 */

(function () {
    'use strict';

    /* =================================================================
     * Reporting
     * ================================================================= */

    /**
     * Report a result somewhere the user can actually see it.
     *
     * Everything in this file is reached from a session tools menu row,
     * and the terminal status pill (z-index 70) is painted over by the
     * sticky header (1000) and by every overlay a row can open. That is
     * why the paste row looked like it did nothing at all over plain
     * http: it DID report, underneath the header. FabMenu.notify is the
     * shared fix; the pill stays as the fallback for a document that
     * somehow loaded this without fab-menu.js.
     *
     * @param {object} term - the Terminal wrapper.
     * @param {string} message - lowercase user-facing text.
     * @param {string} kind - 'info', 'success' or 'error'.
     * @returns {void}
     */
    function report(term, message, kind) {
        if (window.FabMenu && typeof window.FabMenu.notify === 'function') {
            window.FabMenu.notify(message, kind);
            return;
        }
        if (term && typeof term._showStatusPill === 'function') {
            term._showStatusPill(message, kind);
        }
    }

    /* =================================================================
     * Copy chord
     * ================================================================= */

    /**
     * Called from xterm's attachCustomKeyEventHandler closure for EVERY
     * key event. Returns true only when the event was consumed (the
     * caller then returns false so xterm drops it). Anything that is
     * not a copy chord - including bare Ctrl+C - falls straight through
     * to xterm's default handling.
     */
    function handleCopyChord(ev, term) {
        if (ev.type !== 'keydown') return false;
        if ((ev.key || '').toLowerCase() !== 'c') return false;

        // Cmd+C (mac) or Ctrl+Shift+C (win/linux). Bare Ctrl+C (no shift)
        // is deliberately excluded - that chord is SIGINT and must reach
        // the pty untouched.
        const isCopyChord =
            (ev.metaKey && !ev.ctrlKey && !ev.shiftKey && !ev.altKey) ||
            (ev.ctrlKey && ev.shiftKey && !ev.metaKey && !ev.altKey);
        if (!isCopyChord) return false;

        if (!term.term || typeof term.term.hasSelection !== 'function' || !term.term.hasSelection()) {
            return false; // nothing selected - let the key pass through
        }

        ev.preventDefault();
        ev.stopPropagation();
        writeSystemClipboard(term, term.term.getSelection());
        return true;
    }

    /**
     * Fire-and-forget clipboard write. Success is silent (matches macOS
     * Terminal - the selection staying put is the confirmation); failure
     * surfaces via the existing status pill. Never throws.
     */
    function writeSystemClipboard(term, text) {
        if (!text) return;
        // Routed through CopyCompat: over plain http (Tailscale / LAN)
        // navigator.clipboard is entirely undefined, and the old code
        // dead-ended there with "clipboard unavailable". CopyCompat falls
        // back to execCommand, which is not secure-context gated.
        window.CopyCompat.copyText(text).then((result) => {
            if (!result.ok) {
                report(term, 'copy blocked by browser - use the system copy shortcut', 'error');
            }
        });
    }

    /* =================================================================
     * File input
     * ================================================================= */

    /**
     * Wire the hidden file input. The picker itself is opened by the
     * "attach file" row of the session tools menu; this only owns what
     * happens once a file comes back.
     *
     * Any file, not just an image: the server sanitises the name and the
     * terminal gets a path Claude can read for itself.
     *
     * @param {object} term - the Terminal wrapper.
     * @param {HTMLInputElement} input - the hidden file input.
     * @returns {void}
     */
    function wireFileInput(term, input) {
        if (!input || input._clipboardWired) return;
        input._clipboardWired = true;
        input.addEventListener('change', async () => {
            const file = input.files && input.files[0];
            if (!file) return;
            await uploadAndInject(term, file, file.name || '');
            input.value = '';
        });
    }

    /* =================================================================
     * Upload + path injection
     * ================================================================= */

    /**
     * Upload a blob and inject the resulting absolute path into the terminal.
     *
     * THE one upload flow, for every entry point: the desktop paste
     * interceptor, the paste-fallback sheet, and the attach-file picker.
     * Generalised from Terminal#_uploadAndInjectImage() 2026-08-16 rather
     * than given a sibling, because nothing in the old body was
     * image-specific beyond the wording. Images behave exactly as before;
     * the server still applies the stricter image contract (magic-byte
     * cross-check, tighter size cap) to anything with an image extension.
     *
     * Trailing SPACE, not Enter: Claude Code auto-attaches an absolute
     * image path that appears in its prompt buffer, and reads any other
     * absolute path with its own file tools, so the path lands with a
     * separator and the user keeps typing. Auto-Enter would submit a
     * path-only message and waste the round-trip.
     *
     * @param {object} term - the Terminal wrapper (insertText, status pill).
     * @param {Blob} blob - the bytes to upload.
     * @param {string} [filename] - declared name; empty for a nameless
     *   clipboard blob, where api.js derives "paste.<ext>" from the type.
     * @returns {Promise<void>}
     */
    async function uploadAndInject(term, blob, filename) {
        report(term, 'uploading...', 'info');
        try {
            // Multi-session: scope the upload to THIS tab's session so the
            // file lands in the right project's working dir.
            const sessionId = typeof term._sessionId === 'function' ? term._sessionId() : null;
            const result = await window.API.uploadFile(blob, filename || '', sessionId);
            term.insertText(quotePathForPrompt(result.path) + ' ');
            report(term, 'attached: ' + result.filename, 'success');
        } catch (err) {
            console.error('[FILE-PASTE] upload failed', err);
            report(term, 'upload failed: ' + (err && err.message ? err.message : 'unknown'), 'error');
        }
    }

    /**
     * Render an absolute path so it survives the shell prompt it lands in.
     *
     * The server sanitises the BASENAME it writes, so the injected string
     * is usually already bare-safe. The DIRECTORY half is the session's
     * working dir, which the app does not control and which routinely
     * contains spaces on macOS ("~/Library/Mobile Documents/..."). So the
     * decision is made on the whole string, not on the part we sanitised.
     *
     * Bare when every character is in the shell-safe set, otherwise
     * single-quoted with the standard `'\''` break-out for any embedded
     * single quote. Single quotes are used rather than double because
     * they suppress $ and ` expansion too, and Claude Code reads a quoted
     * absolute path the same as a bare one.
     *
     * @param {string} p - absolute path from the upload response.
     * @returns {string} the path, bare or single-quoted.
     */
    function quotePathForPrompt(p) {
        var s = String(p == null ? '' : p);
        if (s === '') return s;
        if (/^[A-Za-z0-9._\-\/~+=:,@]+$/.test(s)) return s;
        return "'" + s.split("'").join("'\\''") + "'";
    }

    /* =================================================================
     * Paste from local clipboard
     * ================================================================= */

    /**
     * Reads the LOCAL device clipboard. Only called from the menu tap
     * (user gesture) to satisfy clipboard-permission rules.
     *
     * Order: rich read() first - an image wins when present (mirrors
     * the desktop paste interceptor in terminal.js). Text comes from
     * the same rich items when available, else the readText() fallback.
     *
     * DETECT, DO NOT ASSUME. The read APIs need a secure context, which
     * a LAN address never is, so on http they are simply absent and on
     * https they can still reject with NotAllowedError. Either way we
     * stop asking the browser and ask the USER instead, via the paste
     * fallback - the one path that works on every origin. Never throws.
     *
     * @param {object} term - the Terminal wrapper.
     * @returns {Promise<void>}
     */
    async function pasteFromClipboard(term) {
        const canRead = !!(navigator.clipboard && typeof navigator.clipboard.read === 'function');
        const canReadText = !!(navigator.clipboard && typeof navigator.clipboard.readText === 'function');

        if (!canRead && !canReadText) {
            openFallback(term);
            return;
        }

        if (canRead) {
            try {
                const items = await navigator.clipboard.read();
                for (const item of items) {
                    const imageType = (item.types || []).find((t) => t.indexOf('image/') === 0);
                    if (imageType) {
                        const blob = await item.getType(imageType);
                        // A clipboard blob carries no name; api.js derives
                        // "paste.<ext>" from the blob's own mime type.
                        await uploadAndInject(term, blob, '');
                        return;
                    }
                }
                for (const item of items) {
                    if ((item.types || []).indexOf('text/plain') !== -1) {
                        const blob = await item.getType('text/plain');
                        injectText(term, await blob.text());
                        return;
                    }
                }
                report(term, 'clipboard is empty', 'info');
                return;
            } catch (err) {
                // Permission denied / unsupported MIME - fall through to
                // the text-only path before giving up.
            }
        }

        if (canReadText) {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    injectText(term, text);
                } else {
                    report(term, 'clipboard is empty', 'info');
                }
                return;
            } catch (err) {
                // Permission denied. Fall through to the fallback.
            }
        }

        openFallback(term);
    }

    /**
     * Hand the paste over to the user because the browser will not hand
     * it to us. Passes injectText in so the fallback shares this file's
     * injection path rather than growing a second one.
     *
     * @param {object} term - the Terminal wrapper.
     * @returns {void}
     */
    function openFallback(term) {
        if (window.PasteFallback && typeof window.PasteFallback.open === 'function') {
            window.PasteFallback.open(term, injectText);
            return;
        }
        report(term,
            'paste unavailable on this connection - use cmd+v / ctrl+v in the terminal',
            'error');
    }

    /**
     * Inject clipboard text exactly as if pasted: ONE binary WebSocket
     * frame via the existing Terminal#insertText() (the same send path
     * term.onData uses) so the server's >256B bracketed-paste heuristic
     * sees the whole paste as a single payload.
     *
     * THE ONLY INJECTION PATH. Both the secure-context read above and
     * the paste fallback in paste-fallback.js land here, so bracketed
     * paste and the single-frame send cannot diverge between them.
     * Newlines are passed through untouched.
     *
     * @param {object} term - the Terminal wrapper.
     * @param {string} text - the text to inject, newlines included.
     * @returns {void}
     */
    function injectText(term, text) {
        if (!text) {
            report(term, 'clipboard is empty', 'info');
            return;
        }
        if (!term.ws || term.ws.readyState !== WebSocket.OPEN) {
            report(term, 'terminal not connected', 'error');
            return;
        }
        term.insertText(text);
        report(term, 'pasted from clipboard', 'success');
    }

    window.ClipboardTools = {
        handleCopyChord,
        pasteFromClipboard,
        wireFileInput,
        injectText,
        quotePathForPrompt,
        uploadAndInject,
    };
})();
