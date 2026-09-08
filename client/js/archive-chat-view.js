/**
 * THE CONVERSATION VIEW: a virtualized, drillable chat over one
 * transcript's turns.
 *
 * THIS IS THE VIEW THE WHOLE FEATURE EXISTS FOR. The raw reader is the
 * byte-exact record and stays one keystroke away, because some questions
 * can only be answered by the bytes. But "im looking at a bunch of raw
 * json so it does not read properly" is a defect report about the
 * DEFAULT, and the default is now this.
 *
 * WHAT THIS FILE OWNS: the rAF paint loop, the render window, the
 * anti-jump reconciliation, the one delegated click listener, the
 * per-turn open-panel state, and the outcome token. It owns no markup
 * for a turn (archive-chat-turn.js), no geometry maths
 * (archive-virtual-list.js), no height policy
 * (archive-chat-estimate.js), no masking (archive-mask.js via
 * archive-chat-block.js), no state markup (archive-outcome-view.js) and
 * no interpretation of a server status (archive-outcome.js).
 *
 * VARIABLE-HEIGHT ROWS ARE THE HARD PART, and the engine already solved
 * it - what changed is how badly the FIRST guess can be wrong. A raw
 * line's height is a function of one number. A bubble's is a function of
 * its block mix and of which panels the reader has opened, so the
 * initial error per row is larger and, crucially, it CHANGES when
 * somebody clicks. Two consequences are handled here and nowhere else:
 *
 *   1. Every panel toggle calls `list.measure(index, ...)` indirectly by
 *      re-estimating that ONE row before the repaint, so the row grows in
 *      the geometry in the same frame it grows on screen. Without that,
 *      opening an envelope panel on a row above the viewport shoves the
 *      content under the reader's eyes down by 380 pixels.
 *   2. reconcileMeasured() is called on EVERY paint, not only after a
 *      data change, because a bubble's real height also depends on the
 *      viewport width and therefore changes on resize with no state
 *      change at all.
 *
 * THE MISSING ENDPOINT IS A THIRD OUTCOME, NOT AN EMPTY CHAT. If the
 * messages route does not exist yet, or answers with anything this
 * client cannot classify as renderable, the view says so through
 * archive-outcome-view.js. A blank conversation pane would claim the
 * transcript is empty, which is a verdict nobody measured.
 *
 * NO PER-ROW LISTENERS. Rows are recycled on every paint.
 *
 * Depends on archive-virtual-list.js, archive-outcome.js,
 * archive-outcome-view.js, archive-chat-turn.js,
 * archive-chat-estimate.js, archive-chat-stack.js.
 * Exports window.ArchiveChatView.
 */

