/**
 * Config/project editor modal - the file-editing half of the config
 * editor feature (see client/js/config-editor-panel.js for the tree half
 * that opens it). Split into its own module purely for the project's
 * 500-line file-size rule; the two files together are one feature and
 * load back to back.
 *
 * Follows SettingsPanel's dismissal contract (client/js/settings-
 * panel.js): a `.modal-overlay` appended to document.body on open,
 * removed on close, close-button/backdrop-click/Escape all dismiss - with
 * two additions. First, dismissal is gated behind an unsaved-changes
 * check (confirmDiscardIfDirty) via App.showConfirmModal, so a stray
 * Escape, backdrop click or BACK tap can never silently drop an edit.
 * Second, it registers with client/js/modal-stack.js, so when it opens
 * over the file picker it becomes the SECOND modal: Escape reaches only
 * this one, the picker is dimmed (desktop) or taken off screen (phone),
 * a "back to files" control appears, and focus returns to the picker row
 * that opened it on close.
 *
 * Editing surface: vendored CodeMirror 6 (window.CodeMirrorBundle, see
 * client/vendor/codemirror/VERSION.md), never a CDN load - this app's CSP
 * is script-src 'self'. Markdown files get an edit/preview toggle;
 * preview renders through window.MarkdownLite
 * (client/js/markdown-lite.js), never raw innerHTML from file content.
 *
 * Sensitive files (server-flagged `is_sensitive` - credentials/secret/
 * key-shaped, see config_files.py's SENSITIVE_*) open MASKED: the editor
 * is covered by a placeholder until the user explicitly reveals via
 * App.showConfirmModal, and a WRITE to one additionally requires that
 * same confirmation (acknowledge_sensitive), mirroring the executable-
 * write gate. This is a screen/screenshot/screen-share guard, not access
 * control - the content is already in this module's memory once read;
 * see config_files.py's module docstring for the full reasoning.
 *
 * Must load AFTER api.js (window.API), markdown-lite.js
 * (window.MarkdownLite), the vendored CodeMirror bundle
 * (window.CodeMirrorBundle) and BEFORE config-editor-panel.js, which
 * calls into window.ConfigEditorModal.open().
 */

console.log('[ConfigEditorModal Module] Loading...');

