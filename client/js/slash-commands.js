/**
 * Slash Commands Module
 * Handles the slash command quick-access modal
 */

console.log('[SlashCommands Module] Loading...');

// Complete list of Claude Code slash commands with descriptions
const ALL_SLASH_COMMANDS = [
    // Session & Context
    { command: '/clear', category: 'Session & Context', description: 'Clear history and free context' },
    { command: '/compact', category: 'Session & Context', description: 'Compact conversation, optionally focused' },
    { command: '/context', category: 'Session & Context', description: 'Visualize context usage as a grid' },
    { command: '/rewind', category: 'Session & Context', description: 'Roll conversation/code back to a prior point' },
    { command: '/resume', category: 'Session & Context', description: 'Resume by ID/name or open picker' },
    { command: '/branch', category: 'Session & Context', description: 'Branch the conversation at this point' },
    { command: '/rename', category: 'Session & Context', description: 'Rename the session' },
    { command: '/export', category: 'Session & Context', description: 'Export conversation as plain text' },
    { command: '/copy', category: 'Session & Context', description: 'Copy the Nth-latest assistant response to clipboard' },
    { command: '/btw', category: 'Session & Context', description: 'Ask a side question without polluting the main thread' },
    { command: '/exit', category: 'Session & Context', description: 'Exit Claude Code' },

    // Models & Effort
    { command: '/model', category: 'Models & Effort', description: 'Select/change model' },
    { command: '/effort', category: 'Models & Effort', description: 'Set reasoning effort (low / medium / high / max / auto)' },
    { command: '/fast', category: 'Models & Effort', description: 'Toggle fast mode' },

    // Planning & Execution
    { command: '/plan', category: 'Planning & Execution', description: 'Enter plan mode, optionally with a starter task' },
    { command: '/ultraplan', category: 'Planning & Execution', description: 'Draft a plan in an ultraplan session, review, then execute' },
    { command: '/loop', category: 'Planning & Execution', description: 'Run a prompt on a schedule while session is open' },
    { command: '/schedule', category: 'Planning & Execution', description: 'Create/manage routines' },
    { command: '/batch', category: 'Planning & Execution', description: 'Decompose large changes into units and spawn background agents per unit' },
    { command: '/simplify', category: 'Planning & Execution', description: 'Parallel review agents scan recent changes and apply fixes' },
    { command: '/debug', category: 'Planning & Execution', description: 'Enable debug logging and troubleshoot' },
    { command: '/security-review', category: 'Planning & Execution', description: 'Scan the branch diff for vulnerabilities' },
    { command: '/tasks', category: 'Planning & Execution', description: 'Manage background tasks' },
    { command: '/diff', category: 'Planning & Execution', description: 'Interactive diff viewer for uncommitted and per-turn changes' },

    // Files, Tools & Permissions
    { command: '/add-dir', category: 'Files, Tools & Permissions', description: 'Add a working directory for file access' },
    { command: '/permissions', category: 'Files, Tools & Permissions', description: 'Manage allow/ask/deny rules' },
    { command: '/sandbox', category: 'Files, Tools & Permissions', description: 'Toggle sandbox mode (supported platforms)' },
    { command: '/hooks', category: 'Files, Tools & Permissions', description: 'View hook configurations' },
    { command: '/agents', category: 'Files, Tools & Permissions', description: 'Manage sub-agent configs' },
    { command: '/skills', category: 'Files, Tools & Permissions', description: 'List skills' },
    { command: '/plugin', category: 'Files, Tools & Permissions', description: 'Manage plugins' },
    { command: '/reload-plugins', category: 'Files, Tools & Permissions', description: 'Hot-reload plugins' },
    { command: '/mcp', category: 'Files, Tools & Permissions', description: 'Manage MCP servers and OAuth' },

    // Configuration & UI
    { command: '/config', category: 'Configuration & UI', description: 'Open Settings' },
    { command: '/status', category: 'Configuration & UI', description: 'Settings > Status tab (works mid-response)' },
    { command: '/theme', category: 'Configuration & UI', description: 'Change color theme' },
    { command: '/color', category: 'Configuration & UI', description: 'Set prompt bar color' },
    { command: '/statusline', category: 'Configuration & UI', description: 'Configure status line' },
    { command: '/keybindings', category: 'Configuration & UI', description: 'Edit keybindings config' },
    { command: '/terminal-setup', category: 'Configuration & UI', description: 'Configure Shift+Enter and other terminal shortcuts' },
    { command: '/memory', category: 'Configuration & UI', description: 'Edit CLAUDE.md files, manage auto-memory' },
    { command: '/init', category: 'Configuration & UI', description: 'Initialize project with a CLAUDE.md' },
    { command: '/ide', category: 'Configuration & UI', description: 'IDE integrations' },
    { command: '/chrome', category: 'Configuration & UI', description: 'Claude in Chrome settings' },

    // Integrations & Remote
    { command: '/install-github-app', category: 'Integrations & Remote', description: 'Set up Claude GitHub Actions' },
    { command: '/install-slack-app', category: 'Integrations & Remote', description: 'Install Claude Slack app' },
    { command: '/web-setup', category: 'Integrations & Remote', description: 'Connect GitHub for Claude Code on the web' },
    { command: '/remote-control', category: 'Integrations & Remote', description: 'Expose session for remote control from claude.ai' },
    { command: '/remote-env', category: 'Integrations & Remote', description: 'Configure default remote env for --remote web sessions' },
    { command: '/teleport', category: 'Integrations & Remote', description: 'Pull a web session into the current terminal' },
    { command: '/autofix-pr', category: 'Integrations & Remote', description: 'Spawn a web session that auto-fixes PR CI failures' },
    { command: '/desktop', category: 'Integrations & Remote', description: 'Continue session in desktop app (macOS/Windows)' },
    { command: '/mobile', category: 'Integrations & Remote', description: 'QR code for mobile app' },
    { command: '/voice', category: 'Integrations & Remote', description: 'Toggle push-to-talk dictation' },

    // Enterprise Auth
    { command: '/setup-bedrock', category: 'Enterprise Auth', description: 'Amazon Bedrock config' },
    { command: '/setup-vertex', category: 'Enterprise Auth', description: 'Google Vertex AI config' },

    // Account, Plan & Billing
    { command: '/login', category: 'Account, Plan & Billing', description: 'Sign in / switch accounts' },
    { command: '/logout', category: 'Account, Plan & Billing', description: 'Sign out' },
    { command: '/upgrade', category: 'Account, Plan & Billing', description: 'Upgrade plan (Pro/Max only)' },
    { command: '/usage', category: 'Account, Plan & Billing', description: 'Plan limits and rate-limit status' },
    { command: '/cost', category: 'Account, Plan & Billing', description: 'Token usage stats' },
    { command: '/extra-usage', category: 'Account, Plan & Billing', description: 'Configure extra usage past rate limits' },
    { command: '/privacy-settings', category: 'Account, Plan & Billing', description: 'Privacy settings (Pro/Max only)' },
    { command: '/passes', category: 'Account, Plan & Billing', description: 'Share a free week with friends (if eligible)' },
    { command: '/stats', category: 'Account, Plan & Billing', description: 'Daily usage, streaks, model preferences' },
    { command: '/insights', category: 'Account, Plan & Billing', description: 'Report on project areas and interaction patterns' },
    { command: '/team-onboarding', category: 'Account, Plan & Billing', description: 'Generate an onboarding guide from your last 30 days' },

    // Help & Reference
    { command: '/help', category: 'Help & Reference', description: 'Get command assistance' },
    { command: '/doctor', category: 'Help & Reference', description: 'Diagnose install/settings; press f to let Claude fix issues' },
    { command: '/release-notes', category: 'Help & Reference', description: 'Interactive changelog picker' },
    { command: '/powerup', category: 'Help & Reference', description: 'Interactive feature lessons' },
    { command: '/claude-api', category: 'Help & Reference', description: 'Load Claude API reference material' },
    { command: '/feedback', category: 'Help & Reference', description: 'Report issues or feedback' },
    { command: '/stickers', category: 'Help & Reference', description: 'Request stickers' }
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
