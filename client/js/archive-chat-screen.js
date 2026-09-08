/**
 * THE COMPOSITION ROOT OF THE CONVERSATION VIEW: fetching, drilling, and
 * the switch back to the byte-exact record.
 *
 * WHAT THIS OWNS THAT NOTHING ELSE DOES: the network call to the
 * messages endpoint, the interpretation of a MISSING endpoint, the drill
 * chain's fetch-per-level, and the chat/raw toggle. archive-chat-view.js
 * owns the pixels and holds no fetch; this holds no geometry.
 *
 * THE MISSING ENDPOINT IS THE STATE THIS FILE EXISTS FOR. The route this
 * view reads was built in parallel with it, so "the route is not there"
 * is a real, expected, first-class outcome rather than a hypothetical.
 * It is NOT an empty conversation and it is NOT a generic transport
 * error: it is a could-not-determine that names the endpoint, says the
 * server has no such route, and points at the raw view, which works
 * regardless. Rendering an empty chat pane would assert that a
 * transcript with 30,805 lines contains no messages, which is a verdict
 * nobody measured and the exact false green this repo's three-outcome
 * rule exists to kill.
 *
 * A 404 FROM A MISSING ROUTE AND A 404 FROM A MISSING TRANSCRIPT ARE
 * DIFFERENT FINDINGS, and they are told apart structurally: the archive
 * routes answer a missing transcript with a COMPLETE envelope carrying
 * `result_status: 'not_found'` (measured, live server, and the reason
 * callEnvelope exists at all), while an unrouted path answers FastAPI's
 * `{"detail": "Not Found"}`, which carries no `result_status` at all. So
 * the discriminator is the presence of the envelope's own status field,
 * not the HTTP code.
 *
 * DRILLING RE-FETCHES, IT DOES NOT CACHE. A subagent is a transcript and
 * is read exactly as its parent was. Caching the chain would mean a
 * level rendered from a snapshot taken before the reader drilled, and
 * the reader has no way to know which levels are stale. One request per
 * level, every time, is honest and is fast enough: these are paged
 * reads, not full transcripts.
 *
 * Depends on archive-chat-view.js, archive-outcome.js,
 * archive-outcome-view.js. Exports window.ArchiveChatScreen.
 */

