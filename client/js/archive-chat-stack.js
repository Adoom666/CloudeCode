/**
 * THE DRILL CHAIN: which conversation you are in, and how you get back.
 *
 * "then you can drill down into and view the subagent the same way." The
 * word doing the work there is SAME. A subagent is not a different kind
 * of thing with a different viewer; it is a transcript, rendered by the
 * same chat view, which means it can itself contain subagents, which
 * means the nesting has no fixed depth. The owner said he will nest. So
 * the way back cannot be a single "back" button holding one previous
 * transcript id - it has to be a stack, and every level of it has to be
 * individually reachable, or somebody four levels down has to click back
 * four times to reach the top.
 *
 * WHY THIS IS SEPARATE FROM THE VIEW. The view is a rAF loop over a
 * virtual list; this is a list of small objects with push and truncate.
 * Mixing them would mean the chain could only be tested by standing up a
 * scroller. Pure: no DOM in the state half, and the render half takes a
 * document as an argument.
 *
 * A LEVEL WHOSE LABEL IS NOT KNOWN SAYS SO. A breadcrumb that invents
 * "Subagent" for a row whose name the server never sent is a fact
 * manufactured by the navigation, and it is exactly the kind of
 * plausible filler that stops people asking why the name is missing.
 *
 * THE ROOT LEVEL IS ALWAYS PRESENT and can never be popped. Truncating
 * the stack to zero would leave the view holding no transcript with no
 * way to say which one it lost.
 *
 * Exports window.ArchiveChatStack.
 */

