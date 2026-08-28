/**
 * Config/project file tree panel.
 *
 * A MODAL, opened from #configEditorBtn in the header. It used to be a
 * right-hand slide-over panel like the session sidebar; it is now the
 * FIRST of two stacked modals, with the editor
 * (client/js/config-editor-modal.js) opening over it. Both register with
 * client/js/modal-stack.js, which owns Escape routing (top modal only),
 * background scroll lock, and focus restore. On a phone the stack takes
 * this one off screen while the editor is up, so exactly one surface is
 * visible at a time and the editor's "back" control brings this one
 * back. Browses THREE roots: `~/.claude`
 * ("user"), the active project's `.claude/` ("project"), and (added
 * 2026-08) the active session's WORKING DIRECTORY itself ("workdir") -
 * general project file browsing, read AND write. All list/read/write
 * logic and the allowed-roots / hide-list live server-side in
 * src/core/config_files.py; this module only renders the tree. Opening a
 * file hands off to window.ConfigEditorModal
 * (client/js/config-editor-modal.js), which owns the editor - split into
 * two files purely for the project's 500-line file-size rule; the two
 * together are one feature.
 *
 * The picker is tree-only: full height, no inline editor sliver.
 *
 * Tree presentation: a classic filesystem-tree look - literal +/-
 * disclosure buttons (real <button>, aria-expanded, the glyph is
 * presentational), left-aligned rows with per-depth indentation GUIDE
 * lines rather than raw padding, and the three ROOTS themselves render as
 * collapsible nodes at depth 0 (same +/- affordance as any directory) so
 * the whole thing reads as one consistent tree instead of three separate
 * sections.
 *
 * Tree rendering is lazy: a directory's (or root's) children are not
 * built into the DOM until it is first expanded, and every directory
 * starts collapsed (READONLY_COLLAPSED_DIRS roots like plugins/
 * additionally IGNORE any persisted "expanded" state - see
 * ConfigEditorTreeState.startsCollapsed). This keeps a repo with
 * thousands of files under plugins/ from building thousands of nodes on open
 * - previously ALL of it rendered eagerly (~965 DOM nodes observed
 * against this user's ~/.claude), burying CLAUDE.md in the wall.
 *
 * Must load AFTER api.js (window.API), session-status-ui.js
 * (window.SessionStatusUI, shared icons), config-editor-modal.js
 * (window.ConfigEditorModal) and BEFORE app.js (App.init() wires
 * #configEditorBtn's click handler to ConfigEditorPanel.open()).
 */

console.log('[ConfigEditorPanel Module] Loading...');

// The tree's collapsed-state persistence (and the forced-collapsed
// directory list) lives in client/js/config-editor-tree-state.js -
// window.ConfigEditorTreeState. Split out for the 500-line rule; read its
// docstring before changing what "collapsed by default" means.

// The three roots this panel browses, the working-directory resolver and
// every "why is this root absent" sentence live in config-editor-roots.js
// (window.ConfigEditorRoots) - pure functions, no DOM, unit-tested. This
// file only renders what that module decides.
const CONFIG_EDITOR_ROOTS = window.ConfigEditorRoots.ROOTS;

class ConfigEditorPanelController {
    constructor() {
        this.overlay = null;
        this.panel = null;
        this.closeBtn = null;
        this.newBtn = null;
        this.treeEl = null;

        this.isOpen = false;
        this._wired = false;
    }

    /**
     * Resolve the active project's working directory AND the reason when
     * it does not resolve, so the tree can say why the project roots are
     * absent instead of silently rendering short. See
     * config-editor-roots.js for the unwrap and the reason codes.
     * Inputs: none.
     * Output: {path: string|null, reason: 'ok'|'no-session'|'no-working-dir'}.
     */
    _projectContext() {
        return window.ConfigEditorRoots.resolveProjectContext(window.TerminalController);
    }

    /**
     * Best-effort resolution of the active project's working directory,
     * used for BOTH the "project" root (its `.claude/` subdirectory) and
     * the "workdir" root (the directory itself).
     * Inputs: none.
     * Output: string|null - absolute working directory, or null when no
     *   session is attached (terminal not open, or on the launchpad).
     */
    _currentProjectPath() {
        return this._projectContext().path;
    }

