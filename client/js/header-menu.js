/**
 * Header overflow menu.
 *
 * AN OVERFLOW, NOT A RESPONSIVE FOLD ANY MORE. This used to move the
 * whole six-icon cluster into a dropdown below 768px and lay it back out
 * inline above it. It now holds TWO controls at EVERY width - logout and
 * settings - because those are the rarely-used ones, and a control that
 * is rare on a phone is rare on a desktop too. A layout
 * that changes what is reachable with the window width teaches two
 * different apps.
 *
 * WHAT IS DELIBERATELY *NOT* IN HERE:
 *   - `#configEditorBtn` (file editor). Used constantly, so it stays
 *     inline and one tap away at every width. This is the whole point of
 *     the split; do not "tidy" it into the menu.
 *   - `#statusText`. A live state light, not an action. Hiding it behind
 *     a tap would mean connection state is only visible to someone who
 *     went looking for it. It is no longer in the header row at all -
 *     it lives in the home screen's bottom bar or, on the terminal
 *     screen, on the FAB rail (see App._placeStatusLight). This module
 *     never sees it, and must not gain a rule that does.
 *   - `#session-sidebar-toggle`, top-LEFT and the one-handed reach
 *     target on a phone. This module never touches it.
 *   - Home and detach, which no longer exist as header buttons: clicking
 *     the title goes home, and detach moved to the session editor FAB.
 *
 * SCOPE BOUNDARY: this menu is APP-scoped and mounts on every screen
 * including the launchpad, where no session exists. Anything
 * session-scoped belongs to the session editor FAB, which hides itself
 * when nothing is attached.
 *
 * THERE IS NO AUDIO CONTROL HERE, AND ADDING ONE WOULD BE A REGRESSION.
 * An "app sound (all sessions)" toggle used to live in this menu. Being
 * app-scoped, persisted and defaulted OFF, it sat in front of the session
 * editor's "play music" row and silently vetoed it, which is why the app
 * produced no sound through five separate fixes. Audio is session-only
 * now, so the only music control is the session-scoped one in the FAB.
 *
 * WHY MOVE THE NODES INSTEAD OF RENDERING A SECOND COPY: every one of
 * these controls is addressed by id from elsewhere in the app
 * (`document.getElementById('settingsBtn')`, `.classList.remove('hidden')`
 * on screen change, click listeners wired once in app.js). A cloned
 * mirror menu would duplicate ids, strip listeners, and drift out of
 * sync with the show/hide gating the moment anything changed.
 * Re-parenting keeps one node per control, so every existing id lookup,
 * listener and `.hidden` toggle keeps working untouched.
 *
 * Depends on window.DismissGuard (client/js/dismiss-guard.js) for
 * outside-click dismissal - deliberately click-based, never focusout, so
 * the dropdown cannot reintroduce the "focus a child input and the menu
 * vanishes" bug that DismissGuard exists to kill.
 */

console.log('[HeaderMenu Module] Loading...');

/**
 * Ids of the controls the overflow owns, in their canonical order.
 *
 * `archiveBtn` USED TO BE FIRST HERE AND IS NOT ANY MORE. It was folded
 * in on the argument that the inline slots are for controls used
 * constantly and a message browser is not one of them. The owner asked
 * for the opposite and the reason is worth keeping: reaching the archive
 * cost a tap on an unlabelled kebab plus a read of a three-item menu,
 * and the launchpad's compensating entry point was a full-width row with
 * a title and a description sitting in the body, spending vertical space
 * on every visit to buy back a destination nobody could find. One 36px
 * icon beside the file-editor icon costs neither. See
 * HEADER_INLINE_CONTROL_IDS below.
 */
const HEADER_MENU_CONTROL_IDS = [
    'logoutBtn',
    'settingsBtn'
];

/**
 * Ids that must stay inline in the header at every width. Asserted by
 * tests: this is the "we keep editor very accessible" requirement, and
 * it is easier to defend as data than as a comment.
 *
 * `archiveBtn` sits beside `configEditorBtn` deliberately: they are the
 * app's two BROWSERS, one over your files and one over your transcripts,
 * and putting them next to each other is the whole reason the archive
 * icon reads as what it is without a label.
 *
 * IT IS STILL GATED. Being inline changes WHERE the control lives, not
 * WHETHER it exists: `_wireArchive()` below hides it at wire time and
 * reveals it only once `ArchiveEntry.ensure()` has MEASURED the server
 * as having the archive switched on. `disabled` and `unknown` both leave
 * it hidden. Moving a control out of a menu must not turn a measured
 * gate into an always-on door.
 *
 * A THIRD INLINE CONTROL IS A LAYOUT FACT, not just a list entry. The
 * home header centres its title against `--home-header-flank-w`, which
 * mirrors `.controls`' real width; styles.css now widens that token
 * under `:has(#archiveBtn:not([hidden]))` so the flank tracks whether
 * this button is actually showing. Adding a fourth means updating it
 * again.
 */