console.log('[ArchiveChatStack Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-chain';

    /** @type {string} data-action that jumps to one level of the chain. */
    var ACTION_UP = 'chain-up';

    /** @type {string} */
    var NOT_KNOWN = 'NOT KNOWN';

    /**
     * Description: element with a class and optional text.
     * Inputs: doc, tag, cls, text. Output: Element.
     */
    function el(doc, tag, cls, text) {
        var n = doc.createElement(tag);
        if (cls) n.setAttribute('class', cls);
        if (text !== null && text !== undefined) n.textContent = String(text);
        return n;
    }

    /**
     * Description: build a drill chain. Empty until `reset()` names a
     *   root, because a chain with no root is not a shallow chain, it is
     *   an unasked question.
     * Inputs: none.
     * Output: object - {reset, push, truncateTo, pop, levels, depth,
     *   current, isRoot}.
     * Example:
     *   var st = ArchiveChatStack.create();
     *   st.reset({transcriptId: 4, label: 'main'});
     *   st.push({transcriptId: 91, label: 'Explore', agentId: 'a1'});
     *   st.depth();  // -> 2
     */
    function create() {
        /** @type {Array<{transcriptId: *, label: *, agentId: *, ordinal: *}>} */
        var levels = [];

        /**
         * Description: normalise one level, keeping `undefined` and
         *   `null` distinct from a string, so an unnamed level renders
         *   as unknown rather than as the string "undefined".
         * Inputs: spec (object|null). Output: object.
         */
        function normalise(spec) {
            var s = spec || {};
            return {
                transcriptId: (s.transcriptId === undefined) ? null : s.transcriptId,
                label: (typeof s.label === 'string' && s.label !== '')
                    ? s.label : null,
                agentId: (s.agentId === undefined) ? null : s.agentId,
                ordinal: Number.isInteger(s.ordinal) ? s.ordinal : null
            };
        }

        return {
            /**
             * Description: start a new chain at one transcript, discarding
             *   any previous chain. Opening a transcript from the list is
             *   a new question, not a step deeper into the old one.
             * Inputs: spec (object) - {transcriptId, label}.
             * Output: object - the new root level.
             */
            reset: function (spec) {
                levels = [normalise(spec)];
                return levels[0];
            },

            /**
             * Description: descend into a subagent. A push onto an EMPTY
             *   chain is refused rather than silently becoming a reset:
             *   it would mean the view drilled into a subagent without
             *   ever having opened a parent, which is a bug worth seeing.
             * Inputs: spec (object) - {transcriptId, label, agentId, ordinal}.
             * Output: object|null - the level pushed, or null if refused.
             */
            push: function (spec) {
                if (levels.length === 0) return null;
                var lvl = normalise(spec);
                levels.push(lvl);
                return lvl;
            },

            /**
             * Description: go back UP to a level by its index, dropping
             *   everything below it. This is what makes a four-deep chain
             *   one click from the top instead of four.
             * Inputs: index (number) - 0 is the root.
             * Output: object|null - the level now current, or null when
             *   the index names no level (out of range is a no-op, not a
             *   truncation to nothing).
             */
            truncateTo: function (index) {
                if (!Number.isInteger(index)) return null;
                if (index < 0 || index >= levels.length) return null;
                levels = levels.slice(0, index + 1);
                return levels[levels.length - 1];
            },

            /**
             * Description: go up exactly one level. A no-op at the root.
             * Inputs: none.
             * Output: object|null - the level now current, or null when
             *   already at the root.
             */
            pop: function () {
                if (levels.length <= 1) return null;
                levels.pop();
                return levels[levels.length - 1];
            },

            /** A copy of the chain, root first. Output: Array. */
            levels: function () { return levels.slice(); },
            /** How deep the chain is. Output: number. */
            depth: function () { return levels.length; },
            /** The level being viewed. Output: object|null. */
            current: function () {
                return levels.length ? levels[levels.length - 1] : null;
            },
            /** Is the view at the top of the chain. Output: boolean. */
            isRoot: function () { return levels.length <= 1; }
        };
    }

    /**
     * Description: render the chain as a breadcrumb of buttons. Every
     *   level except the last is a control that goes there; the last is
     *   marked current and is not a control, because a button that does
     *   nothing is worse than a label.
     *
     *   A ONE-LEVEL CHAIN STILL RENDERS. It is how the reader knows they
     *   are at the top rather than lost, and it is where the chain
     *   appears from as soon as they drill.
     * Inputs: doc (Document) - REQUIRED.
     *         stack (object) - a create() result.
     * Output: Element - a <nav data-depth=...>.
     * Example: renderChain(doc, stack)
     */
    function renderChain(doc, stack) {
        if (!doc) throw new Error('ArchiveChatStack.renderChain needs a document');
        var levels = stack ? stack.levels() : [];
        var nav = el(doc, 'nav', ROOT_CLASS, null);
        nav.setAttribute('aria-label', 'Subagent drill chain');
        nav.setAttribute('data-depth', String(levels.length));

        if (levels.length === 0) {
            nav.appendChild(el(doc, 'p', ROOT_CLASS + '__none',
                'No conversation open.'));
            return nav;
        }

        for (var i = 0; i < levels.length; i++) {
            var lvl = levels[i];
            var last = (i === levels.length - 1);
            var text = (lvl.ordinal !== null ? lvl.ordinal + '. ' : '') +
                (lvl.label !== null ? lvl.label : 'name ' + NOT_KNOWN) +
                (lvl.transcriptId !== null ? ' (t' + lvl.transcriptId + ')' : '');

            if (last) {
                var here = el(doc, 'span', ROOT_CLASS + '__here', text);
                here.setAttribute('aria-current', 'true');
                here.setAttribute('data-level', String(i));
                nav.appendChild(here);
                continue;
            }
            var b = el(doc, 'button', ROOT_CLASS + '__up', text);
            b.setAttribute('type', 'button');
            b.setAttribute('data-action', ACTION_UP);
            b.setAttribute('data-level', String(i));
            b.setAttribute('aria-label', 'Back up to ' + text);
            nav.appendChild(b);
            nav.appendChild(el(doc, 'span', ROOT_CLASS + '__sep', '>'));
        }
        return nav;
    }

    window.ArchiveChatStack = {
        create: create,
        renderChain: renderChain,
        ROOT_CLASS: ROOT_CLASS,
        ACTION_UP: ACTION_UP
    };
    console.log('[ArchiveChatStack Module] Exported as window.ArchiveChatStack');
})();
