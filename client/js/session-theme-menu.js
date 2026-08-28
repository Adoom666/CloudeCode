/**
 * Per-session theme picker.
 * ----------------------------------------------------------------------
 * The point of theming a tmux session INDIVIDUALLY is to tell your
 * sessions apart at a glance: the office box is green, the media box is
 * amber, and you know which one you are typing into before you read a
 * word. This module owns the picker; the control that opens it is the
 * SESSION EDITOR FAB (#sessionEditorBtn, session-editor-menu.js), which
 * is deliberately separate from the terminal tools FAB beside it -
 * configuring a session and copying its output are different jobs. The
 * default theme and the home theme stay where they are, in settings -
 * this picker only ever changes the session you are currently looking
 * at.
 *
 * PERSISTENCE. Nothing is stored client-side. Picking a theme calls
 * `Themes.applyGlobal()`, which - because app.js has put the registry in
 * session scope via `setActiveSession(session.tmux_session)` - PATCHes
 * `/api/v1/sessions/<tmux name>/theme` instead of writing localStorage.
 * The server persists it twice, both keyed by the TMUX SESSION NAME:
 * `<working_dir>/.cc.theme` and `pinned_themes.json`. That name is
 * stable across reloads, across reconnects and across browsers, and it
 * is not a pid and not a client-side id. On re-attach the server hands
 * back `pinned_theme` on the SessionInfo and app.js re-applies it.
 *
 * AUDIO MOVED OUT OF THIS MODULE. The background-music opt-in used to
 * live here, keyed per tmux session name. It is gone: audio is now a
 * single global on/off (client/js/globalAudioToggle.js) living in the
 * bottom bar on every screen that has one, not a per-session choice
 * buried in this picker's neighbourhood. See that file's doc comment for
 * the full reasoning, including why this is not the deleted app-sound
 * master switch reintroduced under a new name.
 */
(function () {
    'use strict';

    /** The open picker element, or null. */
    var pickerEl = null;

    /** Document-level dismiss handlers, bound only while open. */
    var onDocPointer = null;
    var onDocKey = null;

    /**
     * The tmux session name currently in scope, or null.
     *
     * @returns {string|null}
     */
    function activeSession() {
        if (!window.Themes || typeof window.Themes.getActiveSession !== 'function') {
            return null;
        }
        return window.Themes.getActiveSession();
    }

    /**
     * Close the picker if open.
     *
     * @returns {void}
     */
    function close() {
        if (onDocPointer) {
            document.removeEventListener('pointerdown', onDocPointer, true);
            onDocPointer = null;
        }
        if (onDocKey) {
            document.removeEventListener('keydown', onDocKey, true);
            onDocKey = null;
        }
        if (pickerEl) {
            pickerEl.remove();
            pickerEl = null;
        }
    }

    /**
     * Position the picker against its anchor. The placement rule (above
     * the anchor, right edges flush, clamped into the VISUAL viewport)
     * is shared with the session tools menu and lives in
     * client/js/anchor-popover.js - it used to be copied into every
     * anchored surface and the copies drifted.
     *
     * @param {HTMLElement} el - the picker.
     * @param {HTMLElement} anchor - the control it belongs to.
     * @returns {void}
     */
    function position(el, anchor) {
        if (!window.AnchorPopover) return;
        window.AnchorPopover.place(el, anchor);
    }

    /**
     * Open the theme picker anchored to a button.
     *
     * @param {HTMLElement} anchor - the theme button.
     * @returns {void}
     */
    function open(anchor) {
        close();
        if (!window.Themes || typeof window.Themes.listAll !== 'function') return;

        var session = activeSession();
        var themes = window.Themes.listAll();
        var activeId = (window.Themes.getActiveGlobal() || {}).id;

        pickerEl = document.createElement('div');
        pickerEl.className = 'cloude-session-theme';
        pickerEl.setAttribute('role', 'menu');
        pickerEl.setAttribute('aria-label', 'session theme');

        var head = document.createElement('div');
        head.className = 'cloude-session-theme__head';
        head.textContent = session
            ? 'theme for ' + session
            : 'theme';
        pickerEl.appendChild(head);

        var list = document.createElement('div');
        list.className = 'cloude-session-theme__list';
        themes.forEach(function (m) {
            var row = document.createElement('button');
            row.type = 'button';
            row.className = 'cloude-session-theme__item';
            row.setAttribute('role', 'menuitemradio');
            var selected = m.id === activeId;
            row.setAttribute('aria-checked', selected ? 'true' : 'false');
            if (selected) row.classList.add('is-active');
            row.textContent = (m.name || m.id).toLowerCase();
            row.addEventListener('click', function () {
                // applyGlobal PATCHes the server-side per-session pin
                // whenever a session is in scope; it only writes the
                // localStorage default when there is no session.
                window.Themes.applyGlobal(m.id);
                close();
            });
            list.appendChild(row);
        });
        pickerEl.appendChild(list);

        document.body.appendChild(pickerEl);
        position(pickerEl, anchor);

        // Deferred a tick so the tap that opened it does not close it.
        onDocPointer = function (e) {
            if (!pickerEl) return;
            if (pickerEl.contains(e.target)) return;
            if (anchor.contains(e.target)) return;
            close();
        };
        onDocKey = function (e) {
            if (e.key === 'Escape') close();
        };
        setTimeout(function () {
            document.addEventListener('pointerdown', onDocPointer, true);
            document.addEventListener('keydown', onDocKey, true);
        }, 0);
    }

    window.SessionThemeMenu = {
        open: open,
        close: close
    };
})();