(function () {
    // { root, path, projectPath, isExecutable, isSensitive, readOnly,
    //   revealed, originalContent, dirty, mode: 'edit'|'preview',
    //   cmEditor: {getValue,setContent,setReadOnly,focus,destroy}|null } | null
    let activeFile = null;
    let overlayEl = null;

    /**
     * True when a file is markdown, purely by extension - drives whether
     * the edit/preview toggle appears.
     * Inputs: path (string). Output: bool.
     */
    function isMarkdown(path) {
        return /\.md$/i.test(path || '');
    }

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
     * True while a file is open with edits not yet saved. The one
     * question every dismissal path (this module's own close handlers,
     * and ConfigEditorPanel's panel-level close) must ask before
     * discarding state.
     * Inputs: none. Output: bool.
     */
    function isDirty() {
        return !!(activeFile && activeFile.dirty);
    }

    /**
     * Ask the user before discarding unsaved edits. A no-op (resolves
     * true immediately) when there is nothing dirty to lose.
     * Inputs: none. Output: Promise<bool> - true if it is safe to
     *   proceed (either nothing was dirty, or the user confirmed
     *   discarding it).
     */
    async function confirmDiscardIfDirty() {
        if (!isDirty()) return true;
        return window.App.showConfirmModal(
            'discard unsaved changes?',
            `"${activeFile.path}" has unsaved edits.`,
            'closing now will lose them. this cannot be undone.',
            'discard',
            'keep editing'
        );
    }

    /**
     * Close the modal without asking - callers that already know it is
     * safe (a confirmed discard, or a successful save-and-close) use
     * this directly. Every USER-facing dismissal gesture must go through
     * closeGuarded() instead.
     * Inputs: none. Output: void.
     */
    function close() {
        if (activeFile && activeFile.cmEditor) {
            try { activeFile.cmEditor.destroy(); } catch (_) { /* no-op */ }
        }
        if (overlayEl) {
            // Pop BEFORE detaching: ModalStack restores focus to whatever
            // had it when this modal opened (the picker row the user
            // clicked), and it can only do that while that element is
            // still the sensible target - i.e. before the picker beneath
            // is un-covered and re-rendered by anything else.
            window.ModalStack.pop(overlayEl);
            if (overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
        }
        overlayEl = null;
        activeFile = null;
    }

    /**
     * Dismissal entry point for the modal's close button, backdrop
     * click, and Escape - the one path every "make the modal go away"
     * gesture routes through, so unsaved changes are protected
     * identically no matter which gesture triggered it.
     * Inputs: none. Output: Promise<void>.
     */
    async function closeGuarded() {
        if (!(await confirmDiscardIfDirty())) return;
        close();
    }

    /**
     * Open the editor for one file: builds the modal shell immediately
     * (loading state), then fetches content and renders the real editor.
     * A sensitive file (server-flagged `is_sensitive`) opens MASKED
     * regardless of what the caller might otherwise assume - `revealed`
     * starts false and only App.showConfirmModal can flip it.
     * Inputs: rootId (string) - "user"|"project"|"workdir"; relPath
     *   (string); readOnly (bool) - from the tree node's data attribute;
     *   combined with the server's own read_only flag in the response.
     *   projectPath (string|null) - required when rootId != "user".
     * Output: Promise<void>.
     */
    async function open(rootId, relPath, readOnly, projectPath) {
        if (!(await confirmDiscardIfDirty())) return;
        close();
        buildShell(rootId, relPath, projectPath);
        try {
            const result = await window.API.readConfigFile(rootId, relPath, projectPath);
            activeFile = {
                root: rootId,
                path: relPath,
                projectPath,
                isExecutable: result.is_executable,
                isSensitive: result.is_sensitive,
                readOnly: readOnly || result.read_only,
                revealed: !result.is_sensitive,
                originalContent: result.content,
                dirty: false,
                mode: 'edit',
                cmEditor: null,
            };
            renderBody();
        } catch (err) {
            const body = document.getElementById('config-editor-modal-body');
            if (body) body.innerHTML = `<div class="config-editor-error">failed to open: ${esc(err.message || err)}</div>`;
        }
    }

    /**
     * Create the modal DOM shell and append it to document.body. Content
     * is filled in afterward by renderBody() so a loading state shows
     * immediately for a slow read.
     *
     * The shell is FULL SCREEN (config-editor-modal.css) - it used to be a
     * 900px card floating over the file drawer. Nothing about the
     * dismissal contract changes with it: close, back, backdrop and
     * Escape all still route through closeGuarded(), which is still gated
     * behind confirmDiscardIfDirty().
     *
     * Inputs: rootId (string); relPath (string); projectPath (string|null).
     * Output: void.
     */
    function buildShell(rootId, relPath, projectPath) {
        // Whether this editor is the SECOND modal in the stack, i.e. the
        // picker is open beneath it. That is what earns the "back"
        // control - with no picker behind, back would have nowhere to go
        // and would just be a second, differently-worded close button.
        // Now that the editor is full screen the drawer behind is covered
        // at EVERY width, not only on a phone, so the control is no longer
        // hidden by a breakpoint.
        const stacked = window.ModalStack.depth() > 0;
        const backBtn = stacked
            ? '<button type="button" class="config-editor-modal-back" id="config-editor-modal-back" ' +
              'aria-label="Back to files" title="back to files">' +
              '<span aria-hidden="true">&larr;</span> files</button>'
            : '';

        overlayEl = document.createElement('div');
        // The extra class is what config-editor-modal.css hangs the
        // full-screen treatment off. `.modal-overlay` alone is shared with
        // the settings panel and the confirm dialogs, which are still
        // centred cards and must stay that way.
        overlayEl.className = 'modal-overlay config-editor-modal-overlay';
        overlayEl.setAttribute('data-modal', 'config-editor');
        overlayEl.innerHTML = (
            '<div class="config-editor-modal-content" role="dialog" aria-modal="true" aria-labelledby="config-editor-modal-title">' +
            '  <div class="modal-header config-editor-modal-header">' +
            `    <div>${backBtn}<span id="config-editor-modal-title" class="config-editor-modal-title">${esc(relPath)}</span>` +
            `    <div class="config-editor-modal-subtitle">${esc(rootId)} root${projectPath ? ` · ${esc(projectPath)}` : ''}</div></div>` +
            '    <button type="button" class="modal-close" id="config-editor-modal-close" aria-label="Close editor" title="close">&times;</button>' +
            '  </div>' +
            '  <div class="modal-body config-editor-modal-body" id="config-editor-modal-body"><div class="config-editor-loading">loading...</div></div>' +
            '  <div class="modal-footer config-editor-modal-footer" id="config-editor-modal-footer" hidden></div>' +
            '</div>'
        );
        document.body.appendChild(overlayEl);
        // Registering makes this the TOP of the stack: Escape now routes
        // here and nowhere else, and the picker underneath is marked
        // covered (dimmed on desktop, off screen on a phone).
        window.ModalStack.push(overlayEl, { onEscape: () => closeGuarded() });

        overlayEl.querySelector('#config-editor-modal-close').addEventListener('click', () => closeGuarded());
        // Back and close are the SAME operation - both dismiss the editor
        // and reveal the picker underneath, and both go through
        // closeGuarded() so backing out of an unsaved edit warns rather
        // than discards. They differ only in what they say they do, which
        // is what a phone user needs to see.
        if (stacked) {
            overlayEl.querySelector('#config-editor-modal-back')
                .addEventListener('click', () => closeGuarded());
        }
        overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeGuarded(); });

        setTimeout(() => {
            const closeBtn = overlayEl.querySelector('#config-editor-modal-close');
            try { closeBtn.focus(); } catch (_) { /* no-op */ }
        }, 50);
    }

    /**
     * Render the modal body + footer for the currently-loaded
     * `activeFile`: CodeMirror editor (or a masking placeholder for an
     * unrevealed sensitive file), exec/sensitive/read-only warnings,
     * edit/preview toggle for markdown, and save/cancel actions.
     * Inputs: none. Output: void.
     */
    function renderBody() {
        const f = activeFile;
        const body = document.getElementById('config-editor-modal-body');
        const footer = document.getElementById('config-editor-modal-footer');
        if (!body || !footer) return;

        const execNote = f.isExecutable
            ? '<div class="config-editor-exec-warning">this file is code claude code runs automatically. saving requires confirmation and is always backed up first.</div>'
            : '';
        const sensitiveNote = f.isSensitive
            ? '<div class="config-editor-exec-warning">this file looks like credentials, a secret, or a private key. its contents are masked until you reveal them, and saving requires confirmation.</div>'
            : '';
        const roNote = f.readOnly ? '<div class="config-editor-ro-warning">read-only - this file is under a read-only root.</div>' : '';
        const isMd = isMarkdown(f.path);
        const toggleHtml = isMd
            ? `<div class="config-editor-mode-toggle" role="tablist" aria-label="editor mode">
                 <button type="button" role="tab" id="config-editor-mode-edit" aria-selected="${f.mode === 'edit'}">edit</button>
                 <button type="button" role="tab" id="config-editor-mode-preview" aria-selected="${f.mode === 'preview'}">preview</button>
               </div>`
            : '';

        body.innerHTML = (
            `${execNote}${sensitiveNote}${roNote}${toggleHtml}` +
            '<div id="config-editor-save-error" class="config-editor-error" hidden></div>' +
            (f.isSensitive && !f.revealed ? maskHtml() : '') +
            `<div id="config-editor-cm-host" class="config-editor-cm-host" data-readonly="${f.readOnly ? '1' : '0'}" ${f.isSensitive && !f.revealed ? 'hidden' : ''}></div>` +
            `<div id="config-editor-preview" class="config-editor-preview" hidden></div>`
        );

        const saveDisabled = f.readOnly || (f.isSensitive && !f.revealed);
        footer.hidden = false;
        footer.innerHTML = (
            '<span id="config-editor-dirty-flag" class="config-editor-dirty-flag"></span>' +
            '<button type="button" class="modal-btn modal-btn-secondary" id="config-editor-cancel">cancel</button>' +
            `<button type="button" class="modal-btn modal-btn-primary" id="config-editor-save" ${saveDisabled ? 'disabled' : ''}>save</button>`
        );

        wireBody(f, isMd);
    }

    /**
     * Build the mask placeholder shown in place of the editor for an
     * unrevealed sensitive file - a padlock, a plain statement of why,
     * and the one button that starts the reveal flow.
     * Inputs: none. Output: string - HTML.
     */
    function maskHtml() {
        return (
            '<div id="config-editor-sensitive-mask" class="config-editor-sensitive-mask">' +
            window.SessionStatusUI.lockIconSvg() +
            '<div class="config-editor-sensitive-mask-text">this file looks like credentials, a secret, or a private key. ' +
            'its contents are hidden by default so they do not end up on screen in a screenshot or screen-share.</div>' +
            '<button type="button" class="modal-btn modal-btn-secondary" id="config-editor-reveal">reveal contents</button>' +
            '</div>'
        );
    }

    /**
     * Handle the "reveal contents" button: routes through
     * App.showConfirmModal with copy that plainly states the file holds
     * credentials and is about to be displayed on screen - a deliberate,
     * named step, not a toggle to brush past. On confirm, mounts the
     * CodeMirror editor with the real content.
     * Inputs: none. Output: Promise<void>.
     */
    async function revealSensitive() {
        const f = activeFile;
        if (!f) return;
        const confirmed = await window.App.showConfirmModal(
            'show credentials on screen?',
            `"${f.path}" contains credentials, a secret, or a private key.`,
            'its contents will be displayed on screen and stay visible until you close this editor. anyone who can see or record your screen will be able to read them.',
            'reveal',
            'keep hidden'
        );
        if (!confirmed || !activeFile) return;
        activeFile.revealed = true;
        const mask = document.getElementById('config-editor-sensitive-mask');
        if (mask) mask.remove();
        const host = document.getElementById('config-editor-cm-host');
        if (host) host.hidden = false;
        mountEditor(activeFile);
        const saveBtn = document.getElementById('config-editor-save');
        if (saveBtn && !activeFile.readOnly) saveBtn.disabled = false;
    }

    /**
     * Mount (or re-mount) the CodeMirror editor into #config-editor-cm-
     * host for the currently-loaded, revealed `activeFile`. No-op for an
     * unrevealed sensitive file - callers must go through
     * revealSensitive() first.
     * Inputs: f (object) - activeFile. Output: void.
     */
    function mountEditor(f) {
        const host = document.getElementById('config-editor-cm-host');
        if (!host || (f.isSensitive && !f.revealed)) return;
        if (f.cmEditor) {
            try { f.cmEditor.destroy(); } catch (_) { /* no-op */ }
        }
        f.cmEditor = window.CodeMirrorBundle.createEditor(
            host,
            f.originalContent,
            f.path,
            f.readOnly,
            (newValue) => {
                f.dirty = newValue !== f.originalContent;
                updateDirtyFlag();
            }
        );
    }

    /**
     * Wire the event handlers for the just-rendered body/footer (cancel,
     * save, mode toggle, sensitive-reveal), and mount the editor itself.
     * Inputs: f (object) - activeFile; isMd (bool).
     * Output: void.
     */
    function wireBody(f, isMd) {
        if (!f.isSensitive || f.revealed) {
            mountEditor(f);
        } else {
            const revealBtn = document.getElementById('config-editor-reveal');
            if (revealBtn) revealBtn.addEventListener('click', () => revealSensitive());
        }

        document.getElementById('config-editor-cancel').addEventListener('click', () => closeGuarded());
        if (!f.readOnly) {
            document.getElementById('config-editor-save').addEventListener('click', () => save());
        }
        if (isMd) {
            document.getElementById('config-editor-mode-edit').addEventListener('click', () => setMode('edit'));
            document.getElementById('config-editor-mode-preview').addEventListener('click', () => setMode('preview'));
        }
        updateDirtyFlag();
    }

    /**
     * Switch between edit/preview mode for a markdown file. Preview
     * renders the CURRENT (possibly unsaved) editor content through
     * MarkdownLite - never innerHTML from the file directly. A masked
     * sensitive file has no content to preview yet, so preview is a
     * no-op until revealed.
     * Inputs: mode ('edit'|'preview'). Output: void.
     */
    function setMode(mode) {
        if (!activeFile) return;
        if (activeFile.isSensitive && !activeFile.revealed) return;
        const host = document.getElementById('config-editor-cm-host');
        activeFile.mode = mode;
        const editTab = document.getElementById('config-editor-mode-edit');
        const previewTab = document.getElementById('config-editor-mode-preview');
        const preview = document.getElementById('config-editor-preview');
        if (mode === 'preview') {
            const content = activeFile.cmEditor ? activeFile.cmEditor.getValue() : activeFile.originalContent;
            preview.innerHTML = window.MarkdownLite.render(content);
            host.hidden = true;
            preview.hidden = false;
        } else {
            host.hidden = false;
            preview.hidden = true;
        }
        editTab.setAttribute('aria-selected', String(mode === 'edit'));
        previewTab.setAttribute('aria-selected', String(mode === 'preview'));
    }

    /**
     * Reflect `activeFile.dirty` into the footer's status text.
     * Inputs: none. Output: void.
     */
    function updateDirtyFlag() {
        const el = document.getElementById('config-editor-dirty-flag');
        if (!el || !activeFile) return;
        el.textContent = activeFile.dirty ? 'unsaved changes' : '';
    }

    /**
     * Save the editor's current content, enforcing the same confirm-
     * before-write flow as every other destructive action in the app
     * (App.showConfirmModal) - once for an executable write, once for a
     * sensitive write (a file can be both; both confirms fire). Client-
     * side JSON pre-validation is a fast-fail UX nicety only - the server
     * re-validates and is the real enforcement point
     * (config_files.write_file). Content is read from the CodeMirror
     * instance directly, preserving exact whitespace/indentation - never
     * transformed or re-serialized by this module.
     * Inputs: none. Output: Promise<void>.
     */
    async function save() {
        const f = activeFile;
        if (!f || !f.cmEditor) return;
        const errorEl = document.getElementById('config-editor-save-error');
        errorEl.hidden = true;
        const content = f.cmEditor.getValue();

        if (f.path.endsWith('.json')) {
            try {
                JSON.parse(content);
            } catch (err) {
                errorEl.hidden = false;
                errorEl.classList.remove('config-editor-success');
                errorEl.textContent = `invalid json - not saved: ${err.message}`;
                return;
            }
        }

        let acknowledgeExecutable = false;
        if (f.isExecutable) {
            const confirmed = await window.App.showConfirmModal(
                'save executable file?',
                `"${f.path}" is code claude code runs automatically.`,
                'a backup of the current version is made before saving. this cannot be undone.',
                'save',
                'cancel'
            );
            if (!confirmed) return;
            acknowledgeExecutable = true;
        }

        let acknowledgeSensitive = false;
        if (f.isSensitive) {
            const confirmed = await window.App.showConfirmModal(
                'save credentials file?',
                `"${f.path}" contains credentials, a secret, or a private key.`,
                'a backup of the current version is made before saving. this cannot be undone.',
                'save',
                'cancel'
            );
            if (!confirmed) return;
            acknowledgeSensitive = true;
        }

        try {
            const result = await window.API.writeConfigFile({
                root: f.root,
                path: f.path,
                content,
                project_path: f.projectPath,
                acknowledge_executable: acknowledgeExecutable,
                acknowledge_sensitive: acknowledgeSensitive,
            });
            f.originalContent = content;
            f.dirty = false;
            updateDirtyFlag();
            errorEl.hidden = false;
            errorEl.classList.add('config-editor-success');
            errorEl.textContent = result.backed_up ? 'saved (previous version backed up to .bak)' : 'saved';
        } catch (err) {
            errorEl.hidden = false;
            errorEl.classList.remove('config-editor-success');
            errorEl.textContent = `save failed: ${err.message || err}`;
        }
    }

    window.ConfigEditorModal = { open, close, isDirty, confirmDiscardIfDirty };
    console.log('[ConfigEditorModal Module] Exported as window.ConfigEditorModal');
})();
