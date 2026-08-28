/**
 * Slash Commands Module
 *
 * Handles the slash command quick-access modal. The command LIST is no
 * longer hand-written here (see git history for the retired
 * `ALL_SLASH_COMMANDS` array) - it comes from the server's
 * `GET /config/slash-commands`, which merges the release-time scraped
 * official command list with runtime discovery of this user's own
 * commands/skills, installed plugins, and (when a project is open) that
 * project's own commands/skills. See `src/core/slash_command_discovery.py`
 * and `scripts/scrape-slash-commands.py` for where the data comes from.
 *
 * Open/close behavior (button, lazy modal creation, overlay-click-to-close,
 * Escape-to-close) is UNCHANGED from before this module was rewritten.
 * What's new: groups render from server data, and once the modal is open,
 * typing in the filter input live-filters the list (command name +
 * description), with arrow-key/Enter navigation over the visible results.
 */

console.log('[SlashCommands Module] Loading...');

class SlashCommandsModal {
    constructor() {
        this.commonCommands = [];
        // Parallel to commonCommands: [{command, description}] with the
        // SHORT chip description. See _loadCommonCommands().
        this.commonCommandDetails = [];
        // True while the row shows the built-in defaults because the user
        // has never starred anything. See _loadCommonCommands.
        this.commonDefaulted = false;
        this.groups = [];
        // Flat command -> record lookup built from `this.groups`, used
        // for the common-commands button tooltips and for resolving a
        // bare command string back to its full record.
        this.commandIndex = new Map();
        this.modal = null;
        this.button = null;
        this.isOpen = false;
        this.onCommandSelect = null;
        this.projectPath = null;

        // Live-filter + keyboard-nav state (Task 4) - see
        // client/js/slash-command-filter.js. Owns the DOM indexing,
        // filtering, and arrow-key bookkeeping over the rendered list.
        this.filter = new window.SlashCommandFilter();
    }

    /**
     * Initialize the slash commands modal
     *
     * Description: fetches the common-commands "favorites" row and the
     *   full grouped command palette from the server, then creates the
     *   floating button. Does NOT create the modal itself - that still
     *   happens lazily on first open() (unchanged from before).
     * Inputs:
     *   onCommandSelect (function(string): void) - called with the bare
     *     command string (e.g. "/clear") when the user picks one.
     *   projectPath (string|null) - absolute path of the active project's
     *     working directory, forwarded to the server for project-scope
     *     command/skill discovery. Optional; omit when no project is open.
     * Output: Promise<void>.
     */
    async init(onCommandSelect, projectPath = null) {
        console.log('[SlashCommands] Initializing (NOT creating modal yet)');
        this.onCommandSelect = onCommandSelect;
        this.projectPath = projectPath || null;

        await Promise.all([
            this._loadCommonCommands(),
            this._loadGroups(),
        ]);

        console.log('[SlashCommands] Initialization complete (modal will be created on demand)');
        this.createButton();
    }

    /**
     * Fetch the common-commands "favorites" row. Falls back to a fixed
     * default set on any failure so the row is never empty.
     * Inputs: none. Output: Promise<void> (sets this.commonCommands).
     */
    async _loadCommonCommands() {
        try {
            const response = await window.API.getCommonCommands();
            this.commonCommands = response.commands || [];
            // `command_details` is the newer parallel array carrying the
            // short description for each chip. Servers predating it send
            // only `commands`, so fall back to bare entries rather than
            // rendering nothing.
            this.commonCommandDetails = Array.isArray(response.command_details)
                ? response.command_details
                : this.commonCommands.map(c => ({ command: c, description: '' }));
            // `defaulted` distinguishes "never starred anything, these
            // are the built-ins" from "these are the user's picks", so
            // the row can say which it is. A server predating the field
            // sends neither, which reads as "the user's picks" - the
            // conservative answer, since it never claims authorship the
            // user did not have.
            this.commonDefaulted = response.defaulted === true;
            console.log('[SlashCommands] Fetched', this.commonCommands.length, 'common commands');
        } catch (error) {
            console.error('[SlashCommands] Failed to fetch common commands:', error);
            this.commonCommands = [
                '/agents', '/clear', '/compact', '/context',
                '/hooks', '/mcp', '/resume', '/rewind', '/usage'
            ];
            this.commonCommandDetails = this.commonCommands.map(
                c => ({ command: c, description: '' })
            );
            this.commonDefaulted = true;
        }
    }

