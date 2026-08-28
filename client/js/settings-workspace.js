/**
 * SettingsWorkspace - the "workspace" tab: the four global settings.
 *
 *   development root   base directory for projects
 *   default shell      what a new terminal runs
 *   environment        arbitrary NAME=value pairs injected on spawn
 *   default editor     what opens when the app hands you a file
 *   bind + TLS         remembered across restarts
 *
 * It follows settings-sections.js's shape (a pure `render()` returning a
 * string) but adds `wire()` and `collect()`, because the environment list
 * is the one control on this screen with a variable number of rows and a
 * string-driven renderer cannot grow or shrink one.
 *
 * WHAT THIS TAB PROMISES, AND WHAT IT REFUSES TO IMPLY
 *
 * Two of these settings do not apply to what is already on screen, and
 * saying so is part of the feature rather than a caveat bolted on:
 *
 *   - The environment is copied by tmux at new-session time. A session
 *     already running keeps the environment it was born with, forever.
 *     The tab says that next to the list, because a user who believes a
 *     save reached his open terminals will spend the afternoon debugging
 *     the wrong thing. tests/test_workspace_env_reaches_terminal.py is
 *     the measurement behind the sentence.
 *   - uvicorn binds its socket once. A bind change takes effect on the
 *     next restart, exactly as the setup wizard's final step says, and
 *     this tab shows the address IN FORCE beside the one that is saved
 *     rather than letting the stored value pose as the current one.
 *
 * TLS is rendered as a third state, not a switch. This build has no TLS
 * terminator at all (see macOS/tls-status.js, which refuses to draw a
 * padlock it did not measure), so the row records a preference and says
 * out loud that it is not in force. A toggle that silently did nothing
 * would be the same defect one layer up.
 *
 * COLOURS COME FROM TOKENS ONLY. There are 23 themes and three of them
 * zero every radius token deliberately; nothing here may hardcode a hex
 * value or a corner.
 *
 * Loads BEFORE settings-panel.js (see client/index.html script order).
 */