    /**
     * Wire DOM elements + event listeners once. Idempotent.
     * Inputs: none. Output: void.
     */
    _wire() {
        if (this._wired) return;
        this.overlay = document.getElementById('config-editor-overlay');
        this.panel = this.overlay ? this.overlay.querySelector('.config-editor-picker-content') : null;
        this.closeBtn = document.getElementById('config-editor-close');
        this.newBtn = document.getElementById('config-editor-new');
        this.treeEl = document.getElementById('config-editor-tree');
        if (!this.overlay || !this.panel) return;

        this.closeBtn.addEventListener('click', () => this.close());
        if (this.newBtn) this.newBtn.addEventListener('click', () => this.createFile());
        // The overlay IS the backdrop - a click that lands on it rather
        // than on the drawer inside it is a dismissal, same contract as
        // every other .modal-overlay in the app. A PINNED drawer is part
        // of the layout, not something overlaid that a stray click should
        // sweep away, so it opts out (the same question
        // session-sidebar.js asks its own pin module).
        this.overlay.addEventListener('click', (e) => {
            if (e.target !== this.overlay) return;
            if (window.ConfigDrawerPin
                && !window.ConfigDrawerPin.shouldDismissOnOutsideClick()) return;
            this.close();
        });
        // No document-level Escape listener: ModalStack owns Escape for
        // this modal and the editor above it, which is the whole reason
        // one keypress no longer collapses both.
        this._wired = true;
        if (window.ConfigDrawerPin) window.ConfigDrawerPin.init();
    }

    /**
     * Open the picker modal and (re)load all roots' trees.
     * Inputs: triggerEl (Element|null) - the button that opened this,
     *   for focus-return on close (matches the settings panel's pattern).
     * Output: Promise<void>.
     */
    async open(triggerEl = null) {
        this._wire();
        if (!this.overlay) return;
        this._triggerEl = triggerEl || null;
        this.isOpen = true;
        this.overlay.hidden = false;
        // A PINNED drawer is layout, not an overlay, so Escape leaves it
        // alone - the same call session-sidebar.js's keydown handler makes.
        // The close BUTTON still works; Escape is the gesture for
        // "dismiss the thing covering my work", and a docked drawer
        // covers nothing.
        window.ModalStack.push(this.overlay, { onEscape: () => this._escape() });
        if (this._triggerEl) this._triggerEl.setAttribute('aria-expanded', 'true');
        if (this.closeBtn) {
            try { this.closeBtn.focus(); } catch (_) { /* no-op */ }
        }
        // Push the docked layout AFTER isOpen is true - apply() reads it,
        // and it is what asks TerminalLayout for the refit that makes the
        // terminal narrow to the space beside the drawer.
        if (window.ConfigDrawerPin) window.ConfigDrawerPin.apply();
        await this._loadTree();
    }

    /**
     * What Escape should do to the drawer. A pinned (docked) drawer is
     * part of the layout and covers nothing, so Escape leaves it alone -
     * the same rule session-sidebar.js applies to a pinned sidebar. Every
     * other case dismisses.
     * @returns {Promise<void>} Resolves once the close (if any) is done.
     */
    async _escape() {
        if (window.ConfigDrawerPin && window.ConfigDrawerPin.isEffectivelyPinned()) return;
        await this.close();
    }

    /**
     * Close the picker. Refuses to close over an unsaved editor without
     * asking first - the editor modal may be stacked on top of this one,
     * and an Escape/backdrop-click here must not silently drop an edit
     * any more than the editor's own dismissal handlers would.
     * Inputs: none. Output: Promise<void>.
     */
    async close() {
        if (!this.overlay) return;
        if (window.ConfigEditorModal && window.ConfigEditorModal.isDirty()) {
            if (!(await window.ConfigEditorModal.confirmDiscardIfDirty())) return;
        }
        if (window.ConfigEditorModal) window.ConfigEditorModal.close();
        this.isOpen = false;
        window.ModalStack.pop(this.overlay);
        this.overlay.hidden = true;
        // Undock: drop the body class and give the terminal its width back.
        // apply() reads isOpen, so this must run after it is cleared.
        if (window.ConfigDrawerPin) window.ConfigDrawerPin.apply();
        if (this._triggerEl) {
            this._triggerEl.setAttribute('aria-expanded', 'false');
            this._triggerEl.focus();
        }
    }

