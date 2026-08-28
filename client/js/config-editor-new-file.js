/**
 * "new file" prompt for the file editor.
 *
 * The third module of the file-editor feature, alongside
 * config-editor-panel.js (the tree) and config-editor-modal.js (the
 * editor); split out for the project's 500-line file rule.
 *
 * WHY A PROMPT AND NOT A PER-DIRECTORY "+": every tree row is already a
 * real <button>, and a button cannot contain another button. Putting a
 * create control on each directory row would mean restructuring every row
 * into a wrapper plus two buttons. This asks for the root and the path
 * instead, pre-selecting the roots the panel is actually showing.
 *
 * PATH SAFETY IS SERVER-SIDE, and deliberately so. The client does the
 * bare minimum sanity check (non-empty) and otherwise SHOWS THE SERVER'S
 * REJECTION VERBATIM. `..`, absolute paths, hidden entries, a name that
 * already exists, a missing parent directory and the read-only root are
 * all decided by config_files.resolve_safe_path / create_file, which is
 * the same guard the read and write paths use. Duplicating those rules
 * here would create a second, drifting copy of a security boundary.
 *
 * Directory creation is NOT offered: the server refuses it and says so,
 * and this prompt does not pretend otherwise.
 *
 * Must load AFTER api.js (window.API) and modal-stack.js
 * (window.ModalStack), and BEFORE config-editor-panel.js, which opens it.
 */

console.log('[ConfigEditorNewFile Module] Loading...');

(function () {
    let overlayEl = null;

    /**
     * HTML-escape a string for safe interpolation into innerHTML.
     * Inputs: value (any). Output: string.
     */
    function esc(value) {
        const div = document.createElement('div');
        div.textContent = String(value == null ? '' : value);
        return div.innerHTML;
    }

    /**
     * Tear the prompt down and unregister it from the modal stack.
     * Inputs: none. Output: void.
     */
    function close() {
        if (!overlayEl) return;
        window.ModalStack.pop(overlayEl);
        if (overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
        overlayEl = null;
    }

    /**
     * Show one error line inside the open prompt. Never a toast and never
     * a silent no-op: a create that did not happen has to say why, next to
     * the field the user would have to change.
     * Inputs: message (string). Output: void.
     */
    function showError(message) {
        const el = overlayEl && overlayEl.querySelector('#config-editor-new-error');
        if (!el) return;
        el.textContent = message;
        el.hidden = false;
    }

    /**
     * Build the prompt's markup.
     * Inputs: roots (Array<{id, label}>) - the roots currently browsable.
     * Output: string - HTML.
     */
    function promptHtml(roots) {
        const options = roots.map(
            r => `<option value="${esc(r.id)}">${esc(r.label)}</option>`,
        ).join('');
        return (
            '<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="config-editor-new-title">' +
            '  <div class="modal-header" id="config-editor-new-title">» new file</div>' +
            '  <div class="modal-body">' +
            '    <div class="config-editor-new-form">' +
            '      <label for="config-editor-new-root">where</label>' +
            `      <select id="config-editor-new-root">${options}</select>` +
            '      <label for="config-editor-new-path">path, relative to that root</label>' +
            '      <input type="text" id="config-editor-new-path" placeholder="notes.md" ' +
            '             autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" />' +
            '      <div class="config-editor-new-hint">the directory must already exist. this does not create folders.</div>' +
            '      <div id="config-editor-new-error" class="config-editor-error" hidden></div>' +
            '    </div>' +
            '  </div>' +
            '  <div class="modal-footer">' +
            '    <button type="button" class="modal-btn modal-btn-secondary" id="config-editor-new-cancel">cancel</button>' +
            '    <button type="button" class="modal-btn modal-btn-primary" id="config-editor-new-create">create</button>' +
            '  </div>' +
            '</div>'
        );
    }

    /**
     * Open the prompt and resolve once the user has either created a file
     * or backed out.
     * Inputs:
     *   roots (Array<{id, label}>) - roots to offer, in display order.
     *   projectPath (string|null) - the active session's working
     *     directory; required by the server for any root but "user".
     * Output: Promise<{root: string, path: string}|null> - the created
     *   file, or null when the user cancelled or dismissed.
     */
    function open(roots, projectPath) {
        return new Promise((resolve) => {
            close();
            overlayEl = document.createElement('div');
            overlayEl.className = 'modal-overlay';
            overlayEl.setAttribute('data-modal', 'config-new-file');
            overlayEl.innerHTML = promptHtml(roots);
            document.body.appendChild(overlayEl);

            const finish = (value) => { close(); resolve(value); };
            window.ModalStack.push(overlayEl, { onEscape: () => finish(null) });

            const pathInput = overlayEl.querySelector('#config-editor-new-path');
            const rootSelect = overlayEl.querySelector('#config-editor-new-root');
            const createBtn = overlayEl.querySelector('#config-editor-new-create');

            const submit = async () => {
                const root = rootSelect.value;
                const path = (pathInput.value || '').trim();
                if (!path) {
                    showError('a file name is required');
                    return;
                }
                createBtn.disabled = true;
                try {
                    await window.API.createConfigFile({
                        root,
                        path,
                        content: '',
                        project_path: root === 'user' ? null : projectPath,
                    });
                    finish({ root, path });
                } catch (err) {
                    // The server's own wording, verbatim - it already names
                    // the exact reason (exists / escapes the root / missing
                    // parent / read-only / needs confirmation).
                    showError(err.message || String(err));
                    createBtn.disabled = false;
                }
            };

            createBtn.addEventListener('click', submit);
            overlayEl.querySelector('#config-editor-new-cancel')
                .addEventListener('click', () => finish(null));
            overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) finish(null); });
            pathInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); submit(); }
            });

            setTimeout(() => { try { pathInput.focus(); } catch (_) { /* no-op */ } }, 50);
        });
    }

    window.ConfigEditorNewFile = { open, close };
    console.log('[ConfigEditorNewFile Module] Exported as window.ConfigEditorNewFile');
})();