const HEADER_INLINE_CONTROL_IDS = ['archiveBtn', 'configEditorBtn'];

class HeaderMenu {
    constructor() {
        /** @type {Element|null} */ this.controls = null;
        /** @type {Element|null} */ this.toggle = null;
        /** @type {Element|null} */ this.panel = null;
        /** @type {boolean} */ this.isOpen = false;
        /** @type {boolean} */ this.isFolded = false;
    }

    /**
     * Description: build the toggle + panel and move the overflow
     *   controls into it. Idempotent - a second call is a no-op.
     *
     *   There is no MediaQueryList any more: the contents are the same at
     *   every width, so there is nothing to re-evaluate on resize or
     *   rotate and no second layout to drift out of sync with the first.
     * Inputs: none.
     * Output: void.
     */
    init() {
        if (this.panel) return;
        this.controls = document.querySelector('.header .controls');
        if (!this.controls) {
            console.warn('[HeaderMenu] no .header .controls, skipping');
            return;
        }

        this._buildChrome();
        this._wireEvents();
        this._wireArchive();
        this.applyLayout();
        console.log('[HeaderMenu] initialized');
    }

    /**
     * Description: create the dropdown trigger and its panel and append
     *   both to `.controls`. The trigger is a real <button> with
     *   aria-expanded/aria-controls, matching the accessibility pattern
     *   the other header controls already use.
     * Inputs: none.
     * Output: void.
     */
    _buildChrome() {
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.id = 'header-menu-toggle';
        toggle.className = 'btn-icon header-menu-toggle';
        toggle.setAttribute('aria-label', 'More actions');
        toggle.setAttribute('title', 'more');
        toggle.setAttribute('aria-haspopup', 'true');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-controls', 'header-menu-panel');
        // THE GLYPH LIVES IN client/js/kebab-icon.js NOW, because the
        // conversations sidebar draws the same mark on every row and the
        // owner asked for it "like in main sites top right" - the same
        // control, so one definition. The ink-weight reasoning that used
        // to sit here moved with it, including why the size is 20 and
        // why scripts/verify_login_chrome.py has a floor for it.
        // KebabIcon.svg(20) is byte-identical to the literal this line
        // replaced, asserted by tests/test_kebab_icon_shared.node.mjs.
        toggle.innerHTML = window.KebabIcon.svg(window.KebabIcon.DEFAULT_SIZE);

        const panel = document.createElement('div');
        panel.id = 'header-menu-panel';
        panel.className = 'header-menu-panel';
        panel.setAttribute('role', 'menu');
        panel.setAttribute('aria-label', 'More actions');
        panel.hidden = true;

        this.controls.appendChild(toggle);
        this.controls.appendChild(panel);
        this.toggle = toggle;
        this.panel = panel;
    }

    /**
     * Description: wire open/close - trigger click, Escape, outside click,
     *   and a click on any control inside the panel (which should run that
     *   control's own action AND collapse the menu behind it).
     * Inputs: none.
     * Output: void.
     */
    _wireEvents() {
        this.toggle.addEventListener('click', (e) => {
            e.stopPropagation();
            this.isOpen ? this.close() : this.open();
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this.isOpen) {
                this.close();
                this.toggle.focus();
            }
        });

        window.DismissGuard.onOutsideDismiss(this.panel, () => this.close(), {
            trigger: this.toggle,
            isOpen: () => this.isOpen
        });

