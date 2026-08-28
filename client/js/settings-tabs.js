/**
 * SettingsTabs - the tab strip for the settings modal
 * (client/js/settings-panel.js).
 *
 * ONE design rule, and it is the whole reason this is a separate module
 * rather than three lines inside the panel: every pane is rendered into
 * the DOM ONCE, at open, and switching tabs only toggles a class. Nothing
 * is re-rendered, nothing is destroyed. That is what makes half-typed
 * edits survive a tab switch - the inputs holding them are never removed
 * from the document, so the panel's existing "collect from the DOM at
 * save time" contract (settings-panel.js collectSectionPatch) keeps
 * working across every tab at once, unchanged, including panes the user
 * is not currently looking at.
 *
 * A re-render-on-switch implementation would be shorter and would silently
 * eat unsaved input, which is exactly the bug this avoids.
 *
 * Mobile: the strip scrolls horizontally (see .settings-tabs in
 * styles.css) instead of wrapping or shrinking text, because this app is
 * driven from a phone and a wrapped two-row tab strip eats the modal's
 * vertical budget.
 *
 * Loads BEFORE settings-panel.js (see client/index.html script order).
 */
(function () {
    'use strict';

    /**
     * Escape a string for safe interpolation into innerHTML-built markup.
     * Inputs: str (any) - value to escape; stringified first.
     * Output: string - HTML-escaped text.
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    /**
     * Build the markup for the tab strip plus every pane, all at once.
     * Inputs:
     *   tabs (Array<{id: string, label: string, bodyHtml: string}>) - in
     *     display order; the first is active on open.
     * Output: string - HTML: one `.settings-tabs` strip followed by one
     *   `.settings-tabpanel` per tab (all but the first carrying the
     *   `hidden` class).
     */
    function render(tabs) {
        var strip = tabs.map(function (tab, i) {
            var selected = i === 0;
            return (
                '<button type="button" role="tab" class="settings-tab' + (selected ? ' settings-tab--active' : '') + '"' +
                ' id="settings-tab-' + escapeHtml(tab.id) + '"' +
                ' data-settings-tab="' + escapeHtml(tab.id) + '"' +
                ' aria-selected="' + (selected ? 'true' : 'false') + '"' +
                ' aria-controls="settings-tabpanel-' + escapeHtml(tab.id) + '"' +
                ' tabindex="' + (selected ? '0' : '-1') + '">' +
                escapeHtml(tab.label) +
                '</button>'
            );
        }).join('');

        var panels = tabs.map(function (tab, i) {
            return (
                '<div role="tabpanel" class="settings-tabpanel' + (i === 0 ? '' : ' hidden') + '"' +
                ' id="settings-tabpanel-' + escapeHtml(tab.id) + '"' +
                ' data-settings-tabpanel="' + escapeHtml(tab.id) + '"' +
                ' aria-labelledby="settings-tab-' + escapeHtml(tab.id) + '">' +
                tab.bodyHtml +
                '</div>'
            );
        }).join('');

        return '<div class="settings-tabs" role="tablist" aria-label="settings sections">' + strip + '</div>' + panels;
    }

    /**
     * The strip's inline padding, in px. Kept in lock-step with the
     * `padding: 0 20px` on `.settings-tabs` in styles.css: `revealTab`
     * scrolls a tab clear of it, so a tab is never left tucked half
     * under the strip's own edge.
     */
    var STRIP_PAD_PX = 20;

    /**
     * Scroll the strip so one tab is fully inside the visible window.
     *
     * Five tabs did not fit a phone: measured at a 390px viewport the
     * strip was 355px wide against a 444px scroll width, and the fifth
     * tab ("general") started at x=354.3 - entirely off-screen. Arrowing
     * onto it used to move focus and the active state to a tab the user
     * could not see. This is the fix for that, and it also handles the
     * panel being opened straight onto a later tab.
     *
     * The strip is FOUR tabs now (the "agents" tab was removed once
     * wrappers covered every family). Re-measured at 390px after that
     * removal: scrollWidth 393 against clientWidth 355, so it STILL
     * scrolls, by 38px instead of 89px. "general" is reachable by a
     * shorter drag, not by no drag - do not read the shorter strip as a
     * reason this function is dead code.
     *
     * A no-op when the strip does not scroll.
     *
     * @param {Element} stripEl - the `.settings-tabs` scroll container.
     * @param {Element} btn - one `.settings-tab` inside it.
     * @returns {void}
     */
    function revealTab(stripEl, btn) {
        if (!stripEl || !btn) return;
        var left = btn.offsetLeft - STRIP_PAD_PX;
        var right = btn.offsetLeft + btn.offsetWidth + STRIP_PAD_PX;
        if (left < stripEl.scrollLeft) {
            stripEl.scrollLeft = left > 0 ? left : 0;
        } else if (right > stripEl.scrollLeft + stripEl.clientWidth) {
            stripEl.scrollLeft = right - stripEl.clientWidth;
        }
    }

    /**
     * Mark which edges of the strip have more tabs beyond them, so the
     * stylesheet can fade that edge.
     *
     * Without it the strip gave NO signal at all that it scrolled: the
     * fifth tab was simply absent from the screen and the last visible
     * one ended in clean whitespace, which reads as "that is all of
     * them". Three states, not two - a strip that fits gets no fade at
     * either edge rather than a permanent decorative one.
     *
     * @param {Element} stripEl - the `.settings-tabs` scroll container.
     * @returns {void}
     */
    function updateEdgeHints(stripEl) {
        if (!stripEl) return;
        var slack = stripEl.scrollWidth - stripEl.clientWidth;
        var scrolls = slack > 1;
        stripEl.classList.toggle(
            'settings-tabs--more-before', scrolls && stripEl.scrollLeft > 1
        );
        stripEl.classList.toggle(
            'settings-tabs--more-after', scrolls && stripEl.scrollLeft < slack - 1
        );
    }

    /**
     * Show one tab and hide the rest, without touching any pane's content.
     * Inputs:
     *   rootEl (Element) - element containing the strip and the panes.
     *   tabId (string) - the `data-settings-tab` value to activate.
     * Output: void.
     */
    function activate(rootEl, tabId) {
        var stripEl = rootEl.querySelector('.settings-tabs');
        rootEl.querySelectorAll('[data-settings-tab]').forEach(function (btn) {
            var isActive = btn.getAttribute('data-settings-tab') === tabId;
            btn.classList.toggle('settings-tab--active', isActive);
            btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
            btn.setAttribute('tabindex', isActive ? '0' : '-1');
            if (isActive) revealTab(stripEl, btn);
        });
        rootEl.querySelectorAll('[data-settings-tabpanel]').forEach(function (panel) {
            var isActive = panel.getAttribute('data-settings-tabpanel') === tabId;
            panel.classList.toggle('hidden', !isActive);
        });
        updateEdgeHints(stripEl);
    }

    /**
     * Wire click and arrow-key navigation for an already-rendered strip,
     * plus the scroll-edge hints.
     * Inputs: rootEl (Element) - element containing the strip and panes.
     * Output: void.
     */
    function wire(rootEl) {
        var buttons = Array.prototype.slice.call(rootEl.querySelectorAll('[data-settings-tab]'));
        var stripEl = rootEl.querySelector('.settings-tabs');
        buttons.forEach(function (btn, i) {
            btn.addEventListener('click', function () {
                activate(rootEl, btn.getAttribute('data-settings-tab'));
            });
            btn.addEventListener('keydown', function (e) {
                var delta = e.key === 'ArrowRight' ? 1 : (e.key === 'ArrowLeft' ? -1 : 0);
                if (!delta) return;
                e.preventDefault();
                var next = buttons[(i + delta + buttons.length) % buttons.length];
                activate(rootEl, next.getAttribute('data-settings-tab'));
                next.focus();
            });
        });
        if (stripEl) {
            stripEl.addEventListener('scroll', function () {
                updateEdgeHints(stripEl);
            }, { passive: true });
            // The strip's own width changes with the viewport (the modal
            // is a percentage width), so "does it scroll" is not a
            // one-time answer.
            window.addEventListener('resize', function () {
                updateEdgeHints(stripEl);
            });
            updateEdgeHints(stripEl);
        }
    }

    /**
     * Read the currently active tab id.
     * Inputs: rootEl (Element).
     * Output: string|null - active tab id, or null when none is rendered.
     */
    function activeTab(rootEl) {
        var btn = rootEl.querySelector('.settings-tab--active');
        return btn ? btn.getAttribute('data-settings-tab') : null;
    }

    window.SettingsTabs = {
        render: render,
        wire: wire,
        activate: activate,
        activeTab: activeTab,
    };
})();
