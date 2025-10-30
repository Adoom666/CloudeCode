/**
 * Slash Commands Module
 * Handles the slash command quick-access modal
 */

console.log('[SlashCommands Module] Loading...');

// Complete list of Claude Code slash commands with descriptions
const ALL_SLASH_COMMANDS = [
    // Workflow Management
    { command: '/clear', category: 'Workflow', description: 'Clear conversation history' },
    { command: '/compact', category: 'Workflow', description: 'Compress conversation with optional focus' },
    { command: '/rewind', category: 'Workflow', description: 'Undo changes and revert to earlier states' },

    // Configuration & Settings
    { command: '/config', category: 'Config', description: 'Open settings interface' },
    { command: '/model', category: 'Config', description: 'Select AI model' },
    { command: '/permissions', category: 'Config', description: 'View/update access controls' },
    { command: '/settings', category: 'Config', description: 'Configuration options' },

    // Account & Authentication
    { command: '/login', category: 'Account', description: 'Switch accounts' },
    { command: '/logout', category: 'Account', description: 'Sign out' },
    { command: '/status', category: 'Account', description: 'Display version and connectivity info' },

    // Development Tools
    { command: '/sandbox', category: 'Dev Tools', description: 'Enable isolated bash execution' },
    { command: '/review', category: 'Dev Tools', description: 'Request code review' },
    { command: '/cost', category: 'Dev Tools', description: 'Show token usage' },
    { command: '/usage', category: 'Dev Tools', description: 'Display plan limits' },
    { command: '/help', category: 'Dev Tools', description: 'Get command assistance' },

    // Project Setup
    { command: '/init', category: 'Project', description: 'Initialize project with CLAUDE.md guide' },
    { command: '/add-dir', category: 'Project', description: 'Add working directories' },
    { command: '/agents', category: 'Project', description: 'Manage AI subagents' },

    // Utilities
    { command: '/doctor', category: 'Utilities', description: 'Check installation health' },
    { command: '/mcp', category: 'Utilities', description: 'Manage MCP server connections' },
    { command: '/memory', category: 'Utilities', description: 'Edit memory files' },
    { command: '/vim', category: 'Utilities', description: 'Enter vim mode' },
    { command: '/bug', category: 'Utilities', description: 'Report issues' },
    { command: '/pr_comments', category: 'Utilities', description: 'View PR feedback' },
    { command: '/terminal-setup', category: 'Utilities', description: 'Configure key bindings' },
    { command: '/context', category: 'Utilities', description: 'View context information' },
    { command: '/hooks', category: 'Utilities', description: 'Manage hooks' },
    { command: '/resume', category: 'Utilities', description: 'Resume previous session' }
];

class SlashCommandsModal {
    constructor() {
        this.commonCommands = [];
        this.modal = null;
        this.button = null;
        this.isOpen = false;
        this.onCommandSelect = null;
    }

    /**
     * Initialize the slash commands modal
     * Note: Does NOT create the modal - that happens lazily on first open()
     */
    async init(onCommandSelect) {
        console.log('[SlashCommands] Initializing (NOT creating modal yet)');
        this.onCommandSelect = onCommandSelect;

        // Fetch common commands from API
        try {
            const response = await window.API.getCommonCommands();
            this.commonCommands = response.commands || [];
            console.log('[SlashCommands] Fetched', this.commonCommands.length, 'common commands');
        } catch (error) {
            console.error('[SlashCommands] Failed to fetch common commands:', error);
            // Fallback to defaults
            this.commonCommands = [
                '/agents', '/clear', '/compact', '/context',
                '/hooks', '/mcp', '/resume', '/rewind', '/usage'
            ];
        }

        // Modal will be created lazily on first open() call
        console.log('[SlashCommands] Initialization complete (modal will be created on demand)');

        // Create the floating button
        this.createButton();
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
                            <button class="modal-close" aria-label="Close modal">&times;</button>
                        </div>
                        <div class="modal-body">
                            <div class="common-commands-section">
                                <div class="common-commands-grid" id="common-commands-grid">
                                    ${this.renderCommonCommands()}
                                </div>
                            </div>
                            <div class="all-commands-section">
                                <h3>all commands</h3>
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
    }

    /**
     * Render common commands as buttons
     */
    renderCommonCommands() {
        console.log('[SlashCommands] renderCommonCommands() called with', this.commonCommands.length, 'commands:', this.commonCommands);

        if (!this.commonCommands || this.commonCommands.length === 0) {
            console.warn('[SlashCommands] No common commands to render!');
            return '<div style="color: #858585; padding: 12px; text-align: center;">No common commands configured</div>';
        }

        return this.commonCommands.map(cmd => {
            const cmdInfo = ALL_SLASH_COMMANDS.find(c => c.command === cmd);
            const description = cmdInfo ? cmdInfo.description : '';
            console.log('[SlashCommands] Rendering button for:', cmd, 'description:', description);
            return `
                <button class="command-button" data-command="${cmd}" title="${description}">
                    ${cmd}
                </button>
            `;
        }).join('');
    }

    /**
     * Render all commands grouped by category
     */
    renderAllCommands() {
        const categories = {};

        // Group commands by category
        ALL_SLASH_COMMANDS.forEach(cmd => {
            if (!categories[cmd.category]) {
                categories[cmd.category] = [];
            }
            categories[cmd.category].push(cmd);
        });

        // Render each category
        let html = '';
        for (const [category, commands] of Object.entries(categories)) {
            html += `
                <div class="command-category">
                    <h4 class="category-title">${category}</h4>
                    ${commands.map(cmd => `
                        <div class="command-item" data-command="${cmd.command}">
                            <span class="command-name">${cmd.command}</span>
                            <span class="command-description">${cmd.description}</span>
                        </div>
                    `).join('')}
                </div>
            `;
        }

        return html;
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

        // Overlay click to close
        const overlay = this.modal.querySelector('.modal-overlay');
        if (overlay) {
            overlay.addEventListener('click', () => this.close());
        }

        // Common command buttons
        const commandButtons = this.modal.querySelectorAll('.command-button');
        commandButtons.forEach(btn => {
            btn.addEventListener('click', () => {
                const command = btn.dataset.command;
                this.selectCommand(command);
            });
        });

        // All commands items
        const commandItems = this.modal.querySelectorAll('.command-item');
        commandItems.forEach(item => {
            item.addEventListener('click', () => {
                const command = item.dataset.command;
                this.selectCommand(command);
            });
        });

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