console.log('[ArchiveChatScreen Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-screen';

    /** Turns requested per page. Deliberately smaller than the raw
     *  reader's spine page: a turn carries its blocks inline, so a page
     *  of turns is far more bytes than a page of spine rows.
     *  @type {number} */
    var PAGE_TURNS = 200;

    /**
     * Description: build the synthetic envelope for "this server has no
     *   such route". Synthetic because there is no envelope to classify:
     *   the server never produced one. Everything about it is stated,
     *   including that it is this client's own construction.
     * Inputs: transcriptId (*), httpStatus (number|null),
     *   transportError (string|null).
     * Output: object - an envelope archive-outcome.js classifies as
     *   cannot-determine.
     */
    function noRouteEnvelope(transcriptId, httpStatus, transportError) {
        return {
            result: null,
            result_status: 'cannot_determine',
            scope_status: 'resolved',
            unevaluated: [{
                subject: 'GET /archive/transcripts/' + transcriptId + '/messages',
                reason: 'this server answered ' +
                    (httpStatus === null ? 'nothing' : 'HTTP ' + httpStatus) +
                    ' with no archive envelope' +
                    (transportError ? ' (' + transportError + ')' : '') +
                    '. The conversation view could not be built. This is NOT ' +
                    'a claim that the transcript is empty. The raw view reads ' +
                    'the same transcript from a different endpoint and is ' +
                    'unaffected.'
            }],
            meta: {}
        };
    }

    /**
     * Description: pull the turns out of a messages envelope, whatever
     *   shape the server chose. `result` may BE the array or may hold it
     *   under `turns`; both are accepted, and neither is invented.
     * Inputs: envelope (object|null).
     * Output: Array|null - null means the envelope was renderable but
     *   carried no array, which is a could-not-determine of its own
     *   rather than an empty conversation.
     * Example: turnsOf({result: {turns: []}}) // -> []
     */
    function turnsOf(envelope) {
        if (!envelope || typeof envelope !== 'object') return null;
        var r = envelope.result;
        if (Array.isArray(r)) return r;
        if (r && typeof r === 'object' && Array.isArray(r.turns)) return r.turns;
        return null;
    }

    /**
     * Description: the cursor for the next page, or null when there is
     *   none. A cursor that is not a non-empty string is NOT a cursor,
     *   and sending one would be asking the server a question built out
     *   of whatever happened to be in that field.
     * Inputs: envelope (object|null).
     * Output: string|null.
     * Example: nextCursorOf({meta: {paging: {next_cursor: 'eyJ9'}}})
     *   // -> 'eyJ9'
     */
    function nextCursorOf(envelope) {
        if (!envelope || typeof envelope !== 'object') return null;
        var meta = envelope.meta;
        if (!meta || typeof meta !== 'object') return null;
        var paging = meta.paging;
        if (!paging || typeof paging !== 'object') return null;
        var c = paging.next_cursor;
        return (typeof c === 'string' && c !== '') ? c : null;
    }

    /**
     * Description: build the conversation screen.
     * Inputs: options (object) -
     *   document (Document), api (object) - anything exposing
     *   listArchiveMessages; pane (Element) - where to mount.
     * Output: object - {element, open, openLevel, setVisible, visible,
     *   view, destroy}.
     * Example:
     *   var chat = ArchiveChatScreen.create({api: window.API});
     *   chat.mount(readPane);
     *   chat.open(4, 'session name');
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document ||
            (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveChatScreen.create needs a document');
        var api = opts.api;

        var view = window.ArchiveChatView.create({
            document: doc,
            onOpenSubagent: function (spec) { drillInto(spec); },
            onChainUp: function (level, index) { goUp(index); },
            onLoadMore: function () { return loadMore(); },
            canLoadMore: function () { return cursor !== null; }
        });

        /** The cursor for the NEXT page of the level being shown, or
         *  null when there is none to ask for. Reset on every
         *  navigation, because a cursor belongs to one transcript. */
        var cursor = null;

        // NO WRAPPER ELEMENT. An extra <div> around the view broke the
        // HEIGHT CHAIN: `.archive-chat` asks for `height: 100%`, the
        // wrapper had no height of its own, so 100% resolved against an
        // auto-height box and the scroller grew to the full content
        // height instead of the viewport's. Measured in a real browser
        // on transcript 5767: scrollHeight 23369 === clientHeight 23369,
        // scrollTop pinned at 0 through forty wheel events. Nothing
        // errored and bubbles painted correctly, so every check short of
        // actually scrolling passed. The view's own root IS the screen's
        // root now, exactly as the raw reader mounts into the pane.
        var root = null;
        /** Guards against a slow response for a level nobody is on any
         *  more. Incremented on every navigation; a response whose ticket
         *  no longer matches is DISCARDED rather than rendered, because a
         *  late answer painted over the current level is a wrong
         *  conversation that looks entirely plausible. */
        var ticket = 0;

        /**
         * Description: fetch and render whatever level the chain
         *   currently names.
         * Inputs: none. Output: Promise<string> - the token rendered.
         */
        function loadCurrent() {
            var lvl = view.stack().current();
            if (!lvl || lvl.transcriptId === null) {
                view.setToken('cannot-determine', noRouteEnvelope(
                    'unknown', null, 'no transcript id for this level'));
                return Promise.resolve('cannot-determine');
            }
            var mine = ++ticket;
            var id = lvl.transcriptId;
            cursor = null;
            view.setTurns([], null);
            view.setToken('loading', null);

            if (!api || typeof api.listArchiveMessages !== 'function') {
                view.setToken('cannot-determine',
                    noRouteEnvelope(id, null, 'this build has no ' +
                        'listArchiveMessages on its API client'));
                return Promise.resolve('cannot-determine');
            }

            return api.listArchiveMessages(id, { limit: PAGE_TURNS })
                .then(function (r) {
                    if (mine !== ticket) return 'superseded';
                    var env = r ? r.envelope : null;
                    // THE DISCRIMINATOR: an archive route always answers
                    // with a result_status, even when it is refusing.
                    // A body without one did not come from the archive
                    // API, so the route is absent or something else is
                    // answering for it.
                    var isEnvelope = env && typeof env === 'object' &&
                        typeof env.result_status === 'string';
                    if (!isEnvelope) {
                        var e = noRouteEnvelope(id,
                            r ? r.httpStatus : null,
                            r ? r.transportError : null);
                        view.setToken('cannot-determine', e);
                        return 'cannot-determine';
                    }
                    var c = window.ArchiveOutcome.classify(env);
                    if (!window.ArchiveOutcome.isRenderable(c.token)) {
                        view.setToken(c.token, env);
                        return c.token;
                    }
                    var rows = turnsOf(env);
                    if (rows === null) {
                        view.setToken('cannot-determine', {
                            result: null, result_status: 'cannot_determine',
                            scope_status: 'resolved',
                            unevaluated: [{
                                subject: 'transcript ' + id + ' messages',
                                reason: 'the server reported success but the ' +
                                    'response carried no turns array, so what ' +
                                    'this conversation contains is NOT KNOWN.'
                            }],
                            meta: env.meta || {}
                        });
                        return 'cannot-determine';
                    }
                    // COMPLETENESS IS THREE-OUTCOME AND IS PASSED
                    // THROUGH AS ONE. `hasMore` returns null on every
                    // failure path, and null is NOT false: treating it as
                    // false would end the conversation on the strength of
                    // a number nobody read.
                    var more = window.ArchiveOutcome.hasMore(env);
                    cursor = nextCursorOf(env);
                    view.setTurns(rows, more === false ? true
                        : (more === true ? false : null));
                    view.setToken(c.token, env);
                    return c.token;
                });
        }

        /**
         * Description: fetch the NEXT page of the level being shown and
         *   APPEND it. Never replaces: the turns already on screen are
         *   the ones the reader is reading.
         *
         *   A FAILED PAGE DOES NOT WIPE THE CONVERSATION. It leaves the
         *   loaded turns exactly where they are and flips completeness
         *   back to NOT KNOWN, so the sentinel says the truth - some
         *   turns are loaded and whether there are more could not be
         *   established this time - rather than either claiming the end
         *   or blanking the pane.
         * Inputs: none. Output: Promise<string>.
         */
        function loadMore() {
            var lvl = view.stack().current();
            if (!lvl || lvl.transcriptId === null || cursor === null) {
                return Promise.resolve('no-cursor');
            }
            if (!api || typeof api.listArchiveMessages !== 'function') {
                return Promise.resolve('no-api');
            }
            var mine = ticket;
            var id = lvl.transcriptId;
            var ask = cursor;
            return api.listArchiveMessages(id, { limit: PAGE_TURNS, cursor: ask })
                .then(function (r) {
                    if (mine !== ticket) return 'superseded';
                    var env = r ? r.envelope : null;
                    var ok = env && typeof env === 'object' &&
                        typeof env.result_status === 'string';
                    if (!ok) { view.appendTurns([], null); return 'cannot-determine'; }
                    var c = window.ArchiveOutcome.classify(env);
                    if (!window.ArchiveOutcome.isRenderable(c.token)) {
                        view.appendTurns([], null);
                        return c.token;
                    }
                    var rows = turnsOf(env);
                    if (rows === null) { view.appendTurns([], null); return 'cannot-determine'; }
                    var more = window.ArchiveOutcome.hasMore(env);
                    cursor = nextCursorOf(env);
                    view.appendTurns(rows, more === false ? true
                        : (more === true ? false : null));
                    return c.token;
                }, function () {
                    view.appendTurns([], null);
                    return 'cannot-determine';
                });
        }

        /**
         * Description: descend into a subagent. The chain grows; the
         *   fetch is the same one the parent used.
         * Inputs: spec (object) - {transcriptId, agentId, label, ordinal}.
         * Output: Promise<string>.
         */
        function drillInto(spec) {
            var s = spec || {};
            view.stack().push({
                transcriptId: s.transcriptId === undefined ? null : s.transcriptId,
                label: s.label, agentId: s.agentId, ordinal: s.ordinal
            });
            return loadCurrent();
        }

        /**
         * Description: go back up to one level of the chain, dropping
         *   everything below it.
         * Inputs: index (number). Output: Promise<string>|null - null
         *   when the index named no level, which is a no-op rather than
         *   a truncation to nothing.
         */
        function goUp(index) {
            if (view.stack().truncateTo(index) === null) return null;
            return loadCurrent();
        }

        return {
            /** The screen root, null before mount(). Output: Element|null. */
            element: function () { return root; },
            /**
             * Description: attach to a host and mount the view inside.
             * Inputs: host (Element). Output: Element.
             */
            mount: function (host) {
                root = view.mount(host);
                return root;
            },
            /**
             * Description: open a transcript at the TOP of a new chain.
             *   Opening from the list is a new question, not a step
             *   deeper into the previous one, so the chain is reset.
             * Inputs: transcriptId (*), label (string|null).
             * Output: Promise<string>.
             */
            open: function (transcriptId, label) {
                view.stack().reset({
                    transcriptId: transcriptId === undefined ? null : transcriptId,
                    label: (typeof label === 'string' && label !== '') ? label : null
                });
                return loadCurrent();
            },
            /** Re-fetch the level currently shown. Output: Promise. */
            reload: loadCurrent,
            /**
             * Description: give the ROOT level of the chain the name the
             *   header carries, once it arrives. A no-op unless the root
             *   is the transcript being named AND the view is still on
             *   it, so a late header cannot relabel a level the reader
             *   has already drilled into.
             * Inputs: transcriptId (*), header (object|null).
             * Output: boolean - whether anything was renamed.
             */
            nameRoot: function (transcriptId, header) {
                var levels = view.stack().levels();
                if (!levels.length) return false;
                if (String(levels[0].transcriptId) !== String(transcriptId)) return false;
                var name = header && (header.title || header.session_ref);
                if (typeof name !== 'string' || name === '') return false;
                if (levels[0].label === name) return false;
                levels[0].label = name;
                view.stack().reset(levels[0]);
                for (var i = 1; i < levels.length; i++) view.stack().push(levels[i]);
                view.schedule();
                return true;
            },
            /**
             * Description: show or hide the whole screen. Hidden rather
             *   than destroyed, so the toggle back is instant and the
             *   drill chain survives a trip to the raw view.
             * Inputs: on (boolean). Output: void.
             */
            setVisible: function (on) {
                if (root) root.hidden = !on;
                if (on) view.schedule();
            },
            /** Is the chat screen showing. Output: boolean. */
            visible: function () { return !!root && root.hidden !== true; },
            /** The underlying view, for callers and tests. Output: object. */
            view: view,
            /** Detach. Output: void. */
            destroy: function () {
                view.destroy();
                root = null;
            }
        };
    }

    window.ArchiveChatScreen = {
        create: create,
        turnsOf: turnsOf,
        nextCursorOf: nextCursorOf,
        noRouteEnvelope: noRouteEnvelope,
        ROOT_CLASS: ROOT_CLASS,
        PAGE_TURNS: PAGE_TURNS
    };
    console.log('[ArchiveChatScreen Module] Exported as window.ArchiveChatScreen');
})();