    /**
     * Ask for a new file, create it, then reload the tree and open the
     * new file in the editor - so "create" lands the user where they were
     * going anyway rather than back at a tree they now have to search.
     * Offers only the roots this panel is actually showing: "user" always,
     * "project"/"workdir" only when a session is attached, because the
     * server cannot resolve either without a working directory.
     * Inputs: none. Output: Promise<void>.
     */
    async createFile() {
        const projectPath = this._currentProjectPath();
        const roots = CONFIG_EDITOR_ROOTS
            .filter(r => r.id === 'user' || projectPath)
            .map(r => ({ id: r.id, label: r.label }));
        const created = await window.ConfigEditorNewFile.open(roots, projectPath);
        if (!created) return;
        await this._loadTree();
        window.ConfigEditorModal.open(
            created.root,
            created.path,
            false,
            created.root === 'user' ? null : projectPath,
        );
    }

    // ---- tree loading / rendering -----------------------------------

    /**
     * Load all roots' trees (each fetched independently so one root's
     * failure/emptiness doesn't block the others) and render them as one
     * flat top-level list, each root a collapsible node.
     *
     * A COULD-NOT-EVALUATE root is never dropped in silence. When the
     * working directory does not resolve at all, ConfigEditorRoots
     * .planRoots() returns a notice in the project roots' place; when a
     * root the panel DID ask for turns out to be genuinely unreachable
     * (see _buildRootEl - workdir vanishing out from under an attached
     * session, or a real 401/403/5xx/network failure), that renders as a
     * named error row too. A MEASURED ABSENCE is the opposite case and is
     * intentionally silent: a project with no .claude/ subfolder, or a
     * root that resolved to zero entries, renders no row and no message
     * at all - the user is not told about a file that was never there,
     * only about a file the panel could not find out about one way or
     * the other.
     * Inputs: none. Output: Promise<void>.
     */
    async _loadTree() {
        this.treeEl.innerHTML = '<div class="config-editor-loading">loading...</div>';
        const context = this._projectContext();
        const projectPath = context.path;
        const list = document.createElement('ul');
        list.className = 'config-editor-list config-editor-list--roots';

        for (const step of window.ConfigEditorRoots.planRoots(context)) {
            if (step.kind === 'notice') {
                list.appendChild(this._noticeLi(step.message));
                continue;
            }
            const el = await this._buildRootEl(step.def, projectPath);
            // A null element means the root is a MEASURED ABSENCE (no
            // .claude/ subfolder, or a root the server listed zero
            // entries for) - _buildRootEl already decided that needs no
            // row and no message, so there is nothing left to append
            // here. A could-not-evaluate case (e.g. workdir vanished)
            // always comes back as a populated element instead; see
            // _buildRootEl's own doc for the split.
            if (el) list.appendChild(el);
        }

        this.treeEl.innerHTML = '';
        this.treeEl.appendChild(list);
    }

