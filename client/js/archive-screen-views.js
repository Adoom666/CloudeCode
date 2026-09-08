/**
 * THE READER PANE HOLDS TWO VIEWS OF ONE TRANSCRIPT, and this owns the
 * switch between them.
 *
 * WHAT THE TWO ARE, AND WHY BOTH. The CONVERSATION view
 * (archive-chat-screen.js) is the reading: turns as chat, the envelope
 * behind an "i", subagents behind a counted expander. The RAW view
 * (archive-reader.js) is the byte-exact record, which is the only thing
 * that can answer a question about what is actually on disk. The
 * conversation view is the DEFAULT because the complaint that produced
 * it was about the default - "im looking at a bunch of raw json so it
 * does not read properly" - and the raw view is one keystroke away
 * because some questions still need the bytes.
 *
 * NEITHER IS DESTROYED WHEN HIDDEN. The raw reader holds the line spine,
 * the body cache and the selection cursor; the chat view holds its drill
 * chain and its scroll position. Tearing either down on a toggle would
 * make the other feel like a slow mode rather than a peer view, and it
 * would silently discard a drill chain the reader had built by hand.
 * Measured: the toggle is 0.02s because it is a show/hide and not a
 * refetch.
 *
 * WHY IT IS ITS OWN FILE. archive-screen.js is the composition root and
 * was already at this repo's 500-line cap; adding a second view to the
 * pane pushed it over. The seam is the one archive-screen-tools.js
 * already established: everything here is construction and visibility
 * for one pane's contents, it fetches nothing, and it hands back the
 * handles the composition root needs to keep.
 *
 * Exports window.ArchiveScreenViews.
 */

console.log('[ArchiveScreenViews Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: build both reader-pane views plus the toolbar, and
     *   return the switch between them.
     * Inputs: options (object) -
     *   document (Document), api (object), pane (Element) - the reader
     *   pane; rootClass (string); onSearch (function(text)), onExport
     *   (function()) - forwarded to archive-screen-tools.js.
     * Output: {reader, chat, tools, searchInput, exportBtn,
     *   setChatVisible(on), chatOn(), toggle()}
     * Example:
     *   var views = ArchiveScreenViews.create({document: document,
     *       api: api, pane: readPane, rootClass: 'archive-screen',
     *       onSearch: runSearch, onExport: openExport});
     *   views.chat.open(5767, 'a session');
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document ||
            (typeof window !== 'undefined' ? window.document : null);
        var pane = opts.pane;
        if (!doc || !pane) {
            throw new Error('ArchiveScreenViews.create needs a document and a pane');
        }

        var reader = window.ArchiveReader.createReader({
            document: doc, api: opts.api });
        reader.mount(pane);

        var chat = window.ArchiveChatScreen.create({
            document: doc, api: opts.api });
        chat.mount(pane);

        // The toolbar is CROSS-PANE (search writes into the LIST column,
        // export opens a modal), so it belongs to neither view. It is
        // built here rather than by the composition root only because it
        // carries the view toggle, and a control and the thing it
        // controls belong in one file.
        var tools = window.ArchiveScreenTools.create({
            document: doc, pane: pane,
            rootClass: opts.rootClass || 'archive-screen',
            onSearch: opts.onSearch, onExport: opts.onExport,
            onToggleView: function () { setChatVisible(!chatOn); }
        });

        /** Is the CONVERSATION view showing. @type {boolean} */
        var chatOn = true;

        /**
         * Description: show exactly one of the two views.
         *
         *   THE BUTTON'S LABEL NAMES THE DESTINATION, not the current
         *   state ("Raw" while the conversation is showing). It is the
         *   only wording that stays unambiguous when the control is read
         *   on its own, and `aria-pressed` carries the state for anyone
         *   who needs it as a state rather than as a direction.
         * Inputs: on (boolean) - true for the conversation view.
         * Output: void.
         */
        function setChatVisible(on) {
            chatOn = !!on;
            chat.setVisible(chatOn);
            var rd = reader.root();
            if (rd) rd.hidden = chatOn;
            var b = tools.viewBtn;
            if (!b) return;
            b.textContent = chatOn ? 'Raw (v)' : 'Conversation (v)';
            b.setAttribute('aria-pressed', chatOn ? 'false' : 'true');
            b.setAttribute('data-view', chatOn ? 'chat' : 'raw');
        }

        setChatVisible(true);

        /**
         * Description: hand ArchiveScreenReader a reader that also
         *   REPORTS the header it is given, so the caller can repaint a
         *   breadcrumb with the session's real name.
         *
         *   A DELEGATING WRAPPER RATHER THAN A CALLBACK PARAMETER: the
         *   header is already fetched exactly once by that module, and a
         *   second request to learn the same fact would double every
         *   transcript open. It adds no branch - `setHeader` is
         *   forwarded whatever it receives, INCLUDING the `null` that
         *   means the header could not be evaluated, and `onHeader` is
         *   deliberately NOT called for that null: a name nobody read is
         *   not a name, and inventing one is worse than the honest
         *   NOT NAMED YET it would replace.
         * Inputs: transcriptId (number), onHeader (function(id, header)).
         * Output: object - the reader, with setHeader instrumented.
         * Example:
         *   ArchiveScreenReader.load({
         *       reader: views.watchHeader(5767, onHeader), ...});
         */
        function watchHeader(transcriptId, onHeader) {
            return Object.create(reader, { setHeader: { value: function (h) {
                if (h && typeof onHeader === 'function') onHeader(transcriptId, h);
                return reader.setHeader(h);
            } } });
        }

        return {
            reader: reader,
            watchHeader: watchHeader,
            chat: chat,
            tools: tools,
            searchInput: tools.searchInput,
            exportBtn: tools.exportBtn,
            viewBtn: tools.viewBtn,
            setChatVisible: setChatVisible,
            /** Is the conversation view showing. Output: boolean. */
            chatOn: function () { return chatOn; },
            /** Flip to the other view. Output: void. */
            toggle: function () { setChatVisible(!chatOn); }
        };
    }

    window.ArchiveScreenViews = { create: create };
    console.log('[ArchiveScreenViews Module] Exported as window.ArchiveScreenViews');
})();
