/**
 * Launchpad Module - Project selection UI with terminal aesthetic
 */

console.log('[Launchpad Module] Loading...');

class Launchpad {
    constructor() {
        this.launchpadScreen = null;
        this.projects = [];
    }

    /**
     * Initialize launchpad screen
     */
    init() {
        this.launchpadScreen = document.getElementById('launchpad-screen');
        this.renderLaunchpadUI();
        // Note: loadProjects() will be called by App.showLaunchpad()
    }

    /**
     * Load and display projects
     */
    async loadProjects() {
        try {
            this.projects = await window.API.getProjects();
            this.renderProjectList();
        } catch (error) {
            console.error('Launchpad: Failed to load projects:', error);
            this.showError('failed to load projects: ' + error.message);
        }
    }

    /**
     * Render launchpad UI structure
     */
    renderLaunchpadUI() {
        this.launchpadScreen.innerHTML = `
            <div class="launchpad-container">
                <div class="launchpad-header">☁️ Cloude Code Launcher</div>
                <div class="launchpad-prompt">select a project or create a new project</div>

                <div class="launchpad-section">
                    <div class="launchpad-section-title">► new project</div>
                    <button class="new-session-btn" id="new-session-btn">
                        <span>⚡</span>
                        <span>create new project</span>
                    </button>
                </div>

                <div class="launchpad-section" id="projects-section">
                    <div class="launchpad-section-title">► existing projects</div>
                    <div id="project-list" class="project-list">
                        <div class="launchpad-empty">loading projects...</div>
                    </div>
                </div>

                <div class="launchpad-section">
                    <div class="launchpad-section-title">► server management</div>
                    <button class="reset-server-btn" id="reset-server-btn">
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                            <path d="M13 8C13 10.7614 10.7614 13 8 13C5.23858 13 3 10.7614 3 8C3 5.23858 5.23858 3 8 3C9.87677 3 11.5 4.01207 12.3284 5.5" stroke="#d77757" stroke-width="1.5" stroke-linecap="round"/>
                            <path d="M12 2.5V5.5H9" stroke="#d77757" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <span>reset server</span>
                    </button>
                </div>

                <div class="launchpad-footer">
                    <a href="https://DrinkBlackMarket.com" target="_blank" rel="noopener noreferrer">
                        <svg version="1.1" xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 827 814">
                            <path d="M 399 0 C 407.91 0 416.82 0 426 0 C 426.33 135.63 426.66 271.26 427 411 C 434.618 399.649 609.473 128.537 618 115 C 623.377 115.567 639.725 123.44 640.891 125.816 C 641.048 128.963 453.608 418.363 448 427 C 456.264 422.984 679.016 303.482 680 303 C 683.164 304.101 691.507 315.724 691.953 316.398 C 692.347 316.993 452.689 452.704 450 456 C 451.044 456.081 520.653 476.354 526 478 C 525.715 486.257 523.179 493.875 519 501 C 515.688 502.397 442.202 479.4 426 474 C 426 489.84 426 505.68 426 522 C 417.09 522 408.18 522 399 522 C 398.67 506.16 398.34 490.32 398 474 C 390.373 476.347 308.499 502.028 305.633 501.866 C 303.451 500.709 296.607 482.74 297 478 C 298.107 477.671 371.612 454.981 374 454 C 372.855 453.413 138.231 328.154 131 324 C 131.362 318.049 141.707 304.675 143 303 C 148.977 304.654 374.147 426.136 377 427 C 367.051 411.432 183.948 128.542 184.196 125.953 C 185.641 122.443 202.468 115.478 207 115 C 207.49 115.777 207.49 115.777 207.991 116.57 C 211.17 121.608 396.858 409.287 398 411 C 398.33 275.37 398.66 139.74 399 0 Z" fill="#d77757" transform="matrix(0.999974, 0.007264, -0.007264, 0.999974, 0, 0)"/>
                            <path d="M 7 529 C 14.866 529.557 387.15 649.223 392.79 651.283 C 405.483 655.806 415.028 657.457 427.526 652.007 C 432.7 649.9 817.501 528.914 820 529 C 821.207 532.556 826.648 553.159 826 556 C 792.238 566.782 490.79 662.27 463 671 C 465.852 672.901 797.149 777.901 826 787 C 826.922 791.046 820.856 811.478 820 814 C 813.295 813.418 411.629 687.261 406 689 C 404.991 689.303 11 814 7 814 C 5.793 810.444 0.352 789.841 1 787 C 36.966 775.49 337.27 680.359 364 672 C 361.079 670.052 28.79 564.73 1 556 C -0.082 552.02 6.144 531.522 7 529 Z" fill="#d77757" transform="matrix(0.999974, 0.007264, -0.007264, 0.999974, -0.000025, -0.000024)"/>
                        </svg>
                    </a>
                </div>
            </div>
        `;

        // Event listeners
        document.getElementById('new-session-btn').addEventListener('click', () => {
            this.createNewSession();
        });

        document.getElementById('reset-server-btn').addEventListener('click', () => {
            this.resetServer();
        });

        // Note: loadProjects() will be called by App.showLaunchpad()
    }