    /**
     * Fetch one root's tree and build it as a collapsible depth-0 node,
     * indistinguishable in interaction from a directory node inside it.
     *
     * THREE OUTCOMES, but only two ever produce visible output. A root
     * that resolves - even to zero entries - renders as a normal node:
     * the path/label, and whatever is underneath it (nothing, if that's
     * the truth). A root that is a MEASURED ABSENCE (the project has no
     * .claude/ subfolder at all) renders NOTHING - no row, no message;
     * the user never asked to be told about a folder that was never
     * there. A root the server could not even evaluate (workdir's own
     * directory vanished out from under an attached session, or any
     * other real failure - 401/403/5xx, network error) DOES render,
     * as a named error row, because that is the one case a short tree
     * cannot be allowed to explain by staying silent.
     *
     * Inputs: rootDef (object) - one entry of CONFIG_EDITOR_ROOTS;
     *   projectPath (string|null).
     * Output: Promise<Element|null> - the <li> to append, or null when
     *   this root is a measured absence and the caller should render
     *   nothing for it at all.
     */
    async _buildRootEl(rootDef, projectPath) {
        let nodes;
        try {
            const resp = await window.API.getConfigFileTree(rootDef.id, projectPath);
            nodes = resp.tree || [];
        } catch (err) {
            // A project working directory with no .claude/ subdirectory
            // is the ordinary case, not a failure: config_files.py's
            // resolve_roots() only registers the "project" root when the
            // directory exists, so list_tree() raises "unknown or
            // unavailable root" (HTTP 400) for every project that simply
            // hasn't got project-scoped config yet. That is a measured
            // absence - render nothing for it, the same as an empty root.
            if (err.status === 400 && rootDef.id === 'project') {
                return null;
            }
            // "workdir" is different: resolve_roots() only registers it
            // when the session's own working directory still exists on
            // disk, and a session always has one when it starts. A 400
            // here means that directory was deleted, unmounted, or
            // renamed out from under a still-attached session - could
            // not evaluate, not a normal absence - so it gets a named
            // row instead of being dropped.
            if (err.status === 400 && rootDef.id === 'workdir') {
                const li = document.createElement('li');
                li.appendChild(this._errorEl(
                    window.ConfigEditorRoots.workdirUnavailableNotice(rootDef.label, projectPath),
                ));
                return li;
            }
            // Any other status (401/403/5xx, or no status for a network
            // failure) is a real failure and surfaces as an error row.
            const li = document.createElement('li');
            li.appendChild(this._errorEl(`failed to load ${rootDef.label}: ${err.message || err}`));
            return li;
        }

        // Zero entries is a successfully-read, genuinely empty root - not
        // a failure and not worth a sentence. Render it exactly like any
        // other root: the path/label, with nothing underneath.
        const rootNode = { name: rootDef.label, rel_path: '', is_dir: true, children: nodes };
        return this._buildNodeEl(rootDef.id, rootNode, 0, { isRoot: true, defaultExpanded: rootDef.defaultExpanded });
    }

    /**
     * Build one <li> for a tree node - a directory/root (collapsible,
     * +/- disclosure) or a file (single tappable row that opens the
     * editor modal). Directories/roots get a real <button> toggle with a
     * literal `+`/`-` glyph (presentational; the control's actual state
     * is `aria-expanded`) and a children <ul> that is populated the first
     * time the node is expanded - at render time if it STARTS expanded,
     * otherwise on the click that expands it. That is the lazy-render
     * mechanism that keeps a 1000+ entry collapsed directory from costing
     * 1000+ DOM nodes until the user asks to see them. Every row is
     * left-aligned with per-depth indentation GUIDE lines (vertical
     * rules) rather than raw padding, so the tree reads as a real
     * filesystem tree rather than a flat, centered list.
     * Inputs: rootId (string); node (object) - server TreeNode, or (for
     *   depth 0) a synthetic root node; depth (number) - for indentation;
     *   rootOpts (object|undefined) - {isRoot, defaultExpanded}, only
     *   passed for the depth-0 root node itself.
     * Output: Element - <li>.
     */
    _buildNodeEl(rootId, node, depth, rootOpts) {
        const li = document.createElement('li');
        const isRoot = !!(rootOpts && rootOpts.isRoot);
        li.className = `config-editor-node config-editor-node--${node.is_dir ? 'dir' : 'file'}${isRoot ? ' config-editor-node--root' : ''}`;

        const guides = this._buildGuides(depth);

        if (node.is_dir) {
            const collapsed = window.ConfigEditorTreeState.startsCollapsed(
                rootId, node, isRoot ? rootOpts.defaultExpanded : undefined);
            const key = `${rootId}:${node.rel_path || '__root__'}`;

            const toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = `config-editor-toggle${isRoot ? ' config-editor-toggle--root' : ''}`;
            toggle.setAttribute('aria-expanded', String(!collapsed));
            const glyphSpan = `<span class="config-editor-toggle-glyph" aria-hidden="true">${collapsed ? '+' : '-'}</span>`;
            toggle.innerHTML = (
                guides +
                glyphSpan +
                window.SessionStatusUI.folderIconSvg() +
                `<span class="config-editor-node-name${isRoot ? ' config-editor-root-label' : ''}">${this._esc(node.name)}</span>` +
                (node.read_only ? ' <span class="config-editor-badge">read-only</span>' : '')
            );

            const childList = document.createElement('ul');
            childList.className = 'config-editor-list';
            childList.hidden = collapsed;
            let built = false;

            // Populate `childList` exactly once. Lazy-render is about
            // deferring the cost until a node is EXPANDED, not until it is
            // CLICKED - a node that starts expanded (the `~/.claude` and
            // project `.claude` roots do) has to be built here at render
            // time, or it shows an open disclosure over an empty list and
            // needs two clicks to reveal anything.
            const buildChildren = () => {
                if (built) return;
                built = true;
                // THREE-OUTCOME RULE: node.list_error means this
                // directory's own contents could not be enumerated
                // (server-side OSError, see config_files.py) - render
                // that explicitly instead of leaving an empty <ul> that
                // is visually identical to a directory that really has
                // nothing in it.
                if (node.list_error) {
                    childList.appendChild(this._noticeLi(
                        window.ConfigEditorRoots.listErrorNotice(node.list_error),
                    ));
                    return;
                }
                (node.children || []).forEach(
                    (child) => childList.appendChild(this._buildNodeEl(rootId, child, depth + 1)),
                );
            };
            if (!collapsed) buildChildren();

            toggle.addEventListener('click', () => {
                const nowExpanded = toggle.getAttribute('aria-expanded') !== 'true';
                if (nowExpanded) buildChildren();
                toggle.setAttribute('aria-expanded', String(nowExpanded));
                toggle.querySelector('.config-editor-toggle-glyph').textContent = nowExpanded ? '-' : '+';
                childList.hidden = !nowExpanded;
                if (!window.ConfigEditorTreeState.isForcedCollapsed(node.name)) {
                    window.ConfigEditorTreeState.setNodeCollapsed(key, !nowExpanded);
                }
            });

            li.appendChild(toggle);
            li.appendChild(childList);
            return li;
        }

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'config-editor-file';
        btn.dataset.root = rootId;
        btn.dataset.path = node.rel_path;
        btn.dataset.readonly = node.read_only ? '1' : '0';
        btn.dataset.sensitive = node.is_sensitive ? '1' : '0';
        btn.innerHTML = (
            guides +
            '<span class="config-editor-toggle-spacer" aria-hidden="true"></span>' +
            window.SessionStatusUI.fileIconSvg() +
            `<span class="config-editor-node-name">${this._esc(node.name)}</span>` +
            (node.is_executable ? ' <span class="config-editor-badge config-editor-badge--exec">runs automatically</span>' : '') +
            (node.is_sensitive ? ` <span class="config-editor-badge config-editor-badge--sensitive">${window.SessionStatusUI.lockIconSvg()}sensitive</span>` : '')
        );
        btn.addEventListener('click', () => {
            const projectPath = rootId === 'user' ? null : this._currentProjectPath();
            window.ConfigEditorModal.open(rootId, node.rel_path, node.read_only, projectPath);
        });
        li.appendChild(btn);
        return li;
    }

