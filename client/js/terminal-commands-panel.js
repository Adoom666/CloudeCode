/**
 * TerminalCommandsPanel - the settings modal's "terminal" tab: a
 * user-editable, reorderable list of common shell commands, each runnable
 * in one click.
 *
 * Clicking "run" does NOT execute anything through an API. It opens a
 * normal console session (the same one the "new console" FAB creates,
 * Launchpad.createConsoleSession) and asks the server to type the stored
 * command into that pane. The user watches it run in a real terminal and
 * can Ctrl-C it. Only the command's ID crosses the wire; the text is read
 * from the user's own config server-side. See
 * src/core/terminal_commands.py's module docstring for why that
 * distinction is the whole security model here.
 *
 * Why these commands work at all: a console session runs `$SHELL -i`
 * (config.py AgentsConfig.shell_command), so ~/.zshrc is sourced and
 * Homebrew's PATH, `claude`, and the user's own functions resolve. Under
 * a non-interactive shell none of them would be found.
 *
 * Persistence is a WHOLE-LIST replace (PUT /terminal/commands) because
 * add, edit, delete and reorder are all just "the list is now this" -
 * one endpoint, one validation gate, and the stored order can never
 * disagree with itself. Like AgentWrappersPanel, edits here write
 * immediately rather than joining the panel's batched Save.
 *
 * Loads AFTER api.js and BEFORE settings-panel.js mounts it.
 */