    /**
     * Apply a post-write favorites payload: repaint the chip row and
     * every star in the list below, then re-wire both.
     *
     * Description: repaints from the AUTHORITATIVE server response rather
     *   than mutating local state to match what the click intended, so a
     *   partially-applied write can never leave the UI claiming a star
     *   that is not on disk. Only the two star-bearing regions are
     *   rebuilt; the filter's index is untouched because it holds
     *   references to `.command-item` elements, which are not replaced.
     * Inputs: payload (object) - `{commands, command_details, defaulted}`.
     * Output: void.
     */
    _applyFavorites(payload) {
        this.commonCommands = payload.commands || [];
        this.commonCommandDetails = Array.isArray(payload.command_details)
            ? payload.command_details
            : [];
        this.commonDefaulted = payload.defaulted === true;
        window.SlashFavorites.repaint(this.modal, {
            chipHtml: () => this.renderCommonCommands(),
            details: this.commonCommandDetails,
            onSelect: (command) => this.selectCommand(command),
            onChange: (p) => this._applyFavorites(p),
        });
    }

    /**
     * Fetch the full grouped command palette and rebuild the flat lookup
     * index used for tooltips and filtering. Falls back to an empty group
     * list on failure - the modal still opens, just with an explanatory
     * empty state (see renderAllCommands).
     * Inputs: none. Output: Promise<void> (sets this.groups, this.commandIndex).
     */
    async _loadGroups() {
        try {
            const response = await window.API.getSlashCommands(this.projectPath);
            this.groups = Array.isArray(response.groups) ? response.groups : [];
            console.log('[SlashCommands] Fetched', this.groups.length, 'command groups');
        } catch (error) {
            console.error('[SlashCommands] Failed to fetch slash command groups:', error);
            this.groups = [];
        }
        this.commandIndex = new Map();
        for (const group of this.groups) {
            for (const cmd of (group.commands || [])) {
                this.commandIndex.set(cmd.command, cmd);
            }
        }
    }

    /**
     * Create the floating slash command button
     */
    createButton() {
        if (this.button) {
            console.log('[SlashCommands] Button already exists');
            return;
        }

        this.button = document.createElement('button');
        this.button.id = 'slash-commands-btn';
        this.button.className = 'slash-commands-btn';
        this.button.setAttribute('aria-label', 'Open Slash Commands');
        this.button.setAttribute('title', 'Open Slash Commands');
        // AUTHENTICATED-ONLY. show() below sets an inline
        // `display: flex`, which used to survive a logout because
        // App.showAuth() named the controls it hid and this one was not
        // on that list - so the slash button sat on the LOGIN screen.
        // This token puts it under the fail-closed rule in
        // client/css/screen-chrome.css instead, which is `!important`
        // precisely so it beats that inline style.
        this.button.setAttribute('data-auth-only', '');
        this.button.style.display = 'none'; // Hidden by default

        // Set initial icon (slash)
        this.updateButtonIcon();

        this.button.addEventListener('click', () => {
            if (this.isOpen) {
                this.close();
            } else {
                this.open();
            }
        });

        document.body.appendChild(this.button);
        console.log('[SlashCommands] Button created and added to DOM');
    }

    /**
     * Update button icon based on open/closed state
     */
    updateButtonIcon() {
        if (!this.button) return;

        if (this.isOpen) {
            // X icon (close)
            this.button.innerHTML = `
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                    <path d="M6 6L18 18M18 6L6 18" stroke="#d77757" stroke-width="2" stroke-linecap="round"/>
                </svg>
            `;
        } else {
            // Slash icon (open)
            this.button.innerHTML = `
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                    <path d="M14 4L10 20" stroke="#d77757" stroke-width="2.5" stroke-linecap="round"/>
                </svg>
            `;
        }
    }

    /**
     * Show the button
     */
    show() {
        if (this.button) {
            this.button.style.display = 'flex';
        }
    }

    /**
     * Hide the button
     */
    hide() {
        if (this.button) {
            this.button.style.display = 'none';
        }
    }

