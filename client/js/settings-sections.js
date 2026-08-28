/**
 * SettingsSections - the section renderers for the settings modal
 * (client/js/settings-panel.js).
 *
 * Pure functions: each takes the last `GET /config/settings` summary plus
 * whatever it needs, and returns an HTML string. No state, no DOM reads,
 * no event wiring - all of that stays in settings-panel.js, which owns
 * the panel lifecycle and the batched Save.
 *
 * Split out of settings-panel.js when the tab layout pushed that file
 * past the repo's 500-line budget. Must load BEFORE settings-panel.js
 * (see client/index.html script order).
 */
(function () {
    'use strict';
    /**
     * Escape a string for safe interpolation into innerHTML-built markup.
     * Description: mirrors AppController._escapeHtml - duplicated locally
     *   (single small pure function) rather than reaching into `App`,
     *   since this module must also work before app.js has finished
     *   constructing `window.App`.
     * Inputs: str (any) - value to escape; stringified first.
     * Output: string - HTML-escaped text.
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    /**
     * Render one field's markup for a generic (agent/notifications) section.
     * Inputs:
     *   field (object) - one entry from AGENT_FIELDS/NOTIFICATION_FIELDS.
     *   currentValue (any) - the value from `lastSummary` for this field
     *     (raw string/bool for plain fields, `{configured: bool}` for
     *     secret fields).
     *   effectiveText (string|null) - only used when field.showEffective -
     *     the "what runs now" preview line.
     * Output: string - HTML for one `.settings-field` block.
     */
    function renderField(field, currentValue, effectiveText) {
        var inputId = 'settings-field-' + field.key;
        var labelHtml = '<label class="settings-field-label" for="' + inputId + '">' + escapeHtml(field.label) + '</label>';

        if (field.type === 'checkbox') {
            var checked = currentValue ? 'checked' : '';
            return (
                '<div class="settings-field settings-field-checkbox">' +
                '  <label class="settings-field-label" for="' + inputId + '">' +
                '    <input type="checkbox" id="' + inputId + '" data-settings-key="' + field.key + '" data-settings-type="checkbox" ' + checked + '>' +
                '    ' + escapeHtml(field.label) +
                '  </label>' +
                '</div>'
            );
        }

        if (field.type === 'secret') {
            var configured = !!(currentValue && currentValue.configured);
            var placeholder = configured ? 'configured - leave blank to keep' : 'not set';
            return (
                '<div class="settings-field">' +
                labelHtml +
                '  <input type="password" autocomplete="new-password" id="' + inputId + '" ' +
                '    data-settings-key="' + field.key + '" data-settings-type="secret" ' +
                '    class="modal-input" placeholder="' + escapeHtml(placeholder) + '">' +
                '  <div class="settings-field-hint">' + (configured ? 'a value is set - typing here replaces it' : 'not configured - this channel is disabled') + '</div>' +
                '</div>'
            );
        }

        // text
        var val = currentValue == null ? '' : String(currentValue);
        var effectiveHtml = '';
        if (field.showEffective && effectiveText) {
            effectiveHtml = '<div class="settings-field-effective">what runs now: <code>' + escapeHtml(effectiveText) + '</code></div>';
        }
        return (
            '<div class="settings-field">' +
            labelHtml +
            '  <input type="text" id="' + inputId + '" data-settings-key="' + field.key + '" data-settings-type="text" ' +
            '    class="modal-input" placeholder="' + escapeHtml(field.placeholder || '') + '" value="' + escapeHtml(val) + '">' +
            (field.hint ? '  <div class="settings-field-hint">' + escapeHtml(field.hint) + '</div>' : '') +
            effectiveHtml +
            '</div>'
        );
    }

    /**
     * Render a full generic section (heading + description + restart
     * notice + its fields), reading current values out of `lastSummary`.
     * Inputs: section (object) - one entry from SECTIONS.
     * Output: string - HTML for one `.settings-section`.
     */
    function renderGenericSection(section, lastSummary) {
        var groupData = (lastSummary && lastSummary[section.group]) || {};
        var restartHtml = section.restartRequired
            ? '<div class="settings-restart-note">changes here need a server restart to take effect</div>'
            : '';
        var fieldsHtml = section.fields.map(function (field) {
            var effectiveText = field.showEffective ? groupData.effective_claude_command : null;
            return renderField(field, groupData[field.key], effectiveText);
        }).join('');
        return (
            '<section class="settings-section" data-settings-section="' + section.id + '" data-settings-group="' + section.group + '">' +
            '  <h3 class="settings-section-title">' + escapeHtml(section.title) + '</h3>' +
            '  <div class="settings-section-description">' + escapeHtml(section.description) + '</div>' +
            restartHtml +
            fieldsHtml +
            '</section>'
        );
    }

    /**
     * Render the appearance section shell. The actual `<select>` is
     * mounted into `#settings-theme-slot` by `window.ThemeSelector.mount`
     * right after this HTML is inserted (see `renderPanel`) - it can't be
     * built as a string because ThemeSelector owns its own DOM node and
     * change-listener wiring.
     * Output: string - HTML for the appearance `.settings-section`.
     */
    function renderAppearanceSection() {
        return (
            '<section class="settings-section" data-settings-section="appearance">' +
            '  <h3 class="settings-section-title">appearance</h3>' +
            '  <div class="settings-section-description">theme applies immediately - no save needed.</div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="theme-selector">theme</label>' +
            '    <div id="settings-theme-slot"></div>' +
            '  </div>' +
            '</section>'
        );
    }

    /**
     * Render the server section: read-only HOST bind address plus an
     * inline safety warning. Deliberately not editable - see the
     * settings-screen spec's HOST handling: it lives in `.env`, not
     * config.json, has no atomic-write convention in this codebase, and
     * the launchd wrapper on the deploy host REFUSES TO BOOT on a
     * wildcard/empty bind, so a bad edit here would strand the server
     * with no way to fix it from the now-unreachable UI. Read-only +
     * loud warning is the safer contract than a "confirm to bypass" edit
     * path would be.
     * Output: string - HTML for the server `.settings-section`.
     */
    function renderServerSection(lastSummary) {
        var server = (lastSummary && lastSummary.server) || {};
        var wildcard = !!server.wildcard_bind;
        var warningHtml = wildcard
            ? (
                '<div class="settings-warning">' +
                '  <strong>this bind address exposes a remote shell on every network interface.</strong> ' +
                '  the server\'s startup guard refuses to boot when HOST is 0.0.0.0, empty, or missing - ' +
                '  if this got here from a hand-edited .env, the app will not come back up after a restart. ' +
                '  fix .env directly on the host, then restart.' +
                '</div>'
            )
            : (
                '<div class="settings-field-hint">bound to a specific interface - not exposed on every network.</div>'
            );
        return (
            '<section class="settings-section" data-settings-section="server">' +
            '  <h3 class="settings-section-title">server</h3>' +
            '  <div class="settings-section-description">bind address - read only. lives in .env, not config.json; edit it on the host directly.</div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label">host</label>' +
            '    <input type="text" class="modal-input" value="' + escapeHtml(server.host || '') + '" readonly disabled>' +
            '  </div>' +
            warningHtml +
            '</section>'
        );
    }

    window.SettingsSections = {
        escapeHtml: escapeHtml,
        renderField: renderField,
        renderGenericSection: renderGenericSection,
        renderAppearanceSection: renderAppearanceSection,
        renderServerSection: renderServerSection,
    };
})();
