/**
 * Project Create Folder - the folder step "start empty" never had.
 *
 * WHY THIS EXISTS. The new-project chain was: "+" > new claude project >
 * start empty > provider > name this project > create session. No folder
 * was ever asked for, so the server had nothing to go on and every
 * project landed at `<projects root>/ses_<hex>` - a random session id,
 * recorded in `sessions.working_dir` as the project's permanent home.
 * This inserts one step after the name: choose the PARENT folder, see the
 * full path that will be created, then create.
 *
 * DELIBERATELY NOT IN launchpad.js. That file is past 5,000 lines against
 * this project's 500-line standard and is on the do-not-grow list. It
 * gains a call site and nothing else.
 *
 * The two functions that matter are PURE and DOM-free - `validateName`
 * and `composePath` - so the rules they enforce are testable without a
 * browser (tests/test_project_create_folder.node.mjs). They mirror
 * src/core/project_directory.py, which is the authority: the client check
 * exists to give an answer without a round-trip, never to be the only
 * check. A client that skipped it would still be refused by the server.
 *
 * The default parent comes from the SERVER: `window.API.browseDirectory()`
 * with no argument returns the configured projects root. No copy of that
 * path lives here.
 *
 * Load order: BEFORE launchpad.js, and it uses window.FolderPickerModal,
 * so after folder-picker-modal.js.
 */

console.log('[ProjectCreateFolder Module] Loading...');