    /**
     * Create the modal HTML structure
     * Note: Content is nested INSIDE overlay (like D-pad pattern)
     */
    createModal() {
        console.log('[SlashCommands] createModal() called');

        const modalHTML = `
            <div id="slash-commands-modal" class="modal" style="display: none;">
                <div class="modal-overlay">
                    <div class="modal-content slash-commands-modal-content">
                        <div class="modal-header">
                            <h2>/ Slash Commands</h2>
                            <button class="modal-close" aria-label="Close modal" title="Close modal">&times;</button>
                        </div>
                        <div class="modal-body">
                            <div class="common-commands-section">
                                <div class="common-commands-grid" id="common-commands-grid">
                                    ${this.renderCommonCommands()}
                                </div>
                            </div>
                            <div class="all-commands-section">
                                <h3>all commands</h3>
                                <input
                                    type="text"
                                    id="slash-command-filter"
                                    class="command-filter-input"
                                    placeholder="type to filter..."
                                    autocomplete="off"
                                    aria-label="Filter commands"
                                />
                                <div class="all-commands-list" id="all-commands-list">
                                    ${this.renderAllCommands()}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Remove existing modal if any
        const existingModal = document.getElementById('slash-commands-modal');
        if (existingModal) {
            console.log('[SlashCommands] Removing existing modal');
            existingModal.remove();
        }

        // Add modal to body
        document.body.insertAdjacentHTML('beforeend', modalHTML);
        this.modal = document.getElementById('slash-commands-modal');
        console.log('[SlashCommands] Modal added to DOM:', this.modal ? 'success' : 'FAILED');
        this.filter.index(this.modal);
    }

    /**
     * Description: render the common-commands "favorites" row as chips.
     *   Each chip shows the command name plus a SHORT description on a
     *   second line. The description is the whole point of the change:
     *   the row used to carry the full scraped description in a `title`
     *   attribute only, which is invisible on a phone (no hover) and far
     *   too long to render inline. The short text comes from the server's
     *   `command_details` (see src/core/slash_command_labels.py), which
     *   caps length so the chip stays one line and the row stays above
     *   the fold. The long description is still available on hover via
     *   `title` for desktop users.
     * Inputs: none (reads this.commonCommandDetails).
     * Output: string - HTML for the chip grid.
     */
    renderCommonCommands() {
        const details = this.commonCommandDetails || [];
        console.log('[SlashCommands] renderCommonCommands() called with', details.length, 'commands');
        // Markup lives in client/js/slash-favorites.js: each chip now
        // carries its own star, so unstarring happens where the chip is
        // rather than by hunting the command down in the list below.
        return window.SlashFavorites.renderChips(
            details,
            this.commonDefaulted,
            (cmd) => {
                const info = this.commandIndex.get(cmd);
                return (info && info.description) || '';
            }
        );
    }

    /**
     * Render all commands grouped by the server-derived groups (Task 3).
     * Each group becomes a `.command-category`.
     *
     * Descriptions render SHORTENED (window.CommandDescription), because a
     * 240-character scraped description is seven wrapped lines on a phone
     * and 145 of them make the list run forever. The FULL text is the
     * value and stays on the row as `data-description`: the live filter
     * searches that (see client/js/slash-command-filter.js), and the
     * `.command-more` button swaps the short text for it in place. Nothing
     * reads the rendered string as data.
     * Inputs: none (reads this.groups). Output: string - HTML.
     */
    renderAllCommands() {
        if (!this.groups || this.groups.length === 0) {
            return '<div style="color: #858585; padding: 12px; text-align: center;">no commands available</div>';
        }

        const starred = window.SlashFavorites.favoriteSet(this.commonCommandDetails);
        return this.groups.map(group => {
            const commands = group.commands || [];
            if (commands.length === 0) return '';
            return `
                <div class="command-category" data-group-id="${this._escapeHtml(group.id)}">
                    <h4 class="category-title">${this._escapeHtml(group.label)}</h4>
                    ${commands.map(cmd => this._renderCommandItem(cmd, starred.has(cmd.command))).join('')}
                </div>
            `;
        }).join('');
    }

    /**
     * Render one `.command-item` row.
     * Inputs: cmd (object) - {command, args, description} from the server.
     * Output: string - HTML for one row.
     */
    _renderCommandItem(cmd, starred) {
        const display = cmd.args ? `${cmd.command} ${cmd.args}` : cmd.command;
        const full = cmd.description || '';
        const short = window.CommandDescription.shorten(full);
        const more = window.CommandDescription.isShortened(full)
            ? '<button type="button" class="command-more" aria-expanded="false">more</button>'
            : '';
        // `data-description` still carries the FULL text and is still the
        // only thing the live filter reads (slash-command-filter.js). The
        // star is a sibling control and changes nothing about that.
        return `
            <div class="command-item" data-command="${this._escapeHtml(cmd.command)}" data-description="${this._escapeHtml(full)}">
                ${window.SlashFavorites.starButton(cmd.command, !!starred)}
                <span class="command-name">${this._escapeHtml(display)}</span>
                <span class="command-description">${this._escapeHtml(short)}</span>
                ${more}
            </div>
        `;
    }

    /**
     * Swap one row's description between its shortened display form and
     * the full text held in `data-description`. The row's canonical value
     * is never touched, so the filter's index stays correct either way.
     * Inputs: btn (Element) - the `.command-more` button that was tapped.
     * Output: void.
     */
    _toggleFullDescription(btn) {
        const item = btn.closest('.command-item');
        if (!item) return;
        const descEl = item.querySelector('.command-description');
        if (!descEl) return;
        const full = item.dataset.description || '';
        const expanded = btn.getAttribute('aria-expanded') === 'true';
        descEl.textContent = expanded ? window.CommandDescription.shorten(full) : full;
        btn.setAttribute('aria-expanded', String(!expanded));
        btn.textContent = expanded ? 'more' : 'less';
    }

    /**
     * HTML-escape helper - command descriptions/labels come from scraped
     * docs and user-authored files, both effectively untrusted input for
     * innerHTML purposes.
     */
    _escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Attach event listeners
     */
    attachEventListeners() {
        if (!this.modal) return;

        // Close button
        const closeBtn = this.modal.querySelector('.modal-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => this.close());
        }

        // Overlay click to close.
        //
        // Routed through DismissGuard.onOverlayDismiss instead of a bare
        // `click -> close()`. The bare form fired for ANY click that
        // bubbled up, including clicks on the filter input three levels
        // down, so clicking the search box closed the whole menu and it
        // could never be typed into. See client/js/dismiss-guard.js.
        const overlay = this.modal.querySelector('.modal-overlay');
        if (overlay) {
            window.DismissGuard.onOverlayDismiss(overlay, () => this.close());
        }

        // Common command buttons
        const commandButtons = this.modal.querySelectorAll('.command-button');
        commandButtons.forEach(btn => {
            btn.addEventListener('click', () => {
                const command = btn.dataset.command;
                this.selectCommand(command);
            });
        });

        // All commands items - click-to-select path, unchanged.
        const commandItems = this.modal.querySelectorAll('.command-item');
        commandItems.forEach(item => {
            item.addEventListener('click', () => {
                const command = item.dataset.command;
                this.selectCommand(command);
            });
        });

        // Star toggles, on the chips AND on every list row. Wired after
        // the row/chip click handlers above precisely so its own
        // stopPropagation runs first; see slash-favorites.js.
        window.SlashFavorites.wire(this.modal, (payload) => this._applyFavorites(payload));

        // "more" reveals the full description in place. stopPropagation is
        // required: the button sits INSIDE .command-item, whose own click
        // handler above selects the command and closes the modal, so
        // without it asking to read more would run the command instead.
        this.modal.querySelectorAll('.command-more').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._toggleFullDescription(btn);
            });
        });

        // Live filter (Task 4) - typing filters the list; arrow keys and
        // Enter navigate/select the visible results. Escape is NOT
        // handled here - it bubbles to the document-level listener below,
        // which already closes the modal regardless of focus.
        const filterInput = this.modal.querySelector('#slash-command-filter');
        if (filterInput) {
            filterInput.addEventListener('input', () => {
                this.filter.apply(filterInput.value);
            });
            filterInput.addEventListener('keydown', (e) => {
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.filter.moveActive(1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.filter.moveActive(-1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    const target = this.filter.getEnterTarget();
                    if (target) this.selectCommand(target.command);
                }
            });
        }

        // Escape key to close
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this.isOpen) {
                this.close();
            }
        });
    }

    /**
     * Open the modal (creates it on first call)
     */
    open() {
        console.log('[SlashCommands] open() called');

        // Lazy initialization - create modal on first open
        if (!this.modal) {
            console.log('[SlashCommands] Modal does not exist yet, creating now...');
            this.createModal();
            this.attachEventListeners();
            console.log('[SlashCommands] Modal created and event listeners attached');
        }

        if (!this.modal) {
            console.error('[SlashCommands] Failed to create modal!');
            return;
        }

        // Reset any filter left over from a previous open, so reopening
        // always starts showing the full list.
        const filterInput = this.modal.querySelector('#slash-command-filter');
        if (filterInput) filterInput.value = '';
        this.filter.apply('');

        console.log('[SlashCommands] Adding .active class to show modal');
        this.modal.classList.add('active');
        this.isOpen = true;

        // Update button icon to X
        this.updateButtonIcon();
    }

    /**
     * Close the modal
     */
    close() {
        console.log('[SlashCommands] close() called');
        if (!this.modal) {
            console.log('[SlashCommands] No modal to close');
            return;
        }
        console.log('[SlashCommands] Removing .active class to hide modal');
        this.modal.classList.remove('active');
        this.isOpen = false;

        // Update button icon back to slash
        this.updateButtonIcon();
    }

    /**
     * Handle command selection
     */
    selectCommand(command) {
        if (this.onCommandSelect) {
            this.onCommandSelect(command);
        }
        this.close();

        // Focus terminal to open keyboard on mobile
        setTimeout(() => {
            const terminal = document.querySelector('.xterm-helper-textarea');
            if (terminal) {
                terminal.focus();
            }
        }, 100);
    }
}

// Export singleton instance
window.SlashCommandsModal = new SlashCommandsModal();
console.log('[SlashCommands Module] Exported as window.SlashCommandsModal');
