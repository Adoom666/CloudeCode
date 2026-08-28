/**
 * Folder Picker Modal - browse the server filesystem and pick a directory.
 *
 * EXTRACTED FROM launchpad.js, 2026-08-26. That file was 5,106 lines
 * against this project's 500-line standard, and six agents touched it in
 * a single day, so a full split is a regression risk nobody needs. This
 * is the one unit that came out cleanly, and it was chosen by
 * measurement rather than by eye: of every candidate cluster in the
 * file, it had the best ratio of lines removed to instance coupling -
 * 225 lines, ONE call site, no external callers, and exactly one
 * reference to the controller (`_escapeHtml`), which is now an injected
 * argument rather than a hidden `this`.
 *
 * Deliberately NOT a class and deliberately not aware of Launchpad. It
 * talks to `window.API` for the two directory calls it needs and returns
 * a Promise, which is the whole contract. Nothing here reads controller
 * state, so nothing here can drift with it.
 *
 * Load order: BEFORE launchpad.js, which delegates to it.
 */

console.log('[FolderPickerModal Module] Loading...');

(function () {
    /**
     * Description: fall back to a local escaper when no injected one is
     *   given, so the module is usable standalone and a caller that
     *   forgets the argument cannot render raw markup.
     * Inputs: s (any) - the value to escape.
     * Output: string - HTML-safe text.
     * Example: defaultEscapeHtml('<b>') === '&lt;b&gt;'
     */
    function defaultEscapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Description: show the folder picker and resolve with the user's
     *   choice. Markup, class names and keyboard behaviour are unchanged
     *   from the launchpad.js original - the extraction moved code, it
     *   did not redesign anything.
     * Inputs: options (object) - `{ escapeHtml }`, an optional escaper.
     * Output: Promise<string|null> - the chosen absolute path, or null if
     *   the user cancelled.
     * Example: await window.FolderPickerModal.open()
     */
    function open(options) {
        var esc = (options && options.escapeHtml) || defaultEscapeHtml;
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            overlay.innerHTML = `
                <div class="modal-content folder-picker-modal">
                    <div class="modal-header">» select a folder</div>
                    <div class="modal-body">
                        <input type="text" class="folder-picker-path modal-input" id="folder-picker-path"
                               spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off"
                               aria-label="directory path"
                               placeholder="type a path then Enter - created if it doesn't exist">
                        <div class="folder-picker-status" id="folder-picker-status" role="status" aria-live="polite"></div>
                        <div class="folder-picker-toolbar">
                            <button class="folder-picker-toolbar-btn" id="folder-picker-up" title="go to parent directory">⬆ up</button>
                            <button class="folder-picker-toolbar-btn" id="folder-picker-home" title="go to home directory">🏠 home</button>
                        </div>
                        <div class="folder-picker-list" id="folder-picker-list" tabindex="-1">
                            <div class="folder-picker-empty">loading...</div>
                        </div>
                        <div class="modal-description">
                            click a folder to enter it, or type letters to jump · ↑/↓ to move · Enter opens the highlighted folder.
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="folder-picker-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="folder-picker-confirm">open here</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const pathInput = overlay.querySelector('#folder-picker-path');
            const statusEl = overlay.querySelector('#folder-picker-status');
            const listEl = overlay.querySelector('#folder-picker-list');
            const upBtn = overlay.querySelector('#folder-picker-up');
            const homeBtn = overlay.querySelector('#folder-picker-home');
            const confirmBtn = overlay.querySelector('#folder-picker-confirm');
            const cancelBtn = overlay.querySelector('#folder-picker-cancel');

            let currentPath = null;
            let currentParent = null;
            let entries = [];        // current folder entries: [{name, path}]
            let activeIndex = -1;    // highlighted item index into `entries`
            let typeBuffer = '';     // type-ahead accumulator
            let typeTimer = null;    // resets the type-ahead buffer after idle

            const close = (value) => {
                document.removeEventListener('keydown', onKeyDown, true);
                if (typeTimer) clearTimeout(typeTimer);
                document.body.removeChild(overlay);
                resolve(value);
            };

            const clearStatus = () => {
                statusEl.textContent = '';
                statusEl.className = 'folder-picker-status';
            };
            const showStatus = (msg, kind) => {
                statusEl.textContent = msg;
                statusEl.className = `folder-picker-status folder-picker-status--${kind || 'info'}`;
            };

            // Highlight the entry at `idx` (clamped) and scroll it into view.
            const setActive = (idx, { scroll = true } = {}) => {
                const els = listEl.querySelectorAll('.folder-picker-item');
                if (!els.length) { activeIndex = -1; return; }
                idx = Math.max(0, Math.min(idx, els.length - 1));
                els.forEach((el, i) => el.classList.toggle('folder-picker-item-active', i === idx));
                activeIndex = idx;
                if (scroll) els[idx].scrollIntoView({ block: 'nearest' });
            };

            // Render entries + sync path bar / parent button from a browse or
            // mkdir response. Single source of "we moved into a directory".
            const navigate = (data) => {
                currentPath = data.path;
                currentParent = data.parent;
                entries = Array.isArray(data.entries) ? data.entries : [];
                pathInput.value = data.path || '';
                upBtn.disabled = !data.parent;
                activeIndex = -1;
                typeBuffer = '';
                if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }

                if (!entries.length) {
                    listEl.innerHTML = '<div class="folder-picker-empty">no subfolders here</div>';
                    return;
                }

                listEl.innerHTML = entries.map((entry, i) => `
                    <div class="folder-picker-item" data-index="${i}" data-path="${entry.path.replace(/"/g, '&quot;')}">
                        <span class="folder-picker-icon">📁</span>
                        <span class="folder-picker-name">${esc(entry.name)}</span>
                    </div>
                `).join('');

                listEl.querySelectorAll('.folder-picker-item').forEach(item => {
                    item.addEventListener('click', () => loadPath(item.dataset.path));
                    item.addEventListener('mousemove', () => {
                        const idx = parseInt(item.dataset.index, 10);
                        if (idx !== activeIndex) setActive(idx, { scroll: false });
                    });
                });
            };

            const loadPath = async (targetPath) => {
                listEl.innerHTML = '<div class="folder-picker-empty">loading...</div>';
                clearStatus();
                try {
                    navigate(await window.API.browseDirectory(targetPath));
                } catch (error) {
                    console.error('Launchpad: Folder browse failed:', error);
                    listEl.innerHTML = `<div class="folder-picker-empty">error: ${esc(error.message || String(error))}</div>`;
                }
            };

            // Enter in the path bar: browse the typed path; if it 404s
            // (doesn't exist / not a directory) auto-create it via mkdir -p
            // and navigate straight in. Other errors surface inline.
            const submitPath = async () => {
                const value = pathInput.value.trim();
                if (!value) return;
                clearStatus();
                try {
                    navigate(await window.API.browseDirectory(value));
                } catch (error) {
                    if (error && error.status === 404) {
                        try {
                            const data = await window.API.makeDirectory(value);
                            navigate(data);
                            showStatus(`created ${data.path}`, 'success');
                        } catch (mkErr) {
                            showStatus(`could not create: ${mkErr.message || mkErr}`, 'error');
                        }
                    } else {
                        showStatus(error.message || String(error), 'error');
                    }
                }
            };

            pathInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    submitPath();
                }
            });

            // Modal-wide key handling (capture phase so it works no matter
            // which child holds focus). Removed on close - no listener leak.
            const onKeyDown = (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault();
                    close(null);
                    return;
                }

                // While editing the path, let the input own its keystrokes
                // (typing edits the path; its own handler submits on Enter).
                if (document.activeElement === pathInput) return;

                // Don't hijack keyboard activation of a focused button.
                const ae = document.activeElement;
                if (ae && ae.tagName === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) return;

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    setActive(activeIndex < 0 ? 0 : activeIndex + 1);
                    return;
                }
                if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    setActive(activeIndex < 0 ? entries.length - 1 : activeIndex - 1);
                    return;
                }
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (activeIndex >= 0 && entries[activeIndex]) {
                        loadPath(entries[activeIndex].path);
                    } else if (currentPath) {
                        close(currentPath);
                    }
                    return;
                }

                // Type-ahead: printable single chars only, no modifier combos.
                if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
                    e.preventDefault();
                    typeBuffer += e.key.toLowerCase();
                    if (typeTimer) clearTimeout(typeTimer);
                    typeTimer = setTimeout(() => { typeBuffer = ''; }, 800);
                    const matchIdx = entries.findIndex(
                        en => en.name.toLowerCase().startsWith(typeBuffer)
                    );
                    if (matchIdx >= 0) setActive(matchIdx);
                }
            };
            document.addEventListener('keydown', onKeyDown, true);

            upBtn.addEventListener('click', () => {
                if (currentParent) loadPath(currentParent);
            });

            homeBtn.addEventListener('click', () => loadPath('~'));

            confirmBtn.addEventListener('click', () => {
                if (currentPath) close(currentPath);
            });

            cancelBtn.addEventListener('click', () => close(null));

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) close(null);
            });

            // Start at the server's default location.
            loadPath(null);

            // Default focus to the LIST (not the path bar) so type-ahead works
            // immediately; clicking the path bar focuses it for direct entry.
            setTimeout(() => listEl.focus(), 100);
        });
    }

    window.FolderPickerModal = { open: open };
})();
