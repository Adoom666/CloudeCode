/**
 * Archive export - preflight, the three integrity outcomes, the filename
 * collision warning, and the reason there is no Download button.
 *
 * THE BLOCKER, MEASURED LIVE AND RE-MEASURED 2026-08-31 BEFORE WRITING
 * THIS FILE. The export endpoints authenticate with `HTTPBearer` and
 * nothing else (src/api/auth.py). Against
 * GET /api/v1/archive/transcripts/4/export/verified:
 *
 *   Authorization: Bearer <jwt>   200
 *   no auth header                401
 *   ?token=<jwt>                  401
 *   ?access_token=<jwt>           401
 *   Cookie: access_token=<jwt>    401
 *   POST .../export/ticket        404   (no ticket route exists)
 *
 * A browser NAVIGATION - `window.location = href`, an `<a download>`
 * click - sends no Authorization header. So a navigation download gets a
 * 401 and the browser saves or displays an error page. The download
 * cannot currently authenticate AT ALL.
 *
 * WHY THE ANSWER IS NOT A TOKEN IN THE URL. It would work, and it would
 * put a live credential in browser history, in the server access log and
 * in any Referer. That is the owner's call to make, not this file's, so
 * no such scheme is invented here.
 *
 * WHY THE ANSWER IS NOT fetch-INTO-A-BLOB EITHER. A tab cannot both
 * stream to disk and hash the bytes. Buffering defeats the entire point:
 * transcript 17266 is 244,117,661 bytes, and the API's own 413 text says
 * buffering the 91 MB one "would peak near 1052 MB".
 *
 * SO THIS FILE IMPLEMENTS `blocked-no-credential`. The modal opens, does
 * the preflight, reports the integrity outcome truthfully, and then
 * states precisely why the download cannot be started and what would
 * unblock it, with a copyable curl carrying the expected sha256. It does
 * NOT render a button that produces a 401. A button that cannot work is
 * worse than a stated blocker, because the blocker is at least
 * information.
 *
 * THE THREE INTEGRITY OUTCOMES, and they are three, not two:
 *
 *   VERIFIED       <= 8 MiB, 98.9% of transcripts. Headers carry BOTH
 *                  x-archive-expected-sha256 AND x-archive-actual-sha256
 *                  with x-archive-verified: true. The server hashed what
 *                  it is about to send. This is the ONLY state that may
 *                  be styled as success.
 *   NOT VERIFIABLE > 8 MiB. Streams. uvicorn implements no HTTP
 *                  trailers - the server says so in
 *                  x-archive-trailer-unavailable - so there is NO hash
 *                  of what was actually sent. This is a COULD NOT
 *                  EVALUATE. Not dismissible, no green, no checkmark,
 *                  and it carries the shasum command so the person can
 *                  do the measurement the server could not.
 *   BUSY           503. Two concurrent exports are the cap and no slot
 *                  came free in 30s. NOTHING FAILED and nothing was
 *                  downloaded; the server declined to start. It renders
 *                  as busy with a Retry and never as "download failed".
 *
 * A 413 IS NOT AN ERROR HERE. Verified export of a large transcript
 * answers 413 with a cannot_determine envelope carrying
 * `meta.stream_href`. That is the server routing you to the streaming
 * path, so this file transitions straight to NOT VERIFIABLE for that
 * href rather than showing a failure.
 *
 * FILENAME COLLISIONS ARE REAL. content-disposition is derived from
 * `session_ref`, and measured: `journal` names 14 different transcripts,
 * `audit` 5, `agent-a877057` 4. Fourteen different files all download as
 * journal.jsonl. The modal warns when the name is known to collide.
 *
 * Does not read `result_status` or `scope_status`.
 */

console.log('[ArchiveExport Module] Loading...');