        // A tap on a folded control fires that control's own handler
        // first (it is the same node, listeners intact); we only collapse
        // the menu behind it. Guarded so a tap on the panel's own padding
        // does not close it out from under a fat finger.
        this.panel.addEventListener('click', (e) => {
            if (e.target === this.panel) return;
            this.close();
        });
    }

    /**
     * Description: wire the archive destination.
     *
     *   Wired HERE rather than in app.js's setupEventListeners, and never
     *   as an inline onclick: src/main.py stamps `script-src 'self'` on
     *   every response, so an inline handler is silently refused - the
     *   element stays present, sized and clickable while doing nothing,
     *   which no DOM test can see (this is exactly how #logoutBtn was
     *   dead from the initial commit; see its comment in index.html).
     *
     *   It calls window.ArchiveEntry, the ONE navigation into the
     *   archive, which the launchpad row also calls. Two copies of a
     *   navigation is two copies that can drift.
     *
     *   Idempotent via a data attribute rather than a member flag,
     *   because init() is idempotent by way of `if (this.panel) return`
     *   and a second HeaderMenu instance would not see the flag.
     *
     *   The attribute is read and written with get/setAttribute rather
     *   than through `dataset`, which the node test harness
     *   (tests/mini-dom.mjs) does not implement - relying on it threw
     *   `Cannot read properties of undefined` and took 17 unrelated
     *   assertions down with it. get/setAttribute is the API every DOM
     *   in this project actually has.
     * Inputs: none.
     * Output: void.
     */
    _wireArchive() {
        const btn = document.getElementById('archiveBtn');
        if (!btn || btn.getAttribute('data-archive-wired') === '1') return;
        btn.setAttribute('data-archive-wired', '1');
        // HIDDEN UNTIL MEASURED. The message archive is off by default
        // (src/core/message_archive_flag.py), and with it off there is no
        // archive screen to reach - the server redirects /archive to the
        // launchpad and every /api/v1/archive/* route 404s. So the
        // control is hidden here, at wire time, and revealed only once
        // ArchiveEntry.ensure() has measured the server as ENABLED.
        //
        // It is hidden from JS rather than by a class in index.html for
        // two reasons: index.html is not this feature's to edit, and a
        // control that is present-but-hidden in the markup is invisible
        // to the DOM-presence assertions that already guard this button.
        // Hiding it here keeps the hide and the reveal in one place.
        //
        // 'unknown' leaves it hidden. A failed probe is not permission.
        btn.style.display = 'none';
        btn.hidden = true;
        if (window.ArchiveEntry &&
            typeof window.ArchiveEntry.ensure === 'function') {
            window.ArchiveEntry.ensure().then((state) => {
                if (state !== window.ArchiveEntry.STATE_ENABLED) return;
                btn.style.display = '';
                btn.hidden = false;
            });
        }
        btn.addEventListener('click', () => {
            if (window.ArchiveEntry) window.ArchiveEntry.open();
            else console.warn('[HeaderMenu] ArchiveEntry is not loaded');
        });
    }

    /**
     * Description: move the overflow controls into the panel. Kept as a
     *   named method (rather than inlined into init) because the tests
     *   and the unfold path below both address it, and because a future
     *   width-dependent rule would land here rather than in init.
     *   Idempotent.
     * Inputs: none.
     * Output: void.
     */
    applyLayout() {
        if (this.isFolded) return;
        this._fold();
    }

    /**
     * Description: move every foldable control into the dropdown panel,
     *   in canonical order, and reveal the trigger.
     * Inputs: none.
     * Output: void.
     */
    _fold() {
        for (const id of HEADER_MENU_CONTROL_IDS) {
            const el = document.getElementById(id);
            if (el) this.panel.appendChild(el);
        }
        this.controls.classList.add('controls--folded');
        this.isFolded = true;
    }

    /**
     * Description: move every foldable control back inline, restoring the
     *   canonical order, and close + hide the dropdown. Controls are
     *   re-inserted BEFORE the trigger so the trigger and panel stay last
     *   in the row.
     * Inputs: none.
     * Output: void.
     */
    _unfold() {
        this.close();
        for (const id of HEADER_MENU_CONTROL_IDS) {
            const el = document.getElementById(id);
            if (el) this.controls.insertBefore(el, this.toggle);
        }
        this.controls.classList.remove('controls--folded');
        this.isFolded = false;
    }

    /** Open the dropdown (idempotent). */
    open() {
        if (!this.panel) return;
        this.panel.hidden = false;
        this.panel.classList.add('header-menu-panel--open');
        this.toggle.setAttribute('aria-expanded', 'true');
        this.isOpen = true;
    }

    /** Close the dropdown (idempotent). */
    close() {
        if (!this.panel) return;
        this.panel.classList.remove('header-menu-panel--open');
        this.panel.hidden = true;
        this.toggle.setAttribute('aria-expanded', 'false');
        this.isOpen = false;
    }
}

window.HeaderMenu = new HeaderMenu();
window.HeaderMenuConstants = {
    CONTROL_IDS: HEADER_MENU_CONTROL_IDS,
    INLINE_CONTROL_IDS: HEADER_INLINE_CONTROL_IDS
};

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.HeaderMenu.init());
} else {
    window.HeaderMenu.init();
}

console.log('[HeaderMenu Module] Exported as window.HeaderMenu');