(function () {
    'use strict';

    var Sections = window.SettingsSections;
    var escapeHtml = Sections ? Sections.escapeHtml : function (s) { return String(s == null ? '' : s); };

    /**
     * Build one NAME/value row for the environment list.
     * Inputs:
     *   name (string) - variable name, may be ''.
     *   value (string) - variable value, may be ''.
     * Output: string - HTML for one `.settings-env-row`.
     */
    function renderEnvRow(name, value) {
        return (
            '<div class="settings-env-row" data-env-row>' +
            '  <input type="text" class="modal-input settings-env-name" data-env-name ' +
            '    placeholder="NAME" aria-label="environment variable name" ' +
            '    value="' + escapeHtml(name) + '">' +
            '  <input type="text" class="modal-input settings-env-value" data-env-value ' +
            '    placeholder="value" aria-label="environment variable value" ' +
            '    value="' + escapeHtml(value) + '">' +
            '  <button type="button" class="settings-env-remove" data-env-remove ' +
            '    aria-label="remove this environment variable" title="remove">&times;</button>' +
            '</div>'
        );
    }

    /**
     * Build the whole tab.
     * Inputs: summary (object|null) - the last GET /config/settings body.
     * Output: string - HTML for the workspace pane.
     */
    function render(summary) {
        var ws = (summary && summary.workspace) || {};
        var prefs = (summary && summary.server_prefs) || {};
        var env = ws.env || {};

        var names = Object.keys(env).sort();
        var rowsHtml = names.map(function (n) { return renderEnvRow(n, env[n]); }).join('');
        if (!rowsHtml) rowsHtml = renderEnvRow('', '');

        var saved = prefs.bind_host || '';
        var effective = prefs.effective_bind_host || '';
        // THREE states, not two. "saved is empty" is not the same as
        // "saved matches what is running", and neither is "they differ".
        var bindStateHtml;
        if (!effective) {
            // The address in force was never measured. Substituting the
            // saved preference here would print a plausible wrong address,
            // which is the defect this whole three-state block exists for -
            // and it is what the server used to send, because the payload
            // reported the configured value under the name "effective".
            bindStateHtml =
                '<div class="settings-field-hint" data-bind-state="unmeasured">' +
                'the address the server is listening on could not be ' +
                'determined. ' +
                (saved
                    ? 'your saved preference is <code>' + escapeHtml(saved) +
                      '</code>, which is what was requested and not proof of ' +
                      'what is reachable.'
                    : 'no preference is saved either.') +
                '</div>';
        } else if (!saved) {
            bindStateHtml =
                '<div class="settings-field-hint" data-bind-state="unset">' +
                'no preference saved. the server is on <code>' + escapeHtml(effective) + '</code> ' +
                'because of how it was launched.</div>';
        } else if (saved === effective) {
            bindStateHtml =
                '<div class="settings-field-hint" data-bind-state="in-force">' +
                'in force: the server is on <code>' + escapeHtml(effective) + '</code>.</div>';
        } else {
            bindStateHtml =
                '<div class="settings-warning" data-bind-state="pending">' +
                '<strong>not in force yet.</strong> the server is still on ' +
                '<code>' + escapeHtml(effective) + '</code>. a bind address is chosen once, ' +
                'when the server starts, so this takes effect after a restart. ' +
                'setup may also pin it to loopback until an authenticator is paired.' +
                '</div>';
        }

        return (
            '<section class="settings-section" data-settings-section="workspace">' +
            '  <h3 class="settings-section-title">workspace</h3>' +
            '  <div class="settings-section-description">' +
            '    what a NEW terminal is born with. sessions already open keep the ' +
            '    environment they started with - reopen one to pick these up.' +
            '  </div>' +

            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="settings-ws-root">development root</label>' +
            '    <input type="text" id="settings-ws-root" data-ws-key="development_root" ' +
            '      class="modal-input" placeholder="~/Development" ' +
            '      value="' + escapeHtml(ws.development_root || '') + '">' +
            '    <div class="settings-field-hint">base directory for projects. reaches terminals as ' +
            '      <code>CLOUDE_DEV_ROOT</code>. must already exist.</div>' +
            '  </div>' +

            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="settings-ws-shell">default shell</label>' +
            '    <input type="text" id="settings-ws-shell" data-ws-key="default_shell" ' +
            '      class="modal-input" placeholder="/bin/zsh" ' +
            '      value="' + escapeHtml(ws.default_shell || '') + '">' +
            '    <div class="settings-field-hint">reaches terminals as <code>SHELL</code>, which is what ' +
            '      the default console command <code>$SHELL -i</code> expands. must be executable.</div>' +
            '  </div>' +

            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="settings-ws-editor">default editor</label>' +
            '    <input type="text" id="settings-ws-editor" data-ws-key="default_editor" ' +
            '      class="modal-input" placeholder="code -w" ' +
            '      value="' + escapeHtml(ws.default_editor || '') + '">' +
            '    <div class="settings-field-hint">command run when the app opens a file for you. ' +
            '      arguments are allowed; the command itself must resolve.</div>' +
            '  </div>' +

            '  <div class="settings-field">' +
            '    <label class="settings-field-label">environment variables</label>' +
            '    <div class="settings-env-list" data-env-list>' + rowsHtml + '</div>' +
            '    <button type="button" class="modal-btn modal-btn-secondary settings-env-add" ' +
            '      data-env-add>add variable</button>' +
            '    <div class="settings-field-hint">injected into every new terminal. names beginning ' +
            '      <code>CLOUDECODE_</code> are reserved by the app and will be refused. ' +
            '      values are stored in plain text in config.json.</div>' +
            '    <div class="settings-env-warnings" data-env-warnings hidden></div>' +
            '  </div>' +
            '</section>' +

            '<section class="settings-section" data-settings-section="server-prefs">' +
            '  <h3 class="settings-section-title">bind and TLS</h3>' +
            '  <div class="settings-section-description">' +
            '    remembered across restarts, so the address does not have to be re-picked ' +
            '    from the menu bar each time.' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="settings-ws-bind">bind address</label>' +
            '    <input type="text" id="settings-ws-bind" data-prefs-key="bind_host" ' +
            '      class="modal-input" placeholder="127.0.0.1" value="' + escapeHtml(saved) + '">' +
            bindStateHtml +
            '  </div>' +
            '  <div class="settings-field settings-field-checkbox">' +
            '    <label class="settings-field-label" for="settings-ws-tls">' +
            '      <input type="checkbox" id="settings-ws-tls" data-prefs-key="tls_preferred"' +
            (prefs.tls_preferred ? ' checked' : '') + '>' +
            '      prefer TLS' +
            '    </label>' +
            '    <div class="settings-field-hint" data-tls-state="unavailable">' +
            '      recorded, <strong>not in force</strong>: this build serves plain HTTP and has no ' +
            '      TLS terminator, so nothing measures a certificate yet. the preference is kept ' +
            '      so it is not lost when one lands.</div>' +
            '  </div>' +
            '</section>'
        );
    }

    /**
     * Wire the add/remove buttons for the environment list.
     * Inputs: rootEl (Element) - the panel body containing this pane.
     * Output: void.
     */
    function wire(rootEl) {
        if (!rootEl) return;
        var listEl = rootEl.querySelector('[data-env-list]');
        var addBtn = rootEl.querySelector('[data-env-add]');
        if (!listEl || !addBtn) return;

        addBtn.addEventListener('click', function () {
            listEl.insertAdjacentHTML('beforeend', renderEnvRow('', ''));
            var rows = listEl.querySelectorAll('[data-env-row]');
            var last = rows[rows.length - 1];
            var nameInput = last && last.querySelector('[data-env-name]');
            if (nameInput) nameInput.focus();
        });

        listEl.addEventListener('click', function (e) {
            var btn = e.target.closest ? e.target.closest('[data-env-remove]') : null;
            if (!btn) return;
            var row = btn.closest('[data-env-row]');
            if (!row) return;
            row.parentNode.removeChild(row);
            // Never leave the list with nothing in it: an empty list has
            // no affordance to type into and reads as a broken control.
            if (!listEl.querySelector('[data-env-row]')) {
                listEl.insertAdjacentHTML('beforeend', renderEnvRow('', ''));
            }
        });
    }

    /**
     * Read the environment rows out of the DOM.
     * Inputs: rootEl (Element).
     * Output: object - NAME -> value. A row with a blank NAME is dropped,
     *   which is how the always-present empty row stays invisible to the
     *   save rather than becoming a variable called "".
     */
    function collectEnv(rootEl) {
        var env = {};
        rootEl.querySelectorAll('[data-env-row]').forEach(function (row) {
            var nameEl = row.querySelector('[data-env-name]');
            var valueEl = row.querySelector('[data-env-value]');
            if (!nameEl) return;
            var name = (nameEl.value || '').trim();
            if (!name) return;
            env[name] = valueEl ? valueEl.value : '';
        });
        return env;
    }

    /**
     * Build the PATCH fragments for this tab.
     * Inputs:
     *   rootEl (Element) - the panel body.
     *   summary (object|null) - the seeded values, so an untouched field
     *     is omitted and keeps the server's "leave unchanged" contract.
     * Output: object - `{workspace?: {...}, server_prefs?: {...}}`, each
     *   key present only when something in it actually changed.
     */
    function collect(rootEl, summary) {
        var out = {};
        if (!rootEl) return out;

        var seededWs = (summary && summary.workspace) || {};
        var ws = {};
        rootEl.querySelectorAll('[data-ws-key]').forEach(function (el) {
            var key = el.getAttribute('data-ws-key');
            var seeded = seededWs[key] == null ? '' : String(seededWs[key]);
            if (el.value !== seeded) ws[key] = el.value;
        });

        // The env map is sent WHOLE or not at all - a key-wise merge has
        // no way to express deleting a row, so a removal would silently
        // not happen. Compared against the seed so an untouched list is
        // still omitted.
        var envList = rootEl.querySelector('[data-env-list]');
        if (envList) {
            var current = collectEnv(rootEl);
            var seededEnv = seededWs.env || {};
            if (JSON.stringify(sortedPairs(current)) !== JSON.stringify(sortedPairs(seededEnv))) {
                ws.env = current;
            }
        }
        if (Object.keys(ws).length) out.workspace = ws;

        var seededPrefs = (summary && summary.server_prefs) || {};
        var prefs = {};
        rootEl.querySelectorAll('[data-prefs-key]').forEach(function (el) {
            var key = el.getAttribute('data-prefs-key');
            if (el.type === 'checkbox') {
                if (el.checked !== !!seededPrefs[key]) prefs[key] = el.checked;
                return;
            }
            var seeded = seededPrefs[key] == null ? '' : String(seededPrefs[key]);
            if (el.value !== seeded) prefs[key] = el.value;
        });
        if (Object.keys(prefs).length) out.server_prefs = prefs;

        return out;
    }

    /**
     * Stable comparison form for an env map.
     * Inputs: obj (object).
     * Output: Array<[string, string]> sorted by name.
     */
    function sortedPairs(obj) {
        return Object.keys(obj || {}).sort().map(function (k) {
            return [k, String(obj[k] == null ? '' : obj[k])];
        });
    }

    /**
     * Render the warnings the server returned for a successful save.
     * Inputs:
     *   rootEl (Element) - the panel body.
     *   warnings (Array<string>|null) - one sentence per warned name.
     * Output: void. An empty list HIDES the block rather than leaving an
     *   empty bordered box, which reads as a control that failed.
     */
    function showWarnings(rootEl, warnings) {
        if (!rootEl) return;
        var box = rootEl.querySelector('[data-env-warnings]');
        if (!box) return;
        if (!warnings || !warnings.length) {
            box.hidden = true;
            box.innerHTML = '';
            return;
        }
        box.hidden = false;
        box.innerHTML = warnings.map(function (w) {
            return '<div class="settings-warning">' + escapeHtml(w) + '</div>';
        }).join('');
    }

    window.SettingsWorkspace = {
        render: render,
        wire: wire,
        collect: collect,
        collectEnv: collectEnv,
        showWarnings: showWarnings,
        renderEnvRow: renderEnvRow,
    };
})();