console.log('[ArchiveChatView Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat';

    /** Viewport height used while the scroller is detached and reports
     *  clientHeight 0. Named rather than silently producing a one-row
     *  window. @type {number} */
    var FALLBACK_VIEWPORT_PX = 600;

    /** @type {string} data-action on the pager at the end of a partial
     *  conversation. */
    var ACTION_LOAD_MORE = 'chat-load-more';

    /**
     * Description: build a chat view. Nothing is fetched here; the
     *   composition root (archive-chat-screen.js) owns the network and
     *   feeds this turns and a token.
     * Inputs: options (object) -
     *   document (Document) - defaults to window.document.
     *   onOpenSubagent (function(spec)) - called with
     *     {transcriptId, agentId, label, ordinal} when a subagent row is
     *     activated. The view does NOT fetch; it reports the intent.
     *   onChainUp (function(level, index)) - called when a breadcrumb
     *     level is chosen.
     *   onProgressToggle (function(index, on)) - optional.
     * Output: object - see the return block.
     * Example:
     *   var chat = ArchiveChatView.create({onOpenSubagent: openSub});
     *   chat.mount(pane);
     *   chat.setTurns(turns);
     *   chat.setToken('ok', envelope);
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document ||
            (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveChatView.create needs a document');

        var VL = window.ArchiveVirtualList;
        var OV = window.ArchiveOutcomeView;
        var TURN = window.ArchiveChatTurn;
        var SUB = window.ArchiveChatSubagents;
        var EST = window.ArchiveChatEstimate;
        var STACK = window.ArchiveChatStack;

        var onOpenSubagent = typeof opts.onOpenSubagent === 'function'
            ? opts.onOpenSubagent : function () {};
        var onChainUp = typeof opts.onChainUp === 'function'
            ? opts.onChainUp : function () {};
        var onLoadMore = typeof opts.onLoadMore === 'function'
            ? opts.onLoadMore : null;
        /** Can a further page actually be ASKED FOR right now. Separate
         *  from `complete` because "there is more" and "I hold a cursor
         *  that would fetch it" are two different facts: a server can
         *  say has_more without handing back a next_cursor, and a pager
         *  with nothing to send is a control that cannot work - which is
         *  furniture, not an affordance. Defaults to "yes" only when a
         *  handler exists at all. */
        var canLoadMore = typeof opts.canLoadMore === 'function'
            ? opts.canLoadMore : function () { return !!onLoadMore; };

        /** @type {Array} the turns being shown. */
        var turns = [];
        /** index -> {infoOpen, subOpen, progressExpanded}. Sparse on
         *  purpose: an untouched row has no entry and no allocation. */
        var open = {};
        /** One of the six outcome tokens, or 'idle' / 'loading'. */
        var token = 'idle';
        /** Are these ALL the turns. THREE-OUTCOME: `true` means the
         *  server said there is no more, `false` means it said there is,
         *  and `null` means it did not say - which is not the same as
         *  "no more" and must never render as the end of a conversation. */
        var complete = null;
        /** Is a further page in flight, so the pager cannot be pressed
         *  twice into two overlapping appends. */
        var loadingMore = false;
        /** The envelope backing a failure token. */
        var envelope = null;
        var stack = STACK.create();

        var root = null, chainEl = null, statusEl = null,
            scroller = null, spacer = null, windowEl = null;
        var destroyed = false, frameQueued = false;

        var list = VL.createList({
            count: 0,
            estimate: EST.estimator(turns, stateAt)
        });

        var raf = (typeof window !== 'undefined' &&
            typeof window.requestAnimationFrame === 'function')
            ? window.requestAnimationFrame.bind(window)
            : function (fn) { return setTimeout(fn, 16); };

        /**
         * Description: the open-panel state of one row. Returns a shared
         *   frozen-in-spirit default rather than allocating, because
         *   this is called once per row per estimate pass over up to
         *   30,805 rows.
         * Inputs: index (number). Output: object.
         */
        function stateAt(index) {
            return open[index] || null;
        }

        /**
         * Description: element with a class and optional text. Text
         *   nodes only, never innerHTML: transcript content is arbitrary
         *   text from the corpus and must never be parsed as markup.
         * Inputs: tag, cls, text. Output: Element.
         */
        function el(tag, cls, text) {
            var n = doc.createElement(tag);
            if (cls) n.setAttribute('class', cls);
            if (text !== null && text !== undefined) {
                n.appendChild(doc.createTextNode(String(text)));
            }
            return n;
        }

        /** Viewport height, with the detached case named. Output: number. */
        function viewportHeight() {
            var h = scroller && scroller.clientHeight;
            return Number.isFinite(h) && h > 0 ? h : FALLBACK_VIEWPORT_PX;
        }

        /** Scroll offset, 0 while detached. Output: number. */
        function scrollTop() {
            var t = scroller && scroller.scrollTop;
            return Number.isFinite(t) && t > 0 ? t : 0;
        }

        /**
         * Description: re-seed the geometry from the current turns and
         *   open state. Called whenever the row SET changes.
         * Inputs: none. Output: void.
         */
        function regroup() {
            list.setCount(turns.length, EST.estimator(turns, stateAt));
        }

        /**
         * Description: correct ONE row's height in the geometry after
         *   its open state changed, so the growth is accounted for in
         *   the same frame it appears. Without this the reconciliation
         *   would still converge, but only after the row had already
         *   shoved everything below it on screen.
         * Inputs: index (number). Output: void.
         */
        function reestimate(index) {
            if (!Number.isInteger(index)) return;
            list.measure(index, EST.estimateTurn(turns[index], stateAt(index)));
        }

        /**
         * Description: paint the render window. Rebuilt whole rather
         *   than diffed: at a dozen visible bubbles the DOM churn is
         *   cheaper than the bookkeeping a diff would need, and a
         *   recycled node cannot go stale if it never survives a frame.
         * Inputs: win (object) - a windowFor() result. Output: void.
         */
        function paint(win) {
            while (windowEl.firstChild) windowEl.removeChild(windowEl.firstChild);
            windowEl.style.transform = 'translateY(' + win.offsetTop + 'px)';
            spacer.style.height = win.totalHeight + 'px';
            for (var i = win.first; i <= win.last; i++) {
                var t = turns[i];
                if (!t) continue;
                var s = open[i] || {};
                windowEl.appendChild(TURN.renderTurn(doc, t, {
                    index: i,
                    infoOpen: !!s.infoOpen,
                    subOpen: !!s.subOpen,
                    progressExpanded: !!s.progressExpanded
                }));
            }
            // A CONVERSATION THAT JUST STOPS LOOKS FINISHED. Measured
            // live 2026-09-01: transcript 4 answers `has_more: true` at a
            // 400-turn page, so the reader would hit turn 400 of a much
            // longer session with nothing on screen saying so and would
            // reasonably conclude that was the end of it. The sentinel is
            // painted only when the window reaches the last row, so it
            // sits at the bottom of the content rather than floating.
            if (complete !== true && win.last >= turns.length - 1) {
                windowEl.appendChild(paintSentinel());
            }
        }

        /**
         * Description: the end-of-page marker. States which of the two
         *   non-complete cases this is, because "there is more" and "I
         *   was not told whether there is more" are different findings
         *   and only one of them justifies a pager.
         * Inputs: none. Output: Element.
         */
        function paintSentinel() {
            var box = el('div', ROOT_CLASS + '__sentinel', null);
            box.setAttribute('data-complete', String(complete));
            box.appendChild(el('p', ROOT_CLASS + '__sentinel-text',
                complete === false
                    ? 'THIS IS NOT THE END OF THE CONVERSATION. ' +
                      turns.length + ' turn(s) loaded so far; the server ' +
                      'says there are more.'
                    : 'WHETHER THERE IS MORE: NOT KNOWN. ' + turns.length +
                      ' turn(s) loaded. The server did not say whether this ' +
                      'is the end, so this view is not claiming it is.'));
            // NO BUTTON WITHOUT A HANDLER. A control that cannot do
            // anything is worse than the sentence alone, because it
            // offers a way forward that does not exist.
            if (!onLoadMore || canLoadMore() !== true) {
                if (complete === false) {
                    box.appendChild(el('p', ROOT_CLASS + '__sentinel-nopager',
                        'There is no way to ask for the rest from here: the ' +
                        'server reported more turns but handed back no ' +
                        'cursor. Open the raw view to read past this point.'));
                }
                return box;
            }
            var b = el('button', ROOT_CLASS + '__pager',
                loadingMore ? 'Loading...' : 'Load more turns');
            b.setAttribute('type', 'button');
            b.setAttribute('data-action', ACTION_LOAD_MORE);
            if (loadingMore) b.setAttribute('disabled', 'disabled');
            box.appendChild(b);
            return box;
        }

        /**
         * Description: pay the anti-jump debt for the window just
         *   painted and write the corrected total onto the spacer.
         *   Called on EVERY paint, including ones with no data change,
         *   because a bubble's height also moves with the viewport width.
         * Inputs: win (object). Output: number - the delta applied.
         */
        function reconcile(win) {
            var r = VL.reconcileMeasured(list, windowEl, win, scroller);
            if (r.applied > 0) spacer.style.height = r.totalHeight + 'px';
            return r.delta;
        }

        /**
         * Description: render the current state. Only a RENDERABLE token
         *   reaches the virtual list; `partial` renders its rows AND its
         *   banner, because a partial answer has real turns in it that
         *   the reader should see alongside what was not reached.
         * Inputs: none. Output: void.
         */
        function render() {
            if (destroyed || !root) return;
            renderChain();

            while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
            var renderable = window.ArchiveOutcome.isRenderable(token);
            statusEl.setAttribute('data-chat-state', token);
            root.setAttribute('data-chat-state', token);

            if (!renderable) {
                scroller.style.display = 'none';
                statusEl.appendChild(OV.renderReaderState(token, envelope, {
                    document: doc, rootClass: ROOT_CLASS,
                    deadlineMs: (window.ArchiveState &&
                        window.ArchiveState.DEADLINES_MS)
                        ? window.ArchiveState.DEADLINES_MS.transcript : undefined
                }));
                return;
            }

            if (token === 'partial') {
                statusEl.appendChild(OV.renderOutcomeBlock(envelope,
                    { document: doc }));
            }
            scroller.style.display = '';
            var win = list.windowFor(scrollTop(), viewportHeight());
            paint(win);
            reconcile(win);
        }

        /** Description: repaint the breadcrumb. Inputs: none. Output: void. */
        function renderChain() {
            if (!chainEl) return;
            while (chainEl.firstChild) chainEl.removeChild(chainEl.firstChild);
            chainEl.appendChild(STACK.renderChain(doc, stack));
        }

        /**
         * Description: queue one render on the next animation frame.
         *   Coalescing keeps a burst of clicks and scroll events from
         *   repainting once each.
         * Inputs: none. Output: void.
         */
        function schedule() {
            if (destroyed || frameQueued) return;
            frameQueued = true;
            raf(function () { frameQueued = false; render(); });
        }

        /**
         * Description: flip one boolean of one row's open state.
         * Inputs: index (number), key (string), on (boolean).
         * Output: void.
         */
        function setOpen(index, key, on) {
            var s = open[index] || (open[index] = {});
            s[key] = !!on;
            reestimate(index);
            schedule();
        }

        // The ONE delegated click listener lives in
        // archive-chat-clicks.js; it decides what a click MEANS and
        // hands every effect back through these callbacks, so it can
        // never grow its own copy of the open-panel state.
        var onClick = window.ArchiveChatClicks.create({
            stack: function () { return stack; },
            onChainUp: function (level, index) { onChainUp(level, index); },
            onOpenSubagent: function (spec) { onOpenSubagent(spec); },
            onLoadMore: onLoadMore,
            isLoadingMore: function () { return loadingMore; },
            setLoadingMore: function (v) { loadingMore = !!v; },
            openStateAt: stateAt,
            setOpen: setOpen,
            schedule: schedule
        });

        /**
         * Description: attach the view. Handles are copied out BEFORE
         *   the first render, because render() bails on a null root and
         *   would otherwise silently draw nothing - which looks exactly
         *   like a conversation with no turns.
         * Inputs: host (Element). Output: Element - the view root.
         */
        function mount(host) {
            root = el('section', ROOT_CLASS, null);
            root.setAttribute('data-view', 'chat');
            chainEl = el('div', ROOT_CLASS + '__chain', null);
            statusEl = el('div', ROOT_CLASS + '__status', null);
            scroller = el('div', ROOT_CLASS + '__scroller', null);
            scroller.setAttribute('tabindex', '0');
            spacer = el('div', ROOT_CLASS + '__spacer', null);
            windowEl = el('div', ROOT_CLASS + '__window', null);
            spacer.appendChild(windowEl);
            scroller.appendChild(spacer);
            root.appendChild(chainEl);
            root.appendChild(statusEl);
            root.appendChild(scroller);

            scroller.addEventListener('scroll', schedule);
            root.addEventListener('click', onClick);
            host.appendChild(root);
            render();
            return root;
        }

        return {
            mount: mount,
            /**
             * Description: install the turns to show. Resets scroll to
             *   the top: a new conversation is a new question, and
             *   keeping the old offset would land the reader in the
             *   middle of somebody else's transcript.
             * Inputs: rows (Array). Output: void.
             */
            setTurns: function (rows, isComplete) {
                turns = Array.isArray(rows) ? rows : [];
                open = {};
                complete = isComplete === true ? true
                    : (isComplete === false ? false : null);
                loadingMore = false;
                regroup();
                if (scroller) scroller.scrollTop = 0;
                schedule();
            },
            /**
             * Description: append a further page. The scroll position is
             *   NOT touched: appending rows below the viewport must not
             *   move what somebody is reading.
             * Inputs: rows (Array), isComplete (boolean|null).
             * Output: void.
             */
            appendTurns: function (rows, isComplete) {
                if (Array.isArray(rows) && rows.length) {
                    turns = turns.concat(rows);
                }
                complete = isComplete === true ? true
                    : (isComplete === false ? false : null);
                regroup();
                schedule();
            },
            /** Are all the turns loaded. true|false|null. Output: *. */
            complete: function () { return complete; },
            /**
             * Description: install the outcome token and its envelope.
             * Inputs: t (string), env (object|null). Output: void.
             */
            setToken: function (t, env) {
                token = t; envelope = env || null; schedule();
            },
            /** The drill chain. Output: object. */
            stack: function () { return stack; },
            /** Force a repaint now, outside the rAF loop. Output: void. */
            render: render,
            schedule: schedule,
            /** The geometry engine, for callers and tests. */
            list: list,
            /** The turns currently held. Output: Array. */
            turns: function () { return turns; },
            /** Current token. Output: string. */
            token: function () { return token; },
            /** The view root, null before mount(). Output: Element|null. */
            root: function () { return root; },
            /** The open-panel state, for tests. Output: object. */
            openState: function () { return open; },
            /** Is a further page in flight. Output: boolean. */
            isLoadingMore: function () { return loadingMore; },
            /** Detach and stop scheduling. Output: void. */
            destroy: function () {
                destroyed = true;
                if (scroller) scroller.removeEventListener('scroll', schedule);
                if (root) {
                    root.removeEventListener('click', onClick);
                    if (root.parentNode) root.parentNode.removeChild(root);
                }
                root = null;
            }
        };
    }

    window.ArchiveChatView = {
        create: create,
        ROOT_CLASS: ROOT_CLASS,
        ACTION_LOAD_MORE: ACTION_LOAD_MORE,
        FALLBACK_VIEWPORT_PX: FALLBACK_VIEWPORT_PX
    };
    console.log('[ArchiveChatView Module] Exported as window.ArchiveChatView');
})();
