/**
 * Attachment Toast
 * ----------------------------------------------------------------------
 * The receipt for a file the user just attached to the prompt: WHAT was
 * attached, shown as a picture rather than described, and it stays up
 * until the prompt carrying it is sent.
 *
 * WHAT THIS REPLACED, because the shape of the fix follows from it. The
 * confirmation used to be `FabMenu.notify('attached: <name>')`, a single
 * low-contrast strip painted directly over live terminal output at
 * z-index 10005 with a 3-second timer. Over a running claude session the
 * text sat on top of moving scrollback with no card behind it, so the
 * two competed for the same pixels and neither won - the user's report
 * was that it is unreadable, and the screenshot bears that out. A
 * notice that cannot be read is not a notice.
 *
 * SO IT IS A TOAST, AND IT IS THE SAME TOAST AS EVERYTHING ELSE. There
 * is exactly one notification card component in this app
 * (client/js/toast.js, painted by client/css/toast.css) and this file
 * does not build a second one. It produces a toast in the server's own
 * record shape, hands it to `ToastManager.add()`, and contributes ONLY
 * the thumbnail strip that no server-sent toast has. Card, stacking,
 * coalescing, cap, overflow, dismiss control, theme accent, live-region
 * politeness and the mobile breakpoint all come free and cannot drift
 * from the notifications beside them.
 *
 * THREE THINGS MAKE IT DIFFERENT FROM A SERVER TOAST, and each is
 * declared rather than inferred:
 *
 *   1. IT IS LOCAL. There is no server record behind it, so dismissing
 *      it must not POST an ack for an id the server never issued. The
 *      toast carries `local: true` and `ToastManager.dismiss` reads it.
 *
 *   2. IT SURVIVES TYPING. `dismissForSessionActivity` clears a
 *      session's toasts on the user's next keystroke, because input is
 *      the ANSWER to a notification. An attachment toast is not waiting
 *      to be answered - it is a receipt for what is staged in the
 *      prompt buffer the user is typing INTO. Clearing it on the first
 *      keystroke would make it flash and vanish, which is a worse
 *      version of the bug being fixed. What retires it is the prompt
 *      being SENT.
 *
 *   3. IT SHOWS THE FILE. `thumbSpecFor` decides what the user sees:
 *      a real downscaled preview for an image, and a typed chip for
 *      anything else. NEVER a broken image - an <img> whose src failed
 *      is the one outcome that reads as a bug rather than as a file.
 *
 * THE PREVIEW IS A DOWNSCALED `data:` URL, AND NEVER A `blob:` ONE. The
 * app ships `img-src 'self' data:` (src/security_headers.py); `blob:` is
 * absent from that list, so an object URL is blocked with no visible
 * error, and widening a CSP directive to paint a 44px square would be a
 * poor trade. The bytes are already in the browser, so the preview is
 * made locally - decode, draw at THUMB_PX, export - with no network
 * request and nothing added to the policy. It is downscaled rather than
 * inlined whole because a phone photo is several megabytes, base64 costs
 * another third, and it is held in the DOM until the prompt is sent.
 *
 * EVERY FAILURE DEGRADES TO THE CHIP. A file that will not decode, a
 * browser with no canvas, a reader that errors: none of them throw and
 * none of them produce an empty frame. They produce the same typed chip
 * a .pdf gets, because "here is a file called x" is true regardless of
 * whether we managed to draw it.
 */

console.log('[AttachmentToast Module] Loading...');

