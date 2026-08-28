/**
 * AgentWrappersView - pure render functions for the wrappers settings
 * screen (feat/universal-wrappers). No state, no fetching, no event
 * wiring: every function takes data and returns an HTML string, so the
 * grouping rules are readable in one place and testable without a DOM.
 * Behavior lives in agent-wrappers-panel.js.
 *
 * Split out for the repo's 500-line file budget, the same reason
 * settings-sections.js was split from settings-panel.js.
 *
 * ONE SCREEN, EVERY FAMILY
 * ------------------------
 * Wrappers used to be claude-only. A wrapper now declares a `family`
 * (claude, codex, hermes, openclaw, shell - see src/core/agent_families.py)
 * and this screen renders ONE GROUP PER FAMILY: its wrappers, an add
 * button scoped to that family, and the family's legacy static command as
 * a collapsed advanced row.
 *
 * The family list is NEVER hardcoded here. It arrives with every wrapper
 * API response (`families`, built from the server-side registry), so
 * adding a family server-side reaches this screen with no JS edit at all
 * - which is the whole point of the registry.
 *
 * Each family group renders even when EMPTY, because an empty group is
 * how a user discovers they can wrap codex at all. An empty group is
 * exactly the state where the legacy command row says "in use now".
 */
(function () {
    'use strict';

    /**
     * Escape a string for safe interpolation into innerHTML-built markup.
     * Inputs: str (any) - value to escape.
     * Output: string - HTML-safe text.
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    /**
     * The family a wrapper belongs to, tolerating a wrapper written before
     * the field existed (a config restored from backup, or a v2 config
     * whose migration has not run yet).
     * Inputs: w (object) - AgentWrapper-shaped dict.
     * Output: string - family name, defaulting to 'claude'.
     */
    function familyOf(w) {
        return (w && w.family) || 'claude';
    }

    /**
     * Group wrappers by family, in the FAMILY REGISTRY's order.
     * Inputs:
     *   wrappers (array) - every configured wrapper, all families.
     *   families (array) - family summaries from the API.
     * Output: array - [{family, wrappers}], one entry per known family,
     *   plus a trailing entry for any unknown family found in the data so
     *   a hand-edited config can never make a wrapper invisible.
     */
    function groupByFamily(wrappers, families) {
        var list = wrappers || [];
        var known = (families || []).map(function (f) {
            return { family: f, wrappers: list.filter(function (w) { return familyOf(w) === f.name; }) };
        });
        var knownNames = (families || []).map(function (f) { return f.name; });
        // A wrapper whose family the server does not know about would
        // otherwise vanish from the screen entirely, which is worse than
        // showing it under a bare heading.
        var orphanNames = [];
        list.forEach(function (w) {
            var name = familyOf(w);
            if (knownNames.indexOf(name) === -1 && orphanNames.indexOf(name) === -1) {
                orphanNames.push(name);
            }
        });
        orphanNames.forEach(function (name) {
            known.push({
                family: { name: name, label: name, description: '', wrapper_count: 0, in_use: false, command: '' },
                wrappers: list.filter(function (w) { return familyOf(w) === name; }),
            });
        });
        return known;
    }

    /**
     * Render one wrapper's summary row.
     *
     * TWO LINES, NOT TWO COLUMNS. The description used to live inside
     * `.settings-wrapper-row-main`, i.e. in the left column of a
     * horizontal flex row whose right column (the action buttons) is
     * `flex-shrink: 0`. The description therefore got whatever width the
     * buttons left over, and that leftover is a function of HOW MANY
     * BUTTONS THE ROW HAS: the default wrapper draws two, every other
     * wrapper draws three. Measured in a 556px settings modal at 1400px:
     * the two-button row gave its description 272.1px and the
     * three-button row gave the SAME text 115.5px - a column about
     * fourteen characters wide, so a one-sentence description rendered as
     * a five-line ribbon and no two rows agreed on a width. The phone
     * layout never showed it because the 480px media query already stacks
     * the row.
     *
     * So the description now sits on its OWN full-width line beneath the
     * title/actions line, where its width is the row's width in every
     * case and does not depend on the buttons above it.
     *
     * It is omitted entirely when there is nothing to say. An empty
     * `.settings-field-hint` is not free - it carries `margin-top: 4px`
     * and left a ragged gap under wrappers with no description.
     *
     * Inputs: w (object) - AgentWrapper-shaped dict.
     * Output: string - HTML.
     */
    function renderRow(w) {
        var badge = w.default ? '<span class="settings-wrapper-badge">default</span>' : '';
        var modelBadge = w.accepts_model
            ? '<span class="settings-wrapper-badge settings-wrapper-badge-model">takes model</span>'
            : '';
        var entryNote = w.entry ? (' &middot; entry: <code>' + escapeHtml(w.entry) + '</code>') : '';
        var id = escapeHtml(w.id);
        var descHtml = (w.description || w.entry)
            ? ('  <div class="settings-wrapper-row-desc settings-field-hint">' +
               escapeHtml(w.description || '') + entryNote + '</div>')
            : '';
        return (
            '<div class="settings-wrapper-row" data-wrapper-id="' + id + '">' +
            '  <div class="settings-wrapper-row-head">' +
            '    <div class="settings-wrapper-row-main">' +
            '      <strong>' + escapeHtml(w.label) + '</strong> <code>' + id + '</code> ' + badge + modelBadge +
            '    </div>' +
            '    <div class="settings-wrapper-row-actions">' +
            '      <button type="button" class="modal-btn modal-btn-secondary" data-wrapper-action="edit" data-wrapper-id="' + id + '">edit</button>' +
            (w.default ? '' : '      <button type="button" class="modal-btn modal-btn-secondary" data-wrapper-action="default" data-wrapper-id="' + id + '">set default</button>') +
            '      <button type="button" class="modal-btn modal-btn-danger" data-wrapper-action="delete" data-wrapper-id="' + id + '">delete</button>' +
            '    </div>' +
            '  </div>' +
            descHtml +
            '</div>'
        );
    }

    /**
     * Config keys the settings PATCH endpoint accepts (see
     * ``ConfigSettingsUpdateRequest`` in src/models.py). ``shell_command``
     * is deliberately absent server-side, so its row stays read-only and
     * SAYS so rather than offering an edit that would 422.
     */
    var EDITABLE_COMMAND_FIELDS = [
        'claude_command', 'codex_command', 'hermes_command', 'openclaw_command',
    ];

    /**
     * Whether a family's static command can be edited from this screen.
     * Inputs: family (object) - one family summary from the API.
     * Output: boolean.
     */
    function legacyIsEditable(family) {
        return EDITABLE_COMMAND_FIELDS.indexOf(
            family.command_field || (family.name + '_command')
        ) !== -1;
    }

    /**
     * Render a family's legacy static command as a collapsed advanced row
     * inside that family's group.
     *
     * WHY THIS IS NOW AN EDIT SURFACE. The settings screen used to carry a
     * separate "agents" tab holding three editable text fields
     * (codex/hermes/openclaw command). That tab was the old half of a
     * split this feature already unified: wrappers cover every family, so
     * the tab administered a fallback for families whose primary surface
     * lives here. Deleting it outright would have deleted the only place
     * those keys could be edited, so the edit moved INTO this row rather
     * than the row staying a read-only mirror of a field somewhere else.
     *
     * It writes IMMEDIATELY through its own button, exactly like every
     * other action on this screen, and carries no `data-settings-key`.
     * That preserves the original invariant behind the disabled input:
     * these keys can never be swept into settings-panel.js's batched
     * Save. What changes is that they are reachable at all.
     *
     * The copy still states plainly which of the two states the value is
     * in, because a value sitting in config.json that does nothing is
     * worse than one shown and labelled inert.
     * Inputs: family (object) - one family summary from the API.
     * Output: string - HTML for a collapsed `<details>` block.
     */
    function renderLegacyCommand(family) {
        var name = escapeHtml(family.name);
        var editable = legacyIsEditable(family);
        var copy = family.in_use
            ? 'in use now. ' + (family.description || '')
            : 'not in use. wrappers above take precedence; this only runs if you delete every wrapper.';
        if (!editable) {
            copy += ' read only here: this key has no settings endpoint. edit it in config.json.';
        }
        var editorHtml = editable
            ? (
                '      <div class="settings-legacy-actions">' +
                '        <button type="button" class="modal-btn modal-btn-secondary"' +
                '          data-legacy-save="' + name + '"' +
                '          data-legacy-field="' + escapeHtml(family.command_field) + '">save command</button>' +
                '        <span class="settings-legacy-status" data-legacy-status="' + name + '"></span>' +
                '      </div>'
            )
            : '';
        return (
            '  <details class="settings-advanced" data-legacy-family="' + name + '">' +
            '    <summary class="settings-advanced-summary">advanced: legacy ' + escapeHtml(family.label) + ' command</summary>' +
            '    <div class="settings-field">' +
            '      <label class="settings-field-label" for="settings-legacy-' + name + '-command">' + escapeHtml(family.command_field || (family.name + '_command')) + '</label>' +
            '      <input type="text" id="settings-legacy-' + name + '-command" class="modal-input"' +
            '        data-legacy-input="' + name + '"' +
            '        value="' + escapeHtml(family.command || '') + '"' + (editable ? '' : ' readonly disabled') + '>' +
            '      <div class="settings-field-hint">' + escapeHtml(copy) + '</div>' +
            editorHtml +
            // Only meaningful when this field IS what runs. With wrappers
            // present the resolver answers from the wrapper list, so
            // printing it here would attach a wrapper's command line to an
            // inert legacy field and read as if that field produced it.
            (family.in_use && family.effective_command
                ? '      <div class="settings-field-effective">what runs now: <code>' + escapeHtml(family.effective_command) + '</code></div>'
                : '') +
            '    </div>' +
            '  </details>'
        );
    }

    /**
     * Render one family group: heading, its wrapper rows (or an empty
     * hint), its scoped add button, and its legacy command row.
     * Inputs:
     *   group (object) - {family, wrappers} from groupByFamily.
     *   editorOpen (bool) - whether an editor is open anywhere on the
     *     screen; the add buttons are hidden while one is, so there is
     *     never more than one open form.
     * Output: string - HTML.
     */
    function renderFamilyGroup(group, editorOpen) {
        var family = group.family;
        var name = escapeHtml(family.name);
        var rows = group.wrappers.map(renderRow).join('');
        var empty = '<div class="settings-field-hint">no ' + escapeHtml(family.label) +
            ' wrappers. the legacy command below is what runs.</div>';
        var addBtn = editorOpen ? '' : (
            '  <div class="settings-wrapper-actions-row">' +
            '    <button type="button" class="modal-btn modal-btn-secondary" data-wrapper-add-family="' + name + '">+ add ' + escapeHtml(family.label) + ' wrapper</button>' +
            // Every family has an offered example now (see
            // src/core/agent_wrappers.EXAMPLE_WRAPPERS), so the import
            // action is no longer claude-only. It used to be, because the
            // example list held two claude entries and nothing else, and
            // a button that opened an empty picker would have been worse
            // than no button.
            '    <button type="button" class="modal-btn modal-btn-secondary" data-wrapper-import-family="' + name + '">import example</button>' +
            '  </div>'
        );
        return (
            '<div class="settings-wrapper-family" data-family="' + name + '">' +
            '  <h4 class="settings-wrapper-family-title">' + escapeHtml(family.label) +
            '    <span class="settings-wrapper-family-count">' + group.wrappers.length + '</span>' +
            '  </h4>' +
            '  <div class="settings-wrapper-list">' + (rows || empty) + '</div>' +
            addBtn +
            renderLegacyCommand(family) +
            '</div>'
        );
    }

    /**
     * Render the inline add/edit editor form.
     * Inputs:
     *   w (object) - the wrapper being edited, or a blank seed for a new one.
     *   isNew (bool) - whether this is an add (id editable) or an edit.
     *   families (array) - family summaries, for the family select.
     * Output: string - HTML.
     */
    function renderEditor(w, isNew, families) {
        var options = (families || []).map(function (f) {
            var sel = (f.name === familyOf(w)) ? ' selected' : '';
            return '<option value="' + escapeHtml(f.name) + '"' + sel + '>' + escapeHtml(f.label) + '</option>';
        }).join('');
        return (
            '<div class="settings-wrapper-editor">' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-family">family</label>' +
            '    <select id="wrapper-field-family" class="modal-input"' + (isNew ? '' : ' disabled') + '>' + options + '</select>' +
            '    <div class="settings-field-hint">which command this wrapper wraps. fixed after creation, because changing it would move the wrapper out from under sessions already launched through it.</div>' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-id">id</label>' +
            '    <input type="text" id="wrapper-field-id" class="modal-input" placeholder="e.g. cld" value="' + escapeHtml(w.id) + '" ' + (isNew ? '' : 'readonly disabled') + '>' +
            '    <div class="settings-field-hint">lowercase letters/digits/-/_ only. also usable directly as the launch agent type. never changes once set.</div>' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-label">label</label>' +
            '    <input type="text" id="wrapper-field-label" class="modal-input" placeholder="e.g. cld (subscription)" value="' + escapeHtml(w.label) + '">' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-script">script</label>' +
            '    <textarea id="wrapper-field-script" class="modal-input settings-wrapper-script-input" rows="16" spellcheck="false" placeholder="claude --dangerously-skip-permissions">' + escapeHtml(w.script) + '</textarea>' +
            '    <div class="settings-field-hint">a single command, or a full function definition (paste it exactly as it appears in your shell rc file).</div>' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-entry">entry (optional)</label>' +
            '    <input type="text" id="wrapper-field-entry" class="modal-input" placeholder="e.g. cld" value="' + escapeHtml(w.entry || '') + '">' +
            '    <div class="settings-field-hint">only needed if script DEFINES a function: the function name to call after sourcing. leave blank if script is already a directly-runnable command.</div>' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="wrapper-field-description">description (optional)</label>' +
            '    <input type="text" id="wrapper-field-description" class="modal-input" value="' + escapeHtml(w.description || '') + '">' +
            '  </div>' +
            '  <div class="settings-field settings-field-checkbox">' +
            '    <label class="settings-field-label" for="wrapper-field-default">' +
            '      <input type="checkbox" id="wrapper-field-default" ' + (w.default ? 'checked' : '') + '> make this the default for its family' +
            '    </label>' +
            '  </div>' +
            '  <div class="settings-field settings-field-checkbox">' +
            '    <label class="settings-field-label" for="wrapper-field-accepts-model">' +
            '      <input type="checkbox" id="wrapper-field-accepts-model" ' + (w.accepts_model ? 'checked' : '') + '> takes a model id as its first argument' +
            '    </label>' +
            '    <div class="settings-field-hint">only tick this for a wrapper that consumes a model id (the cldor shape). when off, the launch picker never offers a model and the server drops one if sent.</div>' +
            '  </div>' +
            '  <div class="settings-wrapper-editor-actions">' +
            '    <span id="wrapper-editor-status" class="settings-save-status"></span>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" id="wrapper-editor-cancel">cancel</button>' +
            '    <button type="button" class="modal-btn modal-btn-primary" id="wrapper-editor-save">' + (isNew ? 'add' : 'save') + '</button>' +
            '  </div>' +
            '</div>'
        );
    }

    /**
     * Render the whole wrappers section: intro, security note, one group
     * per family, and the inline editor when open.
     * Inputs:
     *   wrappers (array) - every configured wrapper.
     *   families (array) - family summaries from the API.
     *   editorHtml (string) - rendered editor, or '' when none is open.
     * Output: string - HTML for the `.settings-section`.
     */
    function renderSection(wrappers, families, editorHtml) {
        var groups = groupByFamily(wrappers, families).map(function (g) {
            return renderFamilyGroup(g, !!editorHtml);
        }).join('');
        return (
            '<section class="settings-section" data-settings-section="wrappers">' +
            '  <h3 class="settings-section-title">launch wrappers</h3>' +
            '  <div class="settings-section-description">' +
            '    named shell commands used to launch a session. one group per command family. ' +
            '    pick a wrapper at launch time, or set a default per family. a family with no ' +
            '    wrappers falls back to its legacy command.' +
            '  </div>' +
            '  <div class="settings-warning settings-wrapper-security-note">' +
            '    a wrapper is a shell command. never paste a token or password into it. read secrets ' +
            '    from the macOS Keychain at run time instead (the example \'cld\' wrapper shows the pattern).' +
            '  </div>' +
            (editorHtml || '') +
            groups +
            '</section>'
        );
    }

    window.AgentWrappersView = {
        escapeHtml: escapeHtml,
        familyOf: familyOf,
        legacyIsEditable: legacyIsEditable,
        EDITABLE_COMMAND_FIELDS: EDITABLE_COMMAND_FIELDS,
        groupByFamily: groupByFamily,
        renderRow: renderRow,
        renderLegacyCommand: renderLegacyCommand,
        renderFamilyGroup: renderFamilyGroup,
        renderEditor: renderEditor,
        renderSection: renderSection,
    };
})();