    /**
     * Render project list
     */
    renderProjectList() {
        const projectListEl = document.getElementById('project-list');

        if (this.projects.length === 0) {
            projectListEl.innerHTML = `
                <div class="launchpad-empty">
                    no projects configured yet<br>
                    <small style="color: #666;">edit config.json to add projects</small>
                </div>
            `;
            return;
        }

        // Render projects
        projectListEl.innerHTML = this.projects.map((project, index) => {
            const description = project.description || 'no description';
            return `
                <div class="project-item" data-index="${index}" data-name="${project.name}">
                    <button class="project-delete-btn" data-name="${project.name}" title="Delete project">×</button>
                    <div class="project-name">» ${project.name}</div>
                    <div class="project-path">${project.path}</div>
                    <div class="project-description">${description}</div>
                </div>
            `;
        }).join('');

        // Add click handlers for project selection
        const projectItems = projectListEl.querySelectorAll('.project-item');
        projectItems.forEach(item => {
            item.addEventListener('click', (e) => {
                // Don't open project if clicking delete button
                if (e.target.classList.contains('project-delete-btn')) {
                    return;
                }
                const index = parseInt(item.dataset.index);
                this.selectProject(this.projects[index]);
            });
        });

        // Add click handlers for delete buttons
        const deleteButtons = projectListEl.querySelectorAll('.project-delete-btn');
        deleteButtons.forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation(); // Prevent project selection
                const projectName = btn.dataset.name;
                await this.deleteProject(projectName);
            });
        });
    }

    /**
     * Delete a project
     */
    async deleteProject(projectName) {
        try {
            // Show confirmation modal
            const confirmed = await this.showConfirmModal(
                'delete project',
                `are you sure you want to delete "${projectName}"?`,
                'this will only remove it from the launcher. the actual files will not be deleted.'
            );

            if (!confirmed) {
                return;
            }

            // Show loading state
            this.updateStatus(`deleting ${projectName}...`);

            // Delete project via API
            await window.API.deleteProject(projectName);

            console.log('Launchpad: Project deleted:', projectName);

            // Reload projects list
            await this.loadProjects();

            this.updateStatus('project deleted');

        } catch (error) {
            console.error('Launchpad: Failed to delete project:', error);
            this.showError('failed to delete project: ' + error.message);
        }
    }

    /**
     * Reset the server
     */
    async resetServer() {
        try {
            // Show confirmation modal
            const confirmed = await this.showConfirmModal(
                'reset server',
                'are you sure you want to reset the server?',
                'this will stop and restart the server. any active sessions will be terminated.'
            );

            if (!confirmed) {
                return;
            }

            // Show loading state
            this.updateStatus('resetting server...');

            // Call reset API
            await window.API.resetServer();

            console.log('Launchpad: Server reset initiated');

            // Show success message
            this.updateStatus('server reset initiated - reconnecting...');

            // Wait a moment for the server to restart, then reload the page
            setTimeout(() => {
                window.location.reload();
            }, 3000);

        } catch (error) {
            console.error('Launchpad: Failed to reset server:', error);
            this.showError('failed to reset server: ' + error.message);
        }
    }

    /**
     * Show confirmation modal
     * @param {string} title - Modal title
     * @param {string} message - Main message
     * @param {string} details - Additional details (optional)
     * @returns {Promise<boolean>} - True if confirmed, false if cancelled
     */
    showConfirmModal(title, message, details = null) {
        return new Promise((resolve) => {
            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            // Create modal content
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» ${title}</div>
                    <div class="modal-body">
                        <div class="modal-message">${message}</div>
                        ${details ? `<div class="modal-description">${details}</div>` : ''}
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="modal-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="modal-confirm">confirm</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            // Handle Escape key
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    document.body.removeChild(overlay);
                    resolve(false);
                }
            });

            // Handle confirm button
            confirmBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(true);
            });

            // Handle cancel button
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(false);
            });

            // Handle click outside modal
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    document.body.removeChild(overlay);
                    resolve(false);
                }
            });

            // Focus confirm button
            setTimeout(() => confirmBtn.focus(), 100);
        });
    }

    /**
     * Create new project with auto-generated workspace
     */
    async createNewSession() {
        console.log('Launchpad: Creating new project');

        try {
            // Show modal to get project details
            const projectDetails = await this.showProjectNameModal();

            if (!projectDetails) {
                console.log('Launchpad: Project creation cancelled');
                return; // User cancelled
            }

            // Show loading state
            this.updateStatus('creating new project...');

            // Create session with auto-generated path and template copying
            const session = await window.API.createSession({
                auto_start_claude: true,
                copy_templates: true
            });

            console.log('Launchpad: New project created:', session);

            // Save project to config with the actual path from the session
            try {
                await window.API.createProject({
                    name: projectDetails.name,
                    path: session.working_dir,
                    description: projectDetails.description || null
                });
                console.log('Launchpad: Project saved to config');
            } catch (error) {
                // If project already exists, that's ok - continue anyway
                if (!error.message.includes('already exists')) {
                    console.error('Launchpad: Failed to save project:', error);
                }
            }

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session }
            }));

        } catch (error) {
            console.error('Launchpad: Failed to create session:', error);

            // If session already exists, offer to connect or destroy
            if (error.message.includes('already running')) {
                if (confirm('A session is already running. Do you want to connect to it?\n\n(Click OK to connect, Cancel to destroy and create new)')) {
                    // Connect to existing session
                    this.connectToExistingSession();
                } else {
                    // Destroy and recreate
                    this.destroyAndCreateNew();
                }
            } else {
                this.showError('failed to create session: ' + error.message);
            }
        }
    }

    /**
     * Show modal to prompt for project name and description
     * @returns {Promise<{name: string, description: string}|null>} Project details or null if cancelled
     */
    showProjectNameModal() {
        return new Promise((resolve) => {
            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            // Create modal content
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">» name this project</div>
                    <div class="modal-body">
                        <div class="modal-input-group">
                            <label class="modal-label">project name</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="modal-project-name"
                                placeholder="e.g., My Awesome Project"
                                autocomplete="off"
                            />
                            <div class="modal-description">
                                give your project a memorable name. you can reconnect to it later from the launcher.
                            </div>
                        </div>
                        <div class="modal-input-group">
                            <label class="modal-label">description (optional)</label>
                            <input
                                type="text"
                                class="modal-input"
                                id="modal-project-description"
                                placeholder="e.g., Building an AI-powered chatbot"
                                autocomplete="off"
                            />
                            <div class="modal-description">
                                add a short description to help remember what this project is about.
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="modal-btn modal-btn-secondary" id="modal-cancel">cancel</button>
                        <button class="modal-btn modal-btn-primary" id="modal-confirm">create session</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            const nameInput = overlay.querySelector('#modal-project-name');
            const descInput = overlay.querySelector('#modal-project-description');
            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            // Focus name input
            setTimeout(() => nameInput.focus(), 100);

            // Handle Enter key on name input (moves to description)
            nameInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (nameInput.value.trim()) {
                        descInput.focus();
                    }
                }
            });

            // Handle Enter key on description input (submits)
            descInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const name = nameInput.value.trim();
                    if (name) {
                        const description = descInput.value.trim();
                        document.body.removeChild(overlay);
                        resolve({ name, description });
                    } else {
                        nameInput.focus();
                    }
                }
            });

            // Handle Escape key
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    document.body.removeChild(overlay);
                    resolve(null);
                }
            });

            // Handle confirm button
            confirmBtn.addEventListener('click', () => {
                const name = nameInput.value.trim();
                if (name) {
                    const description = descInput.value.trim();
                    document.body.removeChild(overlay);
                    resolve({ name, description });
                } else {
                    nameInput.focus();
                }
            });

            // Handle cancel button
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(null);
            });

            // Handle click outside modal
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    document.body.removeChild(overlay);
                    resolve(null);
                }
            });
        });
    }

    /**
     * Connect to existing session
     */
    async connectToExistingSession() {
        try {
            this.updateStatus('connecting to existing session...');
            const data = await window.API.getSession();
            const session = data.session || data;

            console.log('Launchpad: Connecting to existing session:', session);

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session }
            }));
        } catch (error) {
            console.error('Launchpad: Failed to get existing session:', error);
            this.showError('failed to connect: ' + error.message);
        }
    }

    /**
     * Destroy existing session and create new one
     */
    async destroyAndCreateNew() {
        try {
            this.updateStatus('destroying old session...');
            await window.API.destroySession();

            // Wait a moment, then create new
            setTimeout(() => this.createNewSession(), 500);
        } catch (error) {
            console.error('Launchpad: Failed to destroy session:', error);
            this.showError('failed to destroy session: ' + error.message);
        }
    }

    /**
     * Select and open existing project
     */
    async selectProject(project) {
        console.log('Launchpad: Selecting project:', project.name);

        try {
            // Show loading state
            this.updateStatus(`opening ${project.name}...`);

            // Create session with project path (no template copying for existing projects)
            const session = await window.API.createSession({
                working_dir: project.path,
                auto_start_claude: true,
                copy_templates: false
            });

            console.log('Launchpad: Project session created:', session);

            // Trigger session-created event
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: { session, project }
            }));

        } catch (error) {
            console.error('Launchpad: Failed to open project:', error);

            // If session already exists, offer to connect or destroy
            if (error.message.includes('already running')) {
                if (confirm('A session is already running. Do you want to connect to it?\n\n(Click OK to connect, Cancel to destroy and open this project)')) {
                    // Connect to existing session
                    this.connectToExistingSession();
                } else {
                    // Destroy and recreate with this project
                    this.destroyAndOpenProject(project);
                }
            } else {
                this.showError(`failed to open ${project.name}: ${error.message}`);
            }
        }
    }

    /**
     * Destroy existing session and open project
     */
    async destroyAndOpenProject(project) {
        try {
            this.updateStatus('destroying old session...');
            await window.API.destroySession();

            // Wait a moment, then open project
            setTimeout(() => this.selectProject(project), 500);
        } catch (error) {
            console.error('Launchpad: Failed to destroy session:', error);
            this.showError('failed to destroy session: ' + error.message);
        }
    }

    /**
     * Update status message
     */
    updateStatus(message) {
        const statusEl = document.getElementById('statusText');
        if (statusEl) {
            statusEl.setAttribute('data-status', message);
        }
        console.log('Launchpad:', message);
    }

    /**
     * Show error message
     */
    showError(message) {
        // For now, just log and use browser alert
        // Could be improved with a proper error UI element
        console.error('Launchpad Error:', message);
        alert(`Error: ${message}`);
    }
}

// Export singleton instance
window.Launchpad = new Launchpad();
console.log('[Launchpad Module] Exported as window.Launchpad:', window.Launchpad);
