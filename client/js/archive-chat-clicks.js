/**
 * THE ONE DELEGATED CLICK LISTENER OF THE CONVERSATION VIEW.
 *
 * WHY IT IS DELEGATED AND NOT PER-ROW. Bubbles are recycled on every
 * paint - the render window is rebuilt whole - so a listener bound to a
 * bubble would leak when that bubble is discarded and would go stale
 * when the same DOM node is reused for a different turn. One listener on
 * the view root, resolving its target out of the DOM, has neither
 * problem and costs one node walk per click.
 *
 * WHY IT IS ITS OWN FILE. archive-chat-view.js crossed this repo's
 * 500-line cap when the paging sentinel landed, and this is the seam the
 * raw reader already uses: archive-reader-dom.js owns that reader's one
 * delegated listener for exactly the same reason. Splitting here keeps
 * the view file to the rAF loop and the geometry, and puts every
 * "what does this click mean" decision in one readable place.
 *
 * A CLICK WHOSE ROW CANNOT BE RESOLVED IS A THIRD OUTCOME, and it is
 * REPORTED rather than guessed. An index parsed out of a missing
 * attribute is NaN, and toggling a NaN-indexed row would open somebody
 * else's panel - which looks exactly like the feature working, and is
 * therefore the worst available failure. Every such path logs what it
 * saw and changes nothing.
 *
 * IT MUTATES NOTHING DIRECTLY. Every effect goes back through a callback
 * the view supplied, so this file cannot develop its own opinion about
 * the open-panel state or the drill chain.
 *
 * Exports window.ArchiveChatClicks.
 */

console.log('[ArchiveChatClicks Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: the subagent's display name off its RENDERED row, so
     *   the breadcrumb names the level the reader actually clicked
     *   rather than inventing one. A row the server never named yields
     *   null, which the breadcrumb renders as NOT KNOWN.
     * Inputs: btn (Element), nameClass (string) - the class the name
     *   span carries.
     * Output: string|null.
     */
    function nameOf(btn, nameClass) {
        var n = btn.querySelector('.' + nameClass);
        var t = n && n.textContent;
        return (typeof t === 'string' && t !== '') ? t : null;
    }

    /**
     * Description: build the click handler for one chat view.
     * Inputs: ctx (object) -
     *   stack() - the drill chain;
     *   onChainUp(level, index) - a breadcrumb level was chosen;
     *   onOpenSubagent(spec) - a subagent row was activated;
     *   onLoadMore() - the pager was pressed; may return a Promise;
     *   isLoadingMore() / setLoadingMore(bool) - the pager's in-flight
     *     guard, held by the view so the paint can disable the button;
     *   openStateAt(index) - that row's open panels;
     *   setOpen(index, key, on) - flip one;
     *   schedule() - queue a repaint.
     * Output: function(ev) - the listener.
     * Example:
     *   root.addEventListener('click', ArchiveChatClicks.create(ctx));
     */
    function create(ctx) {
        var SUB = window.ArchiveChatSubagents;
        var TURN = window.ArchiveChatTurn;
        var STACK = window.ArchiveChatStack;
        var VIEW = window.ArchiveChatView;

        return function onClick(ev) {
            var target = ev && ev.target;
            if (!target || typeof target.closest !== 'function') return;
            var hit = target.closest('[data-action]');
            if (!hit) return;
            var action = hit.getAttribute('data-action');

            if (action === STACK.ACTION_UP) {
                var lvlRaw = hit.getAttribute('data-level');
                var lvl = parseInt(lvlRaw, 10);
                if (!Number.isInteger(lvl)) {
                    console.error('[ArchiveChatClicks] chain-up with no usable ' +
                        'data-level (saw: ' + String(lvlRaw) + '). Nothing moved.');
                    return;
                }
                ctx.onChainUp(ctx.stack().levels()[lvl] || null, lvl);
                return;
            }

            if (action === (VIEW && VIEW.ACTION_LOAD_MORE)) {
                // ONE PAGE IN FLIGHT AT A TIME. Two overlapping appends
                // would interleave turns from two responses into one
                // list, in an order nobody chose.
                if (ctx.isLoadingMore() || typeof ctx.onLoadMore !== 'function') return;
                ctx.setLoadingMore(true);
                ctx.schedule();
                var done = function () { ctx.setLoadingMore(false); ctx.schedule(); };
                var p = ctx.onLoadMore();
                if (p && typeof p.then === 'function') p.then(done, done);
                else done();
                return;
            }

            if (action === SUB.ACTION_OPEN) {
                // A row the server could not link carries
                // data-openable="false" and is disabled. Checking the
                // attribute rather than relying on `disabled` means a
                // click that reaches here anyway - through a synthetic
                // event, or a browser that dispatches on a disabled
                // ancestor - still cannot drill into a transcript that
                // was never identified.
                if (hit.getAttribute('data-openable') !== 'true') return;
                var ordEl = hit.querySelector('[data-ordinal]');
                var ordRaw = ordEl ? ordEl.getAttribute('data-ordinal') : null;
                ctx.onOpenSubagent({
                    transcriptId: hit.getAttribute('data-transcript-id'),
                    agentId: hit.getAttribute('data-agent-id'),
                    label: nameOf(hit, SUB.ROOT_CLASS + '__name'),
                    ordinal: /^[0-9]+$/.test(String(ordRaw))
                        ? parseInt(ordRaw, 10) : null
                });
                return;
            }

            var row = hit.closest('[data-index]');
            var raw = row ? row.getAttribute('data-index') : null;
            var idx = parseInt(raw, 10);
            if (!Number.isInteger(idx)) {
                console.error('[ArchiveChatClicks] ' + action + ' clicked but ' +
                    'the owning turn carries no usable data-index (saw: ' +
                    String(raw) + '). Nothing toggled.');
                return;
            }
            var s = ctx.openStateAt(idx) || {};
            if (action === TURN.ACTION_INFO) {
                ctx.setOpen(idx, 'infoOpen', !s.infoOpen); return;
            }
            if (action === TURN.ACTION_SUBAGENTS) {
                ctx.setOpen(idx, 'subOpen', !s.subOpen); return;
            }
            if (action === TURN.ACTION_EXPAND_PROGRESS) {
                ctx.setOpen(idx, 'progressExpanded', true); return;
            }
            if (action === TURN.ACTION_COLLAPSE_PROGRESS) {
                ctx.setOpen(idx, 'progressExpanded', false);
            }
        };
    }

    window.ArchiveChatClicks = { create: create, nameOf: nameOf };
    console.log('[ArchiveChatClicks Module] Exported as window.ArchiveChatClicks');
})();