    /**
     * Build the per-depth indentation guide markup for one row: one
     * fixed-width vertical-rule span per ancestor level, so a row's
     * indentation reads as connected tree lines rather than blank
     * left-padding. Purely decorative (aria-hidden).
     * Inputs: depth (number) - 0 for a root node.
     * Output: string - HTML for `depth` guide spans.
     */
    _buildGuides(depth) {
        let html = '';
        for (let i = 0; i < depth; i++) {
            html += '<span class="config-editor-guide" aria-hidden="true"></span>';
        }
        return html;
    }

    /**
     * Build a top-level <li> carrying one explanatory notice: a root that
     * is absent, and why. Not an error - the ordinary "this project has
     * no .claude/" case lands here too - but never silent either.
     * Inputs: message (string). Output: Element - <li>.
     */
    _noticeLi(message) {
        const li = document.createElement('li');
        li.className = 'config-editor-node config-editor-node--notice';
        const el = document.createElement('div');
        el.className = 'config-editor-notice';
        el.textContent = message;
        li.appendChild(el);
        return li;
    }

    /**
     * Build an error message block for the tree.
     * Inputs: message (string). Output: Element.
     */
    _errorEl(message) {
        const el = document.createElement('div');
        el.className = 'config-editor-error';
        el.textContent = message;
        return el;
    }

    /**
     * HTML-escape a string for safe interpolation into innerHTML.
     * Inputs: value (any). Output: string.
     */
    _esc(value) {
        const div = document.createElement('div');
        div.textContent = String(value == null ? '' : value);
        return div.innerHTML;
    }
}

window.ConfigEditorPanel = new ConfigEditorPanelController();
console.log('[ConfigEditorPanel Module] Exported as window.ConfigEditorPanel');