(function () {
    /**
     * Characters that can never appear in a project folder name: the path
     * separator, the other platform's separator, and NUL (which
     * terminates the string the kernel actually receives). Spaces are
     * absent from this list ON PURPOSE - real project names have them.
     */
    const ILLEGAL_CHARS = ['/', '\\', '\u0000'];

    /** Longest single path component macOS accepts, in bytes (NAME_MAX). */
    const MAX_NAME_BYTES = 255;

    /**
     * Description: escape text for safe insertion into innerHTML.
     * Inputs: s (any) - the value to escape.
     * Output: string - HTML-safe text.
     * Example: escapeHtml('<b>') === '&lt;b&gt;'
     */
    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Description: check a project name is usable as one path component.
     *   It REJECTS rather than rewrites: silently turning "a/b" into
     *   "a-b" would create a folder the user did not ask for and cannot
     *   find later. Spaces are legal, because real project names have
     *   them.
     * Inputs: name (string) - the project name exactly as typed.
     * Output: {ok: boolean, message: string|null} - message is a
     *   lowercase sentence to show inline when ok is false.
     * Example: validateName('a/b').message
     *          === "a project name cannot contain '/'"
     */
    function validateName(name) {
        const raw = typeof name === 'string' ? name : '';
        const trimmed = raw.trim();

        if (!trimmed) {
            return { ok: false, message: 'a project name is required' };
        }
        for (const char of ILLEGAL_CHARS) {
            if (trimmed.indexOf(char) !== -1) {
                const shown = char === '\u0000' ? 'a null character' : `'${char}'`;
                return { ok: false, message: `a project name cannot contain ${shown}` };
            }
        }
        for (let i = 0; i < trimmed.length; i += 1) {
            const code = trimmed.charCodeAt(i);
            if (code < 32 || code === 127) {
                return {
                    ok: false,
                    message: 'a project name cannot contain control characters',
                };
            }
        }
        if (trimmed === '.' || trimmed === '..') {
            return { ok: false, message: "a project name cannot be '.' or '..'" };
        }
        if (trimmed.charAt(0) === '.') {
            return { ok: false, message: 'a project name cannot start with a dot' };
        }
        // Byte length, not character length: one emoji is four bytes and
        // the kernel counts bytes.
        if (new TextEncoder().encode(trimmed).length > MAX_NAME_BYTES) {
            return {
                ok: false,
                message: `a project name cannot be longer than ${MAX_NAME_BYTES} bytes`,
            };
        }
        return { ok: true, message: null };
    }

    /**
     * Description: join a parent folder and a project name into the path
     *   that will be created. Exactly one separator between them however
     *   many trailing slashes the parent carried, and the name is used as
     *   typed (trimmed only) so the folder on disk matches what the user
     *   saw in the preview.
     * Inputs: parent (string) - the chosen parent folder; name (string) -
     *   the project name.
     * Output: string - the composed absolute path, or '' when either side
     *   is missing.
     * Example: composePath('/a/b/', 'My App') === '/a/b/My App'
     */
    function composePath(parent, name) {
        const p = typeof parent === 'string' ? parent.trim() : '';
        const n = typeof name === 'string' ? name.trim() : '';
        if (!p || !n) return '';
        const base = p === '/' ? '' : p.replace(/\/+$/, '');
        return `${base}/${n}`;
    }

    /**
     * Description: resolve the folder the picker should start in. Asks
     *   the server, because the browse endpoint with no path returns the
     *   configured projects root - so the default lives in exactly one
     *   place, the server config, and never gets a second copy here.
     * Inputs: none.
     * Output: Promise<string|null> - the default parent, or null when the
     *   server could not be asked (which is CANNOT DETERMINE, not "there
     *   is no default"; the caller shows an empty field rather than a
     *   guessed path).
     * Example: await defaultParent()
     */
    async function defaultParent() {
        try {
            const res = await window.API.browseDirectory();
            return (res && res.path) || null;
        } catch (error) {
            console.warn('ProjectCreateFolder: default folder unavailable:', error);
            return null;
        }
    }

    /**
     * Description: ask where the new project should live. Shows the
     *   chosen parent, a browse button that reuses the existing folder
     *   picker, and the FULL composed path the create button will use -
     *   the preview is the point, since the bug this replaces was a user
     *   never being told where the project went.
     * Inputs: options (object) - `{ name }`, the project name already
     *   collected by the name step.
     * Output: Promise<{parent: string, path: string}|null> - the choice,
     *   or null if the user cancelled.
     * Example: await window.ProjectCreateFolder.choose({ name: 'My App' })
     */
    function choose(options) {
        const name = (options && options.name) || '';

        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» where should it live</div>
                    <div class="modal-body">
                        <div class="modal-input-group">
                            <label class="modal-label">parent folder</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="project-parent-input"
                                placeholder="loading..."
                                autocomplete="off"
                                spellcheck="false"
                            />
                            <div class="modal-description">
                                the project folder is created inside this one.
                            </div>
                        </div>
                        <div class="modal-input-group">
                            <div class="modal-label">full path</div>
                            <div class="folder-picker-path" id="project-path-preview"></div>
                            <div class="folder-picker-status" id="project-path-status" role="status" aria-live="polite"></div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="project-folder-cancel">cancel</button>
                        <button class="modal-btn modal-btn-secondary" id="project-folder-browse">browse</button>
                        <button class="modal-btn modal-btn-primary" id="project-folder-confirm">create session</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const parentInput = overlay.querySelector('#project-parent-input');
            const previewEl = overlay.querySelector('#project-path-preview');
            const statusEl = overlay.querySelector('#project-path-status');
            const browseBtn = overlay.querySelector('#project-folder-browse');
            const confirmBtn = overlay.querySelector('#project-folder-confirm');
            const cancelBtn = overlay.querySelector('#project-folder-cancel');

            const close = () => {
                if (overlay.parentNode) document.body.removeChild(overlay);
            };

            /**
             * Repaint the path preview and the inline refusal, and enable
             * or disable create to match. Returns the composed path when
             * it is usable, otherwise ''.
             */
            const refresh = () => {
                const verdict = validateName(name);
                const parent = parentInput.value.trim();
                const path = composePath(parent, name);

                previewEl.textContent = path || '(choose a folder)';
                if (!verdict.ok) {
                    statusEl.textContent = verdict.message;
                    statusEl.className = 'folder-picker-status folder-picker-status--error';
                    confirmBtn.disabled = true;
                    return '';
                }
                if (!parent) {
                    statusEl.textContent = 'choose a folder to create the project in';
                    statusEl.className = 'folder-picker-status';
                    confirmBtn.disabled = true;
                    return '';
                }
                statusEl.textContent = '';
                statusEl.className = 'folder-picker-status';
                confirmBtn.disabled = false;
                return path;
            };

            parentInput.addEventListener('input', refresh);
            refresh();

            defaultParent().then((dir) => {
                if (!overlay.parentNode) return;
                if (dir && !parentInput.value) {
                    parentInput.value = dir;
                } else if (!dir) {
                    parentInput.placeholder = 'type or browse to a folder';
                }
                refresh();
            });

            browseBtn.addEventListener('click', async () => {
                if (!window.FolderPickerModal) {
                    statusEl.textContent = 'the folder picker is unavailable; type a path instead';
                    statusEl.className = 'folder-picker-status folder-picker-status--error';
                    return;
                }
                const picked = await window.FolderPickerModal.open({ escapeHtml });
                if (picked) {
                    parentInput.value = picked;
                    refresh();
                }
            });

            confirmBtn.addEventListener('click', () => {
                const path = refresh();
                if (!path) return;
                const parent = parentInput.value.trim();
                close();
                resolve({ parent, path });
            });

            cancelBtn.addEventListener('click', () => {
                close();
                resolve(null);
            });

            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    close();
                    resolve(null);
                }
            });

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    close();
                    resolve(null);
                }
            });

            setTimeout(() => parentInput.focus(), 100);
        });
    }

    window.ProjectCreateFolder = {
        validateName,
        composePath,
        defaultParent,
        choose,
        ILLEGAL_CHARS,
        MAX_NAME_BYTES,
    };

    console.log('[ProjectCreateFolder Module] Loaded');
})();
