/**
 * SettingsPanel - gear-icon modal folding in the theme chooser plus the
 * agent/notifications/server config sections.
 *
 * TABBED (feat/settings-tabs-and-commands). FOUR tabs, declared in TABS
 * below: wrappers (every family's launch wrappers, each with its legacy
 * static command folded in as an editable advanced row), terminal (the
 * runnable command list), notifications, and general (appearance, the
 * global music volume, and the read-only server block). The fifth,
 * "agents", was removed once wrappers covered every family: it
 * administered the same four `<family>_command` keys from a second
 * screen. Every pane is built ONCE at open and
 * switching only shows/hides, so a
 * half-typed edit survives moving between tabs and Save still collects
 * from every tab at once. Section MARKUP lives in settings-sections.js
 * and the tab strip in settings-tabs.js; this file owns the lifecycle,
 * the schema, and the batched Save.
 *
 * Data-driven by design (the user's "so we can extrapolate" requirement):
 * SECTIONS below is the single place a future setting gets added - a new
 * field object in an existing section's `fields` array, or a whole new
 * section object, is all that's needed for it to render, collect, and
 * PATCH correctly. No per-field wiring lives outside `renderField()` /
 * `collectSectionPatch()`.
 *
 * Modal conventions follow `AppController.showConfirmModal()` (app.js):
 * a `.modal-overlay` appended to `document.body` on open and removed on
 * close (never left hidden in the DOM), Escape / backdrop-click / close
 * button all dismiss, focus is moved into the panel on open and the
 * triggering button is refocused on close.
 *
 * The appearance section does not appear in SECTIONS - it is special-
 * cased in `renderAppearanceSection()` because it delegates entirely to
 * `window.ThemeSelector.mount()` / `window.Themes`, which already own
 * persistence (localStorage global default, or a server-side PATCH for
 * an active session's pinned/project theme - see registry.js). Appearance
 * changes apply immediately through that existing pipeline and are never
 * part of this panel's own Save/PATCH flow.
 */