(function () {
    'use strict';

    var commands = [];
    var rootEl = null;
    var editingId = null; // id being edited, '__new__' for a blank row, or null

    /**
     * Escape a string for safe interpolation into innerHTML-built markup.
     * Inputs: str (any). Output: string.
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    /**
     * Derive a valid id from a label, for a newly added entry.
     * Description: lowercases, collapses everything outside [a-z0-9] to a
     *   single hyphen, trims stray hyphens, and de-duplicates against the
     *   current list. Must satisfy the server's
     *   TERMINAL_COMMAND_ID_PATTERN (^[a-z0-9][a-z0-9_-]{0,63}$).
     * Inputs: label (string) - the user-typed label.
     * Output: string - a unique, valid id ('cmd' when nothing survives).
     */
    function slugify(label) {
        var base = String(label || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 64);
        if (!base || !/^[a-z0-9]/.test(base)) base = 'cmd' + (base ? '-' + base : '');
        base = base.slice(0, 64);
        var candidate = base;
        var n = 2;
        while (commands.some(function (c) { return c.id === candidate; })) {
            candidate = base.slice(0, 60) + '-' + n;
            n += 1;
        }
        return candidate;
    }

    /**
     * Render one command's summary row (or its inline editor when open).
     * Inputs: cmd (object) - {id, label, command}; index (number) - its
     *   position, used by the move-up/move-down buttons.
     * Output: string - HTML.
     */
    function renderRow(cmd, index) {
        if (editingId === cmd.id) return renderEditor(cmd);
        var id = escapeHtml(cmd.id);
        return (
            '<div class="settings-command-row" data-command-id="' + id + '">' +
            '  <div class="settings-command-row-main">' +
            '    <strong>' + escapeHtml(cmd.label) + '</strong>' +
            '    <div class="settings-command-line"><code>' + escapeHtml(cmd.command) + '</code></div>' +
            '  </div>' +
            '  <div class="settings-command-row-actions">' +
            '    <button type="button" class="modal-btn modal-btn-primary" data-command-action="run" data-command-id="' + id + '">run</button>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" data-command-action="edit" data-command-id="' + id + '">edit</button>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" data-command-action="up" data-command-index="' + index + '"' +
            (index === 0 ? ' disabled' : '') + ' aria-label="move up" title="move up">^</button>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" data-command-action="down" data-command-index="' + index + '"' +
            (index === commands.length - 1 ? ' disabled' : '') + ' aria-label="move down" title="move down">v</button>' +
            '    <button type="button" class="modal-btn modal-btn-danger" data-command-action="delete" data-command-id="' + id + '">delete</button>' +
            '  </div>' +
            '</div>'
        );
    }

    /**
     * Render the inline add/edit form.
     * Inputs: cmd (object|null) - the entry being edited, null when adding.
     * Output: string - HTML.
     */
    function renderEditor(cmd) {
        var c = cmd || { label: '', command: '' };
        return (
            '<div class="settings-command-editor">' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="command-field-label">label</label>' +
            '    <input type="text" id="command-field-label" class="modal-input" placeholder="e.g. update claude" value="' + escapeHtml(c.label) + '">' +
            '  </div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="command-field-command">command</label>' +
            '    <input type="text" id="command-field-command" class="modal-input" spellcheck="false" autocapitalize="off" autocorrect="off"' +
            '      placeholder="e.g. brew upgrade --cask claude-code" value="' + escapeHtml(c.command) + '">' +
            '    <div class="settings-field-hint">typed into a console session running your interactive shell, so anything in your ~/.zshrc resolves.</div>' +
            '  </div>' +
            '  <div class="settings-command-editor-actions">' +
            '    <span id="command-editor-status" class="settings-save-status"></span>' +
            '    <button type="button" class="modal-btn modal-btn-secondary" id="command-editor-cancel">cancel</button>' +
            '    <button type="button" class="modal-btn modal-btn-primary" id="command-editor-save">save</button>' +
            '  </div>' +
            '</div>'
        );
    }

    /**
     * Render the whole terminal-commands section.
     * Output: string - HTML for the `.settings-section`.
     */
    function renderSection() {
        var rows = commands.map(renderRow).join('');
        var newEditor = editingId === '__new__' ? renderEditor(null) : '';
        var addBtn = editingId
            ? ''
            : '<div class="settings-command-actions-row"><button type="button" class="modal-btn modal-btn-secondary" id="command-add-btn">+ add command</button></div>';
        return (
            '<section class="settings-section" data-settings-section="terminal-commands">' +
            '  <h3 class="settings-section-title">terminal commands</h3>' +
            '  <div class="settings-section-description">' +
            '    common shell commands. "run" opens a new console session and types the command into it ' +
            '    so you can watch it and stop it. edits save immediately.' +
            '  </div>' +
            '  <div class="settings-command-list" id="command-list">' +
            (rows || '<div class="settings-field-hint">no commands yet.</div>') +
            '  </div>' +
            newEditor +
            addBtn +
            '  <div id="command-panel-status" class="settings-save-status" role="status" aria-live="polite"></div>' +
            '</section>'
        );
    }

    /**
     * Re-render the section in place and re-wire its handlers.
     * Output: void.
     */
    function rerender() {
        if (!rootEl) return;
        var replacement = document.createElement('div');
        replacement.innerHTML = renderSection();
        var newRoot = replacement.firstElementChild;
        rootEl.parentNode.replaceChild(newRoot, rootEl);
        rootEl = newRoot;
        wire();
    }

    /**
     * Persist the current in-memory list and re-render from the server's
     * authoritative response.
     * Inputs: next (Array<object>) - the complete new list, in order.
     * Output: Promise<boolean> - true on success.
     */
    async function persist(next) {
        try {
            var result = await window.API.replaceTerminalCommands(next);
            commands = result.commands || [];
            editingId = null;
            rerender();
            return true;
        } catch (err) {
            console.error('TerminalCommandsPanel: save failed', err);
            var statusEl = document.getElementById('command-editor-status')
                || document.getElementById('command-panel-status');
            if (statusEl) statusEl.textContent = 'save failed: ' + (err && err.message ? err.message : 'unknown error');
            return false;
        }
    }

    /**
     * Save the open editor (add or update, depending on ``editingId``).
     * Output: Promise<void>.
     */
    async function saveEditor() {
        var labelEl = document.getElementById('command-field-label');
        var commandEl = document.getElementById('command-field-command');
        var statusEl = document.getElementById('command-editor-status');
        var label = labelEl ? labelEl.value.trim() : '';
        var command = commandEl ? commandEl.value.trim() : '';
        if (!label || !command) {
            if (statusEl) statusEl.textContent = 'label and command are both required';
            return;
        }
        var next;
        if (editingId === '__new__') {
            next = commands.concat([{ id: slugify(label), label: label, command: command }]);
        } else {
            next = commands.map(function (c) {
                return c.id === editingId ? { id: c.id, label: label, command: command } : c;
            });
        }
        if (statusEl) statusEl.textContent = 'saving...';
        await persist(next);
    }

    /**
     * Delete one entry after a confirm.
     * Inputs: id (string). Output: Promise<void>.
     */
    async function deleteCommand(id) {
        var target = commands.find(function (c) { return c.id === id; });
        if (!target) return;
        if (!window.confirm('delete "' + target.label + '"?')) return;
        await persist(commands.filter(function (c) { return c.id !== id; }));
    }

    /**
     * Move one entry up or down and persist the new order.
     * Inputs: index (number) - current position; delta (number) - -1 or +1.
     * Output: Promise<void>.
     */
    async function moveCommand(index, delta) {
        var target = index + delta;
        if (target < 0 || target >= commands.length) return;
        var next = commands.slice();
        var moved = next.splice(index, 1)[0];
        next.splice(target, 0, moved);
        await persist(next);
    }

    /**
     * Run one entry: close the settings modal, then open a new console
     * session that runs it. Only the id is sent - see this module's
     * docstring.
     * Inputs: id (string). Output: Promise<void>.
     */
    async function runCommand(id) {
        var target = commands.find(function (c) { return c.id === id; });
        if (!target) return;
        if (window.SettingsPanel && typeof window.SettingsPanel.close === 'function') {
            window.SettingsPanel.close();
        }
        if (!window.Launchpad || typeof window.Launchpad.createConsoleSession !== 'function') {
            console.error('TerminalCommandsPanel: launchpad unavailable');
            return;
        }
        await window.Launchpad.createConsoleSession({ terminalCommandId: target.id });
    }

    /**
     * Wire handlers for the currently-rendered section.
     * Output: void.
     */
    function wire() {
        if (!rootEl) return;

        var addBtn = rootEl.querySelector('#command-add-btn');
        if (addBtn) addBtn.addEventListener('click', function () { editingId = '__new__'; rerender(); });

        var cancelBtn = rootEl.querySelector('#command-editor-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', function () { editingId = null; rerender(); });

        var saveBtn = rootEl.querySelector('#command-editor-save');
        if (saveBtn) saveBtn.addEventListener('click', saveEditor);

        rootEl.querySelectorAll('[data-command-action]').forEach(function (btn) {
            var action = btn.getAttribute('data-command-action');
            var id = btn.getAttribute('data-command-id');
            var index = parseInt(btn.getAttribute('data-command-index'), 10);
            if (action === 'run') {
                btn.addEventListener('click', function () { runCommand(id); });
            } else if (action === 'edit') {
                btn.addEventListener('click', function () { editingId = id; rerender(); });
            } else if (action === 'delete') {
                btn.addEventListener('click', function () { deleteCommand(id); });
            } else if (action === 'up') {
                btn.addEventListener('click', function () { moveCommand(index, -1); });
            } else if (action === 'down') {
                btn.addEventListener('click', function () { moveCommand(index, 1); });
            }
        });
    }

    /**
     * Mount the section into a parent element, seeding from a list the
     * caller may already have (the settings summary includes it, so the
     * common path costs no extra round trip).
     * Inputs:
     *   parentEl (Element) - container to append into.
     *   seed (Array<object>|null) - pre-fetched list, or null to fetch.
     * Output: Promise<void>.
     */
    async function mount(parentEl, seed) {
        editingId = null;
        if (Array.isArray(seed)) {
            commands = seed;
        } else {
            try {
                var resp = await window.API.getTerminalCommands();
                commands = resp.commands || [];
            } catch (err) {
                console.error('TerminalCommandsPanel: failed to load commands', err);
                commands = [];
            }
        }
        var holder = document.createElement('div');
        holder.innerHTML = renderSection();
        rootEl = holder.firstElementChild;
        parentEl.appendChild(rootEl);
        wire();
    }

    window.TerminalCommandsPanel = { mount: mount };
})();