(function () {
    'use strict';

    /**
     * Longest edge of the generated preview, in device pixels.
     *
     * The card paints the thumb at 44 CSS px (the touch-target floor
     * this app uses everywhere), so 88 is exactly 2x for a retina phone
     * and nothing is gained above it - the extra bytes would be scaled
     * back down by the compositor.
     *
     * @type {number}
     */
    var THUMB_PX = 88;

    /**
     * Quality for the exported preview. 0.72 is the point where a 44px
     * square stops changing visibly on the screens this renders on.
     * @type {number}
     */
    var THUMB_QUALITY = 0.72;

    /**
     * Longest extension rendered verbatim on a chip. Past this the
     * extension is not a recognisable type marker any more, so the
     * generic label reads better than eight cramped characters.
     * @type {number}
     */
    var MAX_EXT_CHARS = 4;

    /** What a chip says when there is no usable extension. */
    var GENERIC_EXT_LABEL = 'file';

    /**
     * Is this payload the user SENDING the prompt, as opposed to typing
     * into it?
     *
     * Carriage return is the submit key on every path in this app: xterm
     * emits `\r` for Enter, and the d-pad's ENTER key is the literal
     * string `'\r'` (client/js/dpad.js). Line FEED is deliberately not a
     * submit signal - terminal.js maps the mobile keyboard's Yen key to
     * `\n` precisely so a phone can insert a newline WITHOUT sending,
     * and Claude Code treats it the same way. Reading `\n` as a submit
     * would retire the receipt on the one keystroke that means "I am
     * still writing".
     *
     * SHIFT+ENTER CONTAINS A CARRIAGE RETURN AND IS NOT A SUBMIT. It is
     * sent as ESC+CR (`\x1b\r`), the VSCode/Alacritty pattern Claude
     * Code documents for "newline without sending", and terminal.js
     * emits exactly those two bytes for that chord. A bare `indexOf`
     * would read it as a send and retire the receipt on the one chord
     * that means the user is still composing a multi-line prompt - the
     * same mistake as reading `\n` as a submit, arriving by a different
     * route. The escape-prefixed pairs are removed before the question
     * is asked, so a payload that carries BOTH (a paste ending in a
     * real newline) still reads as a submit.
     *
     * MOUSE REPORTS ARE EXCLUDED, through the module that already owns
     * that question rather than through a second copy of the patterns.
     * xterm delivers pointer reports on the same channel as typing, and
     * under claude's any-event tracking it emits one per pointer MOTION;
     * an X10-encoded report can carry 0x0d as a coordinate byte, so
     * moving the mouse could otherwise "send" a prompt nobody sent.
     *
     * Inputs: data (string) - the bytes about to reach the pty.
     * Output: boolean - true only for a real submit.
     * Example: isPromptSubmit('\r') -> true; isPromptSubmit('\x1b\r') -> false
     */
    function isPromptSubmit(data) {
        if (typeof data !== 'string' || data === '') return false;
        if (window.TerminalInputKind
            && typeof window.TerminalInputKind.isMouseReport === 'function'
            && window.TerminalInputKind.isMouseReport(data)) {
            return false;
        }
        return data.split('\x1b\r').join('').indexOf('\r') !== -1;
    }

    /**
     * Decide what the card should SHOW for one attachment, before any
     * bytes have been decoded.
     *
     * Pure, and separate from the drawing on purpose: "is this an image"
     * is a question about the declared type and the name, while "did the
     * preview render" is a question about the browser. Keeping them
     * apart is what lets a file that claims to be an image but will not
     * decode fall back to a chip instead of leaving an empty frame.
     *
     * THE MIME TYPE OUTRANKS THE EXTENSION because it is what the
     * source actually declared: a clipboard blob has a real
     * `image/png` type and no filename at all, which is the common case
     * for a screenshot paste. The extension is the fallback for a
     * picker-chosen file whose type the browser left empty.
     *
     * Inputs: filename (string) - the stored basename, may be empty;
     *   mimeType (string) - the blob's declared type, may be empty.
     * Output: {image: boolean, label: string} - `label` is the chip
     *   text, uppercased, and is set even for an image so a failed
     *   decode has something true to fall back to.
     * Example: thumbSpecFor('spec.pdf', '') -> {image:false, label:'PDF'}
     *          thumbSpecFor('', 'image/png') -> {image:true, label:'PNG'}
     */
    function thumbSpecFor(filename, mimeType) {
        var mime = String(mimeType == null ? '' : mimeType).toLowerCase();
        var ext = extensionOf(filename);
        // A subtype like `image/svg+xml` is still an image, and its
        // useful label is the part before the `+`.
        var fromMime = mime.indexOf('/') !== -1
            ? mime.split('/')[1].split('+')[0].split(';')[0]
            : '';
        var label = ext || fromMime || GENERIC_EXT_LABEL;
        if (label.length > MAX_EXT_CHARS) label = GENERIC_EXT_LABEL;
        return {
            image: mime.indexOf('image/') === 0 || isImageExtension(ext),
            label: label.toUpperCase(),
        };
    }

    /**
     * The lowercase extension of a basename, without the dot.
     *
     * A dotfile with no extension (`.zshrc`) has none: the leading dot
     * is part of the name, not a separator, and returning `zshrc` would
     * put a wrong four-plus-character token on the chip.
     *
     * Inputs: filename (string). Output: string - '' when there is none.
     * Example: extensionOf('a.tar.gz') -> 'gz'; extensionOf('.zshrc') -> ''
     */
    function extensionOf(filename) {
        var name = String(filename == null ? '' : filename);
        var dot = name.lastIndexOf('.');
        if (dot <= 0 || dot === name.length - 1) return '';
        return name.slice(dot + 1).toLowerCase();
    }

    /**
     * Extensions worth previewing when the blob declared no type.
     *
     * Deliberately short and deliberately not `svg`: an SVG is markup,
     * and drawing untrusted markup through an <img> into a canvas is a
     * bigger question than a 44px square is worth. It gets a chip.
     *
     * Inputs: ext (string, lowercase, no dot). Output: boolean.
     */
    function isImageExtension(ext) {
        return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif']
            .indexOf(String(ext)) !== -1;
    }

    /**
     * Produce a small `data:` preview for an image blob, or null.
     *
     * Never throws and never rejects: every caller treats null as "show
     * the chip instead", which is a correct answer rather than an error
     * state. The reasons it can be null are all legitimate - the file is
     * not an image, the bytes will not decode, the browser has no
     * canvas, a tainted or zero-sized frame - and none of them are worth
     * interrupting an upload that already succeeded.
     *
     * Inputs: blob (Blob) - the bytes that were uploaded.
     * Output: Promise<string|null> - a `data:` URL, or null.
     */
    function previewDataUrl(blob) {
        return new Promise(function (resolve) {
            if (!blob || typeof FileReader !== 'function'
                || typeof Image !== 'function'
                || !document || typeof document.createElement !== 'function') {
                resolve(null);
                return;
            }
            var reader = new FileReader();
            reader.onerror = function () { resolve(null); };
            reader.onload = function () {
                var img = new Image();
                img.onerror = function () { resolve(null); };
                img.onload = function () { resolve(drawThumb(img)); };
                // A `data:` URL, so the CSP's img-src allows it. An
                // object URL would be silently blocked here.
                img.src = String(reader.result || '');
            };
            try {
                reader.readAsDataURL(blob);
            } catch (err) {
                // Not a readable Blob (a stub, a revoked handle). The
                // chip is the answer; log rather than swallow silently.
                console.warn('[AttachmentToast] preview unavailable:',
                    err && err.message);
                resolve(null);
            }
        });
    }

    /**
     * Draw a decoded image into a THUMB_PX box and export it.
     *
     * Aspect ratio is preserved by fitting the LONGEST edge, so a wide
     * screenshot and a tall phone photo both come back inside the same
     * budget; the card crops to a square with `object-fit: cover`, which
     * keeps the subject centred rather than squashing it.
     *
     * webp is asked for first and browsers that cannot encode it hand
     * back a PNG from the same call, so both outcomes are a valid
     * `data:` URL and neither needs a branch. Alpha survives either way,
     * which JPEG would not have done - a transparent screenshot would
     * have picked up a black background.
     *
     * Inputs: img (HTMLImageElement) - already decoded.
     * Output: string|null - a `data:` URL, or null if the canvas refused.
     */
    function drawThumb(img) {
        var w = img.naturalWidth || img.width || 0;
        var h = img.naturalHeight || img.height || 0;
        if (!w || !h) return null;
        var scale = Math.min(1, THUMB_PX / Math.max(w, h));
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        var ctx = typeof canvas.getContext === 'function'
            ? canvas.getContext('2d') : null;
        if (!ctx) return null;
        try {
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            return canvas.toDataURL('image/webp', THUMB_QUALITY);
        } catch (err) {
            // Tainted canvas or an encoder that refused the type. The
            // chip covers it; never leave an empty frame behind.
            console.warn('[AttachmentToast] thumbnail draw failed:',
                err && err.message);
            return null;
        }
    }

    /**
     * Build the thumbnail strip for one card and attach it.
     *
     * Called by client/js/toast.js from `_renderCard`, which owns the
     * card and knows nothing about attachments; everything
     * attachment-shaped lives here so that file stays the notification
     * card and nothing else.
     *
     * EVERY MEMBER OF THE GROUP IS DRAWN, not just the newest. Toasts
     * for this kind coalesce on the session, so two files attached to
     * one prompt are ONE card - which is the whole reason a second
     * attachment cannot build a pile. A card that then showed only the
     * last thumbnail would be claiming, in a picture, that one file is
     * staged when two are.
     *
     * Rebuilt from scratch on every render rather than appended to:
     * `_renderCard` reuses a card element across renders, so an
     * append-only strip would accumulate a duplicate row per keystroke.
     *
     * Inputs: cardEl (HTMLElement) - the card being rendered;
     *   toasts (Array) - every toast in the group, oldest first.
     * Output: void.
     */
    function renderThumbs(cardEl, toasts) {
        if (!cardEl || !Array.isArray(toasts)) return;
        var old = cardEl.querySelector('.toast__thumbs');
        if (old) old.remove();
        var strip = document.createElement('div');
        strip.className = 'toast__thumbs';
        for (var i = 0; i < toasts.length; i++) {
            strip.appendChild(thumbRow(toasts[i]));
        }
        cardEl.appendChild(strip);
    }

    /**
     * One row of the strip: the picture (or the chip) and the name.
     *
     * Inputs: toast (object) - an attachment toast.
     * Output: HTMLElement.
     */
    function thumbRow(toast) {
        var row = document.createElement('div');
        row.className = 'toast__thumb-row';

        var frame = document.createElement('div');
        frame.className = 'toast__thumb';
        var name = String((toast && toast.attachment_name) || '');
        if (toast && toast.thumb_url) {
            var img = document.createElement('img');
            img.className = 'toast__thumb-img';
            // Named, not "thumbnail": a screen reader user gets the same
            // fact a sighted one does - WHICH file this picture is.
            img.setAttribute('alt', name || 'attached image');
            img.setAttribute('src', toast.thumb_url);
            frame.appendChild(img);
        } else {
            var chip = document.createElement('span');
            chip.className = 'toast__thumb-ext';
            // The type is already spoken by the filename beside it, so
            // the chip is decoration to a screen reader and says so.
            chip.setAttribute('aria-hidden', 'true');
            chip.textContent = String((toast && toast.thumb_label)
                || GENERIC_EXT_LABEL.toUpperCase());
            frame.appendChild(chip);
            frame.dataset.noPreview = '1';
        }
        row.appendChild(frame);

        var label = document.createElement('div');
        label.className = 'toast__thumb-name';
        label.textContent = name || '(unnamed file)';
        row.appendChild(label);
        return row;
    }

    /**
     * What the in-session header says this session is called, or ''.
     *
     * Reuses client/js/session-label.js rather than reading
     * `.textContent` directly: that span is MIDDLE-ELIDED at render
     * time, so the raw text can be `long...name` while the real label
     * sits in `dataset.fullTitle`. Returning '' is fine - the card then
     * degrades to its honest "unknown session" line.
     *
     * Inputs: none. Output: string.
     */
    function headerSessionLabel() {
        var el = document.getElementById('header-title-text');
        if (!el || !window.SessionLabel
            || typeof window.SessionLabel.seedFromElement !== 'function') {
            return '';
        }
        return window.SessionLabel.seedFromElement(el);
    }

    /**
     * Show the receipt for a file that has just finished uploading.
     *
     * Called from client/js/clipboard.js after the path has been
     * injected into the prompt, which is the moment the claim "this is
     * attached" becomes true. Awaiting the preview first would put the
     * card up late; the toast is added immediately with a chip and the
     * picture replaces it when it is ready, so a slow decode delays the
     * image and never the confirmation.
     *
     * Inputs: term (object) - the Terminal wrapper, for the session id;
     *   opts (object) - {blob, filename, sessionLabel}.
     * Output: Promise<void> - resolves once the preview has settled.
     *   Never rejects.
     */
    function show(term, opts) {
        var o = opts || {};
        var mgr = window.ToastManager;
        if (!mgr || typeof mgr.add !== 'function') return Promise.resolve();
        var sessionId = (term && typeof term._sessionId === 'function')
            ? term._sessionId() : null;
        var spec = thumbSpecFor(o.filename, o.blob && o.blob.type);
        var id = 'attach:' + (sessionId || 'none') + ':' + Date.now()
            + ':' + Math.random().toString(36).slice(2, 8);
        var record = {
            id: id,
            session_id: sessionId,
            kind: mgr.ATTACHMENT_KIND,
            title: 'attached',
            body: null,
            // No server issued this, so nothing may be acked for it.
            local: true,
            attachment_name: o.filename || '',
            thumb_label: spec.label,
            thumb_url: null,
            // WITHOUT THIS THE CARD SAYS "unknown session". Every other
            // toast is stamped with an identity by the server at record
            // time; this one has no server behind it, so the label has
            // to come from the page. The header is showing the session
            // being attached to, by definition - it is the session the
            // user is looking at - and SessionLabel.seedFromElement is
            // the existing reader for that span, middle-elision and all.
            session_label: o.sessionLabel || headerSessionLabel() || null,
        };
        mgr.add(record);

        if (!spec.image) return Promise.resolve();
        return previewDataUrl(o.blob).then(function (url) {
            if (!url) return;
            // The user may have sent the prompt while the image was
            // decoding. Re-adding a toast the manager has dropped would
            // resurrect a card that was correctly retired, so update it
            // only while it is still live.
            if (typeof mgr.updateLocal !== 'function') return;
            mgr.updateLocal(id, { thumb_url: url });
        });
    }

    /**
     * The seam terminal.js calls on every user send: the prompt was
     * sent, so every receipt for it has been spent.
     *
     * Scoped to ONE session, and that is the safety property rather
     * than a detail: a file staged in session A is still staged there
     * after the user sends a prompt in session B, and clearing A's
     * receipt on B's Enter would tell the user something false about
     * what B just sent.
     *
     * Inputs: sessionId (string); data (string) - the bytes sent.
     * Output: number - how many toasts were cleared.
     */
    function noteUserInput(sessionId, data) {
        var mgr = window.ToastManager;
        if (!sessionId || !isPromptSubmit(data)) return 0;
        if (!mgr || typeof mgr.dismissKindForSession !== 'function') return 0;
        return mgr.dismissKindForSession(mgr.ATTACHMENT_KIND, sessionId);
    }

    window.AttachmentToast = {
        THUMB_PX: THUMB_PX,
        isPromptSubmit: isPromptSubmit,
        thumbSpecFor: thumbSpecFor,
        extensionOf: extensionOf,
        renderThumbs: renderThumbs,
        show: show,
        noteUserInput: noteUserInput,
    };
})();