(function () {
    'use strict';

    // ---- schema -------------------------------------------------------
    //
    // type: 'text' | 'secret' | 'checkbox'.
    // group: which top-level PATCH /config/settings key this field lives
    //   under ('agents' | 'notifications') - must match the server's
    //   ConfigSettingsUpdateRequest sub-model field names exactly.
    // secret fields never receive their real value from the server (see
    // Settings._mask_secret / get_settings_summary on the Python side) -
    // the input starts empty with a placeholder describing current state,
    // and an empty submit means "leave unchanged" (see collectSectionPatch).

    // NO AGENT FIELDS HERE, and none of the four ``<family>_command``
    // keys is collectable by this panel at all. The "agents" tab held
    // three editable command fields (codex/hermes/openclaw) while
    // ``claude_command`` sat on the wrappers tab as a disabled row - the
    // two halves of a split that feat/universal-wrappers had already
    // unified underneath, since wrappers now cover every family.
    //
    // All four keys moved INTO the wrappers screen, as the editable body
    // of each family's collapsed "advanced: legacy <family> command" row
    // (agent-wrappers-view.renderLegacyCommand). Nothing was deleted:
    // the config keys are untouched, still the per-family fallback, and
    // still editable. What went away is the second place to look.
    //
    // They write immediately through their own button rather than through
    // this panel's batched Save, which is why keeping them out of any
    // SECTIONS entry still guarantees collectSectionPatch can never
    // include them in a PATCH built here.

    var NOTIFICATION_FIELDS = [
        { key: 'enabled', type: 'checkbox', label: 'notifications enabled' },
        { key: 'ntfy_base_url', type: 'text', label: 'ntfy server url', placeholder: 'https://ntfy.sh' },
        { key: 'ntfy_topic', type: 'secret', label: 'ntfy topic' },
        { key: 'slack_webhook_url', type: 'secret', label: 'slack webhook url' },
        { key: 'pushover_token', type: 'secret', label: 'pushover token' },
        { key: 'pushover_user_key', type: 'secret', label: 'pushover user key' },
    ];

    // Sections rendered by the generic field-driven path. 'appearance' and
    // 'server' are rendered by their own functions (bespoke behavior: the
    // theme picker delegates to ThemeSelector, the server section is
    // read-only with an inline safety warning) and are stitched into the
    // same panel below.
    var SECTIONS = [
        {
            id: 'notifications', title: 'notifications', group: 'notifications',
            restartRequired: true,
            description: 'push notification channels: ntfy, slack, pushover.',
            fields: NOTIFICATION_FIELDS,
        },
    ];

    // Tab layout. `id` is the tab's DOM key, `label` its visible name,
    // `sectionIds` the SECTIONS entries whose generic markup belongs in
    // it, and `slots` the ids of bespoke mount points appended after them
    // (wrappers, terminal commands, theme picker, server info).
    //
    // Grouping follows what the code actually is, not a guess: how claude
    // launches (wrappers + the legacy command they supersede), the other
    // agent CLIs, the new terminal command list, notifications, and
    // general (appearance, the global music volume, and the read-only
    // server block). Every pane is rendered once at open and only
    // shown/hidden afterwards, so unsaved
    // edits survive tab switches - see settings-tabs.js.
    var TABS = [
        // feat/universal-wrappers - one screen for every family's
        // wrappers. Each family's legacy static command renders INSIDE its
        // own group (agent-wrappers-view.renderLegacyCommand), so there is
        // no separate claude-only legacy slot any more.
        // 'agents' was the fifth tab and is GONE. It held the other agent
        // CLIs' static commands, which are the per-family FALLBACK the
        // wrappers screen already documents inside each family's group;
        // administering the same value from two screens meant two places
        // to look and one of them called the value inert. The keys and
        // their fallback behaviour are unchanged - see the note above
        // NOTIFICATION_FIELDS. Four tabs now; measured at 390px the strip
        // is 393px against a 355px window, so it still scrolls, by 38px
        // instead of 89px. Shorter drag, not no drag.
        // feat/settings-gui - the four global settings. FIRST because
        // it is the tab that answers "what will my next terminal be",
        // which is the question a settings screen is opened to answer.
        { id: 'workspace', label: 'workspace', sectionIds: [], slots: ['workspace'] },
        { id: 'wrappers', label: 'wrappers', sectionIds: [], slots: ['wrappers'] },
        { id: 'terminal', label: 'terminal', sectionIds: [], slots: ['terminal-commands'] },
        { id: 'notifications', label: 'notifications', sectionIds: ['notifications'], slots: [] },
        // 'audio' is the global music volume (settings-audio.js). It sits
        // in general next to appearance because it is an app-wide output
        // preference attached to themes, not to any one agent, terminal
        // or notification channel. Like the theme picker it applies
        // immediately and never joins the batched Save.
        { id: 'general', label: 'general', sectionIds: [], slots: ['appearance', 'audio', 'server'] },
    ];

    // Module state - the last GET /config/settings payload, so re-renders
    // (e.g. after a save) don't need a network round trip for fields the
    // user didn't just change.
    var lastSummary = null;
    var overlayEl = null;
    var triggerEl = null;

    // Section markup lives in settings-sections.js (pure render functions,
    // split out to keep this file inside the 500-line budget).
    var Sections = window.SettingsSections;
    var escapeHtml = Sections.escapeHtml;

    /**
     * Build the HTML for one tab's pane: its generic SECTIONS entries
     * followed by its bespoke slots.
     * Inputs: tab (object) - one TABS entry.
     * Output: string.
     */
    function renderTabBody(tab) {
        var parts = tab.sectionIds.map(function (sectionId) {
            var section = SECTIONS.filter(function (s) { return s.id === sectionId; })[0];
            return section ? Sections.renderGenericSection(section, lastSummary) : '';
        });
        tab.slots.forEach(function (slot) {
            if (slot === 'appearance') {
                parts.push(Sections.renderAppearanceSection());
            } else if (slot === 'audio') {
                parts.push(window.SettingsAudio ? window.SettingsAudio.render() : '');
            } else if (slot === 'workspace') {
                parts.push(window.SettingsWorkspace ? window.SettingsWorkspace.render(lastSummary) : '');
            } else if (slot === 'server') {
                parts.push(Sections.renderServerSection(lastSummary));
            } else {
                // Bespoke panels own their own DOM and mount into an empty
                // slot after insertion (see mountSlots): wrappers,
                // terminal commands.
                parts.push('<div id="settings-' + slot + '-slot"></div>');
            }
        });
        return parts.join('');
    }

    /**
     * Build the full panel body: the tab strip plus every pane, all
     * rendered at once so switching tabs never destroys unsaved input.
     * Output: string.
     */
    function renderBody() {
        return window.SettingsTabs.render(TABS.map(function (tab) {
            return { id: tab.id, label: tab.label, bodyHtml: renderTabBody(tab) };
        }));
    }

    /**
     * Read every field's current input value out of the DOM and build
     * the PATCH payload for one section's group, including ONLY fields
     * the user actually touched:
     *   - text fields: included if the value differs from the value
     *     `renderField` seeded it with (so an untouched field, even one
     *     the user clicked into and clicked back out of unchanged, isn't
     *     sent - matches the "omit = leave unchanged" server contract).
     *   - secret fields: included ONLY if non-empty (an empty secret
     *     input always means "didn't type a new one", never "clear it" -
     *     clearing a channel is done by unmasking to "not set" server-
     *     side, which this UI doesn't expose as a distinct action to
     *     avoid an accidental blank submit wiping a working webhook).
     *   - checkboxes: included if changed from the seeded checked state.
     * Inputs: section (object) - one SECTIONS entry.
     * Output: object - patch fragment for `section.group`, possibly {}.
     */
    function collectSectionPatch(section) {
        var groupData = (lastSummary && lastSummary[section.group]) || {};
        var patch = {};
        section.fields.forEach(function (field) {
            var el = overlayEl.querySelector('[data-settings-key="' + field.key + '"]');
            if (!el) return;

            if (field.type === 'checkbox') {
                var seededChecked = !!groupData[field.key];
                if (el.checked !== seededChecked) patch[field.key] = el.checked;
                return;
            }

            if (field.type === 'secret') {
                var typed = el.value;
                if (typed !== '') patch[field.key] = typed;
                return;
            }

            // text
            var seededVal = groupData[field.key] == null ? '' : String(groupData[field.key]);
            if (el.value !== seededVal) patch[field.key] = el.value;
        });
        return patch;
    }

    /**
     * Gather the full PATCH body across every generic section that has
     * at least one changed field.
     * Output: object - `{agents?: {...}, notifications?: {...}}`, only
     *   including a top-level key when its section has changes.
     */
    function collectPatch() {
        var body = {};
        SECTIONS.forEach(function (section) {
            var fragment = collectSectionPatch(section);
            if (Object.keys(fragment).length > 0) {
                body[section.group] = fragment;
            }
        });
        // feat/settings-gui - the workspace tab owns its own collection
        // because its environment list has a variable number of rows,
        // which the field-driven path above cannot describe. It still
        // joins THIS panel's single batched Save rather than writing on
        // its own, so one Save means one write and one repaint.
        if (window.SettingsWorkspace && overlayEl) {
            var wsFragments = window.SettingsWorkspace.collect(
                overlayEl.querySelector('#settings-panel-body'), lastSummary
            );
            Object.keys(wsFragments).forEach(function (key) {
                body[key] = wsFragments[key];
            });
        }
        return body;
    }

    /**
     * Persist any changed fields via PATCH /config/settings, then
     * re-render the panel body from the fresh server response so secret
     * "configured" badges and the claude effective-command preview
     * reflect what was just saved.
     * Inputs: none (reads the panel's own DOM).
     * Output: Promise<void>.
     */
    async function save() {
        var patch = collectPatch();
        if (Object.keys(patch).length === 0) {
            close();
            return;
        }
        var saveBtn = overlayEl.querySelector('#settings-save-btn');
        var statusEl = overlayEl.querySelector('#settings-save-status');
        if (saveBtn) saveBtn.disabled = true;
        if (statusEl) statusEl.textContent = 'saving...';
        try {
            lastSummary = await window.API.updateSettings(patch);
            if (statusEl) statusEl.textContent = 'saved';
            var savedWarnings = lastSummary && lastSummary.workspace_warnings;
            var bodyEl = overlayEl.querySelector('#settings-panel-body');
            if (bodyEl) {
                // Repaint from the authoritative post-write summary, then
                // put the user back on the tab they saved from - a save
                // that silently bounced them to the first tab would read
                // as the panel having lost their place.
                var priorTab = window.SettingsTabs.activeTab(bodyEl);
                bodyEl.innerHTML = renderBody();
                window.SettingsTabs.wire(bodyEl);
                if (priorTab) window.SettingsTabs.activate(bodyEl, priorTab);
                mountSlots();
                // Warnings are the successful-save channel: a warned env
                // name was SAVED, and the user has to be told which one.
                // Rendered after the repaint so it survives it.
                if (window.SettingsWorkspace) {
                    window.SettingsWorkspace.showWarnings(bodyEl, savedWarnings);
                }
            }
        } catch (err) {
            console.error('SettingsPanel: save failed', err);
            if (statusEl) statusEl.textContent = 'save failed: ' + (err && err.message ? err.message : 'unknown error');
        } finally {
            if (saveBtn) saveBtn.disabled = false;
        }
    }

    /**
     * Mount the theme `<select>` into this panel's slot. Idempotent -
     * ThemeSelector.mount() repopulates an existing `#theme-selector`
     * rather than duplicating it, so calling this on every render (open,
     * post-save) is safe.
     * Output: void.
     */
    function mountThemeSlot() {
        if (!window.ThemeSelector) return;
        var slot = overlayEl.querySelector('#settings-theme-slot');
        if (slot) window.ThemeSelector.mount(slot);
    }

    /**
     * Mount every bespoke panel into its slot. These panels own their own
     * DOM and write immediately rather than joining this panel's batched
     * Save (see agent-wrappers-panel.js / terminal-commands-panel.js).
     * Called on open and after every save, same as mountThemeSlot.
     * Output: void.
     */
    function mountSlots() {
        mountThemeSlot();
        if (window.SettingsWorkspace) {
            window.SettingsWorkspace.wire(overlayEl.querySelector('#settings-panel-body'));
        }
        if (window.SettingsAudio) window.SettingsAudio.wire(overlayEl);
        if (window.AgentWrappersPanel) {
            var wrapperSlot = overlayEl.querySelector('#settings-wrappers-slot');
            if (wrapperSlot) window.AgentWrappersPanel.mount(wrapperSlot);
        }
        if (window.TerminalCommandsPanel) {
            var commandSlot = overlayEl.querySelector('#settings-terminal-commands-slot');
            if (commandSlot) {
                // Seeded from the settings summary we already fetched, so
                // opening settings stays one round trip.
                window.TerminalCommandsPanel.mount(
                    commandSlot,
                    (lastSummary && lastSummary.terminal_commands) || null
                );
            }
        }
    }

    /**
     * Close the panel: remove the overlay from the DOM and return focus
     * to whatever triggered the open (the gear button, normally) - same
     * dismissal contract as `AppController.showConfirmModal`.
     * Output: void.
     */
    function close() {
        if (overlayEl && overlayEl.parentNode) {
            overlayEl.parentNode.removeChild(overlayEl);
        }
        overlayEl = null;
        if (triggerEl && typeof triggerEl.focus === 'function') {
            try { triggerEl.focus(); } catch (_) { /* no-op */ }
        }
        triggerEl = null;
    }

    /**
     * Open the settings panel: fetch the current summary, build the
     * overlay, wire Escape/backdrop/close/save, and move focus in.
     * Safe to call while already open - just re-focuses.
     * Inputs: opener (Element|null) - element to refocus on close.
     * Output: Promise<void>.
     */
    async function open(opener) {
        if (overlayEl) return; // already open
        triggerEl = opener || document.activeElement;

        try {
            lastSummary = await window.API.getSettings();
        } catch (err) {
            console.error('SettingsPanel: failed to load settings', err);
            lastSummary = { agents: {}, notifications: {}, server: {}, terminal_commands: null, workspace: {}, server_prefs: {} };
        }

        overlayEl = document.createElement('div');
        overlayEl.className = 'modal-overlay';
        overlayEl.setAttribute('data-modal', 'settings-panel');

        overlayEl.innerHTML = (
            '<div class="settings-modal-content" role="dialog" aria-modal="true" aria-labelledby="settings-panel-title">' +
            '  <div class="modal-header settings-panel-header">' +
            '    <h2 id="settings-panel-title">settings</h2>' +
            '    <button type="button" class="modal-close" id="settings-close-btn" aria-label="Close settings" title="close">&times;</button>' +
            '  </div>' +
            '  <div class="modal-body settings-panel-body" id="settings-panel-body">' + renderBody() + '</div>' +
            '  <div class="modal-footer settings-panel-footer">' +
            '    <span id="settings-save-status" class="settings-save-status"></span>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" id="settings-cancel-btn">cancel</button>' +
            '    <button type="button" class="modal-btn modal-btn-primary" id="settings-save-btn">save</button>' +
            '  </div>' +
            '</div>'
        );

        document.body.appendChild(overlayEl);
        window.SettingsTabs.wire(overlayEl.querySelector('#settings-panel-body'));
        mountSlots();

        overlayEl.querySelector('#settings-close-btn').addEventListener('click', close);
        overlayEl.querySelector('#settings-cancel-btn').addEventListener('click', close);
        overlayEl.querySelector('#settings-save-btn').addEventListener('click', function () {
            save();
        });
        overlayEl.addEventListener('click', function (e) {
            if (e.target === overlayEl) close();
        });
        overlayEl.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                e.stopPropagation();
                close();
            }
        });

        var closeBtn = overlayEl.querySelector('#settings-close-btn');
        setTimeout(function () {
            try { closeBtn.focus(); } catch (_) { /* no-op */ }
        }, 50);
    }

    window.SettingsPanel = { open: open, close: close };
})();