(function () {
    'use strict';

    /** Root element class. @type {string} */
    var ROOT_CLASS = 'archive-export';

    /**
     * The states this modal can be in. `blocked-no-credential` is a
     * state of the DOWNLOAD, orthogonal to the integrity states, and is
     * rendered alongside them rather than instead of them: the integrity
     * finding is true and useful whether or not the bytes can be fetched.
     * @type {Object<string,string>}
     */
    var STATES = {
        PREFLIGHT: 'preflight',
        VERIFIED: 'verified',
        UNVERIFIABLE: 'unverifiable',
        BUSY: 'busy',
        NOT_FOUND: 'not-found',
        CANNOT_DETERMINE: 'cannot-determine',
        BLOCKED_NO_CREDENTIAL: 'blocked-no-credential'
    };

    /** HTTP status the server uses for "too large to verify". @type {number} */
    var HTTP_TOO_LARGE = 413;
    /** HTTP status the server uses for "already at the concurrency cap". @type {number} */
    var HTTP_BUSY = 503;
    /** HTTP status for a transcript id that is not in the archive. @type {number} */
    var HTTP_NOT_FOUND = 404;

    /**
     * Description: build an element with a class and optional text.
     * Inputs: doc (Document), tag (string), cls (string|null), text (any).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: read one response header from whatever the transport
     *   handed back. Accepts a real Headers object or a plain map, so a
     *   test can pass an object literal.
     * Inputs: headers (object|null), name (string) - lowercase.
     * Output: string|null.
     */
    function header(headers, name) {
        if (!headers) return null;
        if (typeof headers.get === 'function') {
            var v = headers.get(name);
            return typeof v === 'string' ? v : null;
        }
        var direct = headers[name];
        return typeof direct === 'string' ? direct : null;
    }

    /**
     * Description: classify a preflight response into one integrity
     *   state. Pure, so a test can drive every branch from captured
     *   responses without a DOM.
     *
     *   THE 413 BRANCH IS NOT A FAILURE BRANCH. The server answers 413
     *   with a cannot_determine envelope carrying meta.stream_href; that
     *   is a redirect to the streaming path, and the honest state for it
     *   is UNVERIFIABLE, not an error.
     *
     * Inputs: r (object) - a callEnvelope result:
     *   {envelope, httpStatus, headers, transportError}.
     * Output: {state, expectedSha, actualSha, expectedBytes, filename,
     *          streamHref, verified, reason}
     *   `verified` is true ONLY when the server said so in a header AND
     *   both hashes are present and equal. It is never inferred from a
     *   200.
     * Example:
     *   classifyPreflight({httpStatus: 503, envelope: busyEnvelope})
     *   // -> {state: 'busy', ...}
     */
    function classifyPreflight(r) {
        var res = r || {};
        var base = {
            state: STATES.CANNOT_DETERMINE, expectedSha: null, actualSha: null,
            expectedBytes: null, filename: null, streamHref: null,
            verified: false, reason: ''
        };
        if (res.transportError) {
            base.reason = 'The preflight request did not complete: ' + res.transportError +
                          '. Nothing is known about this export.';
            return base;
        }
        var status = res.httpStatus;
        var envelope = res.envelope || {};
        var meta = (envelope.meta && typeof envelope.meta === 'object') ? envelope.meta : {};

        if (status === HTTP_BUSY) {
            base.state = STATES.BUSY;
            base.reason = 'The server declined to start this export right now. ' +
                'Nothing was downloaded and nothing failed. Retry.';
            return base;
        }
        if (status === HTTP_NOT_FOUND) {
            base.state = STATES.NOT_FOUND;
            base.reason = 'The archive was read and this transcript id is not in it.';
            return base;
        }
        if (status === HTTP_TOO_LARGE) {
            base.state = STATES.UNVERIFIABLE;
            base.streamHref = typeof meta.stream_href === 'string' ? meta.stream_href : null;
            base.reason = 'This file is too large to hash before sending, so the server ' +
                'routed it to the streaming path. Streaming carries no hash of what was ' +
                'actually sent: uvicorn implements no HTTP trailers. This download would ' +
                'be NOT VERIFIED. It is also not known to be corrupt.';
            return base;
        }

        base.expectedSha = header(res.headers, 'x-archive-expected-sha256');
        base.actualSha = header(res.headers, 'x-archive-actual-sha256');
        base.expectedBytes = header(res.headers, 'x-archive-expected-bytes');
        base.filename = filenameFrom(header(res.headers, 'content-disposition'));
        var claimed = header(res.headers, 'x-archive-verified');
        var trailerless = header(res.headers, 'x-archive-trailer-unavailable');

        if (claimed === 'true' && base.expectedSha && base.actualSha &&
                base.expectedSha === base.actualSha) {
            base.state = STATES.VERIFIED;
            base.verified = true;
            base.reason = 'The server hashed the bytes it is about to send and they match ' +
                'the stored hash. This is a measurement, not a claim about the transfer.';
            return base;
        }
        if (trailerless || (base.expectedSha && !base.actualSha)) {
            base.state = STATES.UNVERIFIABLE;
            base.reason = 'This export streams and carries no hash of what was actually ' +
                'sent: ' + (trailerless || 'the server supplied no actual-hash header') +
                '. NOT VERIFIED. Also not known to be corrupt.';
            return base;
        }
        base.reason = 'The preflight returned HTTP ' + String(status) +
            ' without the headers that establish integrity either way, so whether this ' +
            'export can be trusted is NOT KNOWN.';
        return base;
    }

    /**
     * Description: pull the filename out of a content-disposition header.
     * Inputs: value (string|null).
     * Output: string|null.
     * Example: filenameFrom('attachment; filename="a.jsonl"') // -> 'a.jsonl'
     */
    function filenameFrom(value) {
        if (typeof value !== 'string') return null;
        var m = /filename="([^"]*)"/.exec(value);
        return m ? m[1] : null;
    }

    /**
     * Description: whether the download can be started from a browser at
     *   all. Separated out and named so the reason lives in exactly one
     *   place and a future ticket route flips one function, not five
     *   call sites.
     * Inputs: none.
     * Output: {canDownload: boolean, reason: string}
     * Example: downloadCapability().canDownload  // -> false
     */
    function downloadCapability() {
        return {
            canDownload: false,
            reason: 'The export endpoints accept an Authorization: Bearer header and ' +
                'nothing else. Measured 2026-08-31: no auth, ?token=, ?access_token= and ' +
                'a cookie all answer 401, and there is no ticket route. A browser ' +
                'navigation or an <a download> click sends no Authorization header, so it ' +
                'would receive a 401 and save an error page. Unblocking this needs a ' +
                'short-lived single-use download ticket on the API, which is the owner\'s ' +
                'call because the alternative - a JWT in the URL - puts a live credential ' +
                'in browser history and server logs.'
        };
    }

    /**
     * Description: the command that does, off-machine, the measurement
     *   the server could not do for a streamed export.
     * Inputs: filename (string|null), expectedSha (string|null).
     * Output: string.
     */
    function shasumCommand(filename, expectedSha) {
        return 'shasum -a 256 ' + (filename || '<file>') +
               '\n# expect: ' + (expectedSha || 'NOT KNOWN - the server supplied no expected hash');
    }

    /**
     * Description: the collision warning, or null when the name is
     *   unique among the ids supplied.
     *
     *   The caller passes the count because only it has the listing.
     *   This function does not guess: a count it was not given is
     *   reported as unknown, not as unique.
     *
     * Inputs: filename (string|null), sameNameCount (number|null) - how
     *   many transcripts in the archive download under this name.
     * Output: string|null.
     * Example: collisionWarning('journal.jsonl', 14)
     */
    function collisionWarning(filename, sameNameCount) {
        if (!filename) return null;
        if (typeof sameNameCount !== 'number') {
            return 'Whether other transcripts download under the name ' + filename +
                   ' is NOT KNOWN. The filename comes from session_ref, which is not ' +
                   'unique in this archive: `journal` names 14 different transcripts.';
        }
        if (sameNameCount <= 1) return null;
        return sameNameCount + ' transcripts in this archive download as ' + filename +
               '. Your browser will save this as a numbered variant or overwrite an ' +
               'existing file, depending on its settings. The name comes from session_ref, ' +
               'which is a label and not an identity.';
    }

    /**
     * Description: render the body of the export modal for one
     *   classified preflight. Split out from open() so a test can render
     *   every state without a modal stack.
     * Inputs: doc (Document), info (object) - classifyPreflight() output.
     *         opts (object) - {sameNameCount: number|null,
     *                          transcriptId: number|string,
     *                          onRetry: function}
     * Output: Element.
     */
    function renderBody(doc, info, opts) {
        var options = opts || {};
        var body = el(doc, 'div', ROOT_CLASS + '__body', null);
        body.setAttribute('data-export-state', info.state);

        var labels = {};
        labels[STATES.VERIFIED] = 'INTEGRITY VERIFIED BEFORE SENDING';
        labels[STATES.UNVERIFIABLE] = 'INTEGRITY: COULD NOT BE EVALUATED';
        labels[STATES.BUSY] = 'THE SERVER IS BUSY';
        labels[STATES.NOT_FOUND] = 'NOT FOUND';
        labels[STATES.CANNOT_DETERMINE] = 'COULD NOT EVALUATE';
        labels[STATES.PREFLIGHT] = 'CHECKING';

        body.appendChild(el(doc, 'p', ROOT_CLASS + '__label',
            labels[info.state] || String(info.state)));
        body.appendChild(el(doc, 'p', ROOT_CLASS + '__reason', info.reason));

        if (info.filename) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__filename', info.filename));
        }
        if (info.expectedBytes) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__bytes',
                info.expectedBytes + ' bytes'));
        }
        if (info.expectedSha) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__sha-expected',
                'expected sha256 ' + info.expectedSha));
        }
        if (info.actualSha) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__sha-actual',
                'actual sha256 ' + info.actualSha));
        }

        if (info.state === STATES.UNVERIFIABLE) {
            // NORMATIVE: this block is not dismissible and is never
            // styled as success. It carries the command that performs
            // the measurement the server could not.
            var shas = el(doc, 'pre', ROOT_CLASS + '__shasum',
                shasumCommand(info.filename, info.expectedSha));
            shas.setAttribute('data-not-dismissible', 'true');
            body.appendChild(shas);
        }

        var collision = collisionWarning(info.filename, options.sameNameCount);
        if (collision) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__collision', collision));
        }

        // The download blocker, rendered on every state where a download
        // would otherwise be offered.
        var cap = downloadCapability();
        if (!cap.canDownload && (info.state === STATES.VERIFIED ||
                                 info.state === STATES.UNVERIFIABLE)) {
            var blocked = el(doc, 'div', ROOT_CLASS + '__blocked', null);
            blocked.setAttribute('data-export-state', STATES.BLOCKED_NO_CREDENTIAL);
            blocked.appendChild(el(doc, 'p', ROOT_CLASS + '__blocked-label',
                'DOWNLOAD BLOCKED: NO CREDENTIAL A BROWSER CAN SEND'));
            blocked.appendChild(el(doc, 'p', ROOT_CLASS + '__blocked-reason', cap.reason));
            body.appendChild(blocked);
        }

        var actions = el(doc, 'div', ROOT_CLASS + '__actions', null);
        if (info.state === STATES.BUSY) {
            var retry = el(doc, 'button', ROOT_CLASS + '__retry', 'Retry');
            retry.setAttribute('type', 'button');
            retry.setAttribute('data-action', 'retry');
            if (typeof options.onRetry === 'function') {
                retry.addEventListener('click', function () { options.onRetry(); });
            }
            actions.appendChild(retry);
        }
        var cancel = el(doc, 'button', ROOT_CLASS + '__cancel', 'Cancel');
        cancel.setAttribute('type', 'button');
        cancel.setAttribute('data-action', 'cancel');
        actions.appendChild(cancel);
        body.appendChild(actions);
        return body;
    }

    /**
     * Description: open the export modal for one transcript, run the
     *   preflight and render the outcome. Registers with ModalStack so
     *   Escape routes here and the focus that was taken is given back.
     * Inputs: options (object) - {document, api, transcriptId,
     *   sameNameCount: number|null}.
     * Output: {overlay, close, refresh} - `refresh` re-runs the
     *   preflight, which is what Retry does on a 503.
     */
    function open(options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveExport.open needs a document');
        var api = opts.api;

        var overlay = el(doc, 'div', 'modal-overlay ' + ROOT_CLASS + '-overlay', null);
        overlay.setAttribute('data-modal', 'archive-export');
        var content = el(doc, 'div', 'modal-content ' + ROOT_CLASS + '__content', null);
        content.setAttribute('role', 'dialog');
        content.setAttribute('aria-modal', 'true');
        var head = el(doc, 'div', 'modal-header ' + ROOT_CLASS + '__header',
            'Export transcript ' + opts.transcriptId);
        var slot = el(doc, 'div', 'modal-body ' + ROOT_CLASS + '__slot', null);
        content.appendChild(head);
        content.appendChild(slot);
        overlay.appendChild(content);

        /**
         * Description: tear the modal down and deregister it.
         * Inputs: none. Output: void.
         */
        function close() {
            if (window.ModalStack) window.ModalStack.pop(overlay);
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        }

        /**
         * Description: run (or re-run) the preflight and repaint.
         * Inputs: none. Output: Promise<string> - the state token.
         */
        function refresh() {
            slot.textContent = '';
            slot.appendChild(renderBody(doc,
                { state: STATES.PREFLIGHT, reason: 'Checking the export headers.',
                  expectedSha: null, actualSha: null, expectedBytes: null,
                  filename: null, streamHref: null, verified: false },
                { sameNameCount: opts.sameNameCount }));
            return Promise.resolve(api.preflightArchiveExport(opts.transcriptId, { verified: true }))
                .then(function (r) {
                    var info = classifyPreflight(r);
                    slot.textContent = '';
                    slot.appendChild(renderBody(doc, info, {
                        sameNameCount: opts.sameNameCount,
                        transcriptId: opts.transcriptId,
                        onRetry: refresh
                    }));
                    slot.querySelector('[data-action="cancel"]').addEventListener('click', close);
                    return info.state;
                });
        }

        if (doc.body) doc.body.appendChild(overlay);
        if (window.ModalStack) window.ModalStack.push(overlay, { onEscape: close });
        var started = refresh();
        return { overlay: overlay, close: close, refresh: refresh, ready: started };
    }

    /**
     * Description: open the export modal for whatever transcript is
     *   currently on screen, refusing VISIBLY when that is nothing.
     *
     *   The refusal lives here rather than at the call site because this
     *   module owns what an export needs, and a button that does nothing
     *   is worse than one that says why. The caller passes the id it has
     *   - possibly null - and gets a named answer either way.
     *
     * Inputs: options (object) - {document, api, transcriptId,
     *   sameNameCount}, the shape open() takes. `transcriptId` may be
     *   null, which is the refusal case.
     * Output: object|null - open()'s handle, or null when nothing was
     *   opened. Null is a real answer and is logged, not swallowed.
     * Example: ArchiveExport.openFor({document: document, api: API,
     *   transcriptId: 5767, sameNameCount: null})
     */
    function openFor(options) {
        var opts = options || {};
        if (opts.transcriptId === null || opts.transcriptId === undefined) {
            console.warn('ArchiveExport: export requested with no transcript open');
            return null;
        }
        return open(opts);
    }

    window.ArchiveExport = {
        open: open,
        openFor: openFor,
        classifyPreflight: classifyPreflight,
        renderBody: renderBody,
        downloadCapability: downloadCapability,
        collisionWarning: collisionWarning,
        shasumCommand: shasumCommand,
        filenameFrom: filenameFrom,
        STATES: STATES,
        ROOT_CLASS: ROOT_CLASS
    };
    console.log('[ArchiveExport Module] Exported as window.ArchiveExport');
})();
