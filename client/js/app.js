// Main app bootstrap — extracted from index.html for CSP compliance (script-src 'self').
/**
 * App Controller - Manages application state and screen transitions
 */
class AppController {
    constructor() {
        this.currentScreen = null;
        this.logoutBtn = null;
        this.destroyBtn = null;
        // Health poller state. Poll every 15s against /health so the
        // top-right status dot reflects server reachability on the
        // auth + launchpad screens. The terminal screen manages the
        // same dot via its WS updateStatus() calls, so the poller
        // yields whenever currentScreen === 'terminal'.
        this._healthPollerInterval = null;
    }

    /**
     * Initialize application
     */
    async init() {
        console.log('App: Initializing');

        this.logoutBtn = document.getElementById('logoutBtn');
        this.destroyBtn = document.getElementById('destroySessionBtn');

        // Setup event listeners
        this.setupEventListeners();

        // Initialize auth module (always needed first)
        window.Auth.init();

        // Kick off server health polling before auth resolves — the
        // /health endpoint is unauthenticated, so the dot works on the
        // auth screen too.
        this._startHealthPoller();

        // Check if user is authenticated
        if (window.Auth.isAuthenticated()) {
            console.log('App: User has token, verifying...');
            const isValid = await window.Auth.verifyToken();
            if (isValid) {
                this.showLaunchpad();
            } else {
                console.log('App: Token invalid, showing auth');
                this.showAuth();
            }
        } else {
            console.log('App: No token, showing auth');
            this.showAuth();
        }
    }

    /**
     * Start the server-health poller. Idempotent — safe to call more
     * than once. Fires an initial probe immediately, then every 15s.
     */
    _startHealthPoller() {
        if (this._healthPollerInterval) return;
        this._healthPollerInterval = setInterval(() => this._pollHealth(), 15000);
        this._pollHealth();
    }

    /**
     * Probe GET /health and paint the top-right status dot.
     *
     * States:
     *   - green (.connected): HTTP 200
     *   - red (.error):       network error, timeout, or non-2xx
     *   - orange (default):   initial state before first probe
     *
     * Yields to the terminal screen's WS updateStatus() by returning
     * early when currentScreen === 'terminal' — otherwise the 15s
     * tick would clobber the live WS status (e.g. "Connected").
     */
    async _pollHealth() {
        if (this.currentScreen === 'terminal') return;
        const statusEl = document.getElementById('statusText');
        if (!statusEl) return;
        try {
            const r = await fetch('/health', { method: 'GET', cache: 'no-store' });
            if (r.ok) {
                statusEl.className = 'status connected';
                statusEl.setAttribute('data-status', 'server OK');
            } else {
                statusEl.className = 'status error';
                statusEl.setAttribute('data-status', `server error · HTTP ${r.status}`);
            }
        } catch (err) {
            statusEl.className = 'status error';
            statusEl.setAttribute('data-status', `server unreachable · ${err && err.message ? err.message : err}`);
        }
    }

    /**
     * Setup event listeners
     */
    setupEventListeners() {
        // Auth events
        window.addEventListener('authenticated', () => {
            console.log('App: User authenticated');
            this.showLaunchpad();
        });

        window.addEventListener('auth-required', () => {
            console.log('App: Auth required');
            this.showAuth();
        });

        window.addEventListener('logged-out', () => {
            console.log('App: User logged out');
            this.showAuth();
        });

        // Session events. The `detail` payload may include adopt-path
        // extras (`initialScrollbackB64`, `fifoStartOffset`) when the
        // launchpad dispatched after adopting an external session —
        // forward the whole thing so showTerminal() can plumb to the
        // terminal controller's connectToSession() opts.
        window.addEventListener('session-created', (e) => {
            console.log('App: Session created', e.detail);
            this.showTerminal(e.detail.session, {
                initialScrollbackB64: e.detail.initialScrollbackB64,
                fifoStartOffset: e.detail.fifoStartOffset,
            });
        });

        window.addEventListener('session-destroyed', () => {
            console.log('App: Session destroyed');
            this.showLaunchpad();
        });

        // Title click - navigate back to launchpad (only from terminal)
        const appTitle = document.getElementById('appTitle');
        appTitle.addEventListener('click', () => {
            if (this.currentScreen === 'terminal') {
                console.log('App: Title clicked, navigating to launchpad');
                this.showLaunchpad();
            }
        });
    }

    /**
     * Show auth screen
     */
    showAuth() {
        console.log('App: Showing auth screen');
        this.hideAllScreens();
        document.getElementById('auth-screen').classList.add('active');
        this.logoutBtn.classList.add('hidden');
        this.destroyBtn.classList.add('hidden');
        this.currentScreen = 'auth';
    }

    /**
     * Show launchpad screen
     */
    showLaunchpad() {
        console.log('App: Showing launchpad screen');
        this.hideAllScreens();
        document.getElementById('launchpad-screen').classList.add('active');
        this.logoutBtn.classList.remove('hidden');
        this.destroyBtn.classList.add('hidden');
        this.currentScreen = 'launchpad';

        // Hide D-pad on launchpad
        if (window.DPad) {
            window.DPad.hide();
        }

        // Hide slash command button on launchpad
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.hide();
        }

        // Initialize launchpad if first time
        if (!window.Launchpad.launchpadScreen) {
            window.Launchpad.init();
        }

        // Reload projects
        window.Launchpad.loadProjects();
    }

    /**
     * Show terminal screen
     * @param {object} session - Session data from the backend
     * @param {object} [opts]
     * @param {string} [opts.initialScrollbackB64] - Adopt-path: base64
     *   scrollback bytes to paint into xterm before the WS opens.
     * @param {number} [opts.fifoStartOffset] - Adopt-path: fifo byte
     *   offset the server's tailer will start from. Passed through for
     *   symmetry/logging; not directly consumed by the client.
     */
    async showTerminal(session, opts = {}) {
        console.log('App: Showing terminal screen');
        this.hideAllScreens();
        document.getElementById('terminal-screen').classList.add('active');
        this.logoutBtn.classList.remove('hidden');
        this.destroyBtn.classList.remove('hidden');
        this.currentScreen = 'terminal';

        // Initialize terminal if first time
        if (!window.TerminalController.term) {
            await window.TerminalController.init();
        }

        // Initialize D-pad (mobile only)
        if (window.DPad && !window.DPad.floatingButton) {
            window.DPad.init();
        }

        // Show D-pad on terminal screen
        if (window.DPad) {
            window.DPad.show();
        }

        // Initialize slash commands modal
        if (window.SlashCommandsModal && !window.SlashCommandsModal.button) {
            await window.SlashCommandsModal.init((command) => {
                // Insert command into terminal without Enter
                window.TerminalController.insertText(command);
            });
        }

        // Show slash command button on terminal screen
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.show();
        }

        // Connect terminal to session. Adopt-path opts (scrollback,
        // fifo offset) are forwarded through — a plain new-session
        // create leaves them undefined and connectToSession treats
        // that as a normal (non-adopt) path.
        window.TerminalController.connectToSession(session, opts);
    }

    /**
     * Return to an ALREADY-ACTIVE terminal session without creating or
     * adopting anything. Used by the launchpad's active-session banner
     * when the user clicks "return to terminal" after navigating away
     * via the logo.
     *
     * The screen-transition side of this mirrors showTerminal() exactly
     * (so D-pad/slash-commands/header buttons land in the same state),
     * but the terminal-controller side calls reconnectToExistingSession
     * instead of connectToSession — the backend is already alive and a
     * POST /sessions would either error (single-session invariant) or
     * silently birth a new unrelated pane.
     *
     * @param {object} session - Session object (from GET /sessions).
     */
    async returnToExistingTerminal(session) {
        console.log('App: Returning to existing terminal', session && session.id);
        this.hideAllScreens();
        document.getElementById('terminal-screen').classList.add('active');
        this.logoutBtn.classList.remove('hidden');
        this.destroyBtn.classList.remove('hidden');
        this.currentScreen = 'terminal';

        // First-time init if the user never hit showTerminal() this page load
        // (e.g. refreshed directly onto launchpad while session was running).
        if (!window.TerminalController.term) {
            await window.TerminalController.init();
        }
        if (window.DPad && !window.DPad.floatingButton) {
            window.DPad.init();
        }
        if (window.DPad) {
            window.DPad.show();
        }
        if (window.SlashCommandsModal && !window.SlashCommandsModal.button) {
            await window.SlashCommandsModal.init((command) => {
                window.TerminalController.insertText(command);
            });
        }
        if (window.SlashCommandsModal) {
            window.SlashCommandsModal.show();
        }

        window.TerminalController.reconnectToExistingSession(session);
    }

    /**
     * Hide all screens
     */
    hideAllScreens() {
        document.querySelectorAll('.screen').forEach(screen => {
            screen.classList.remove('active');
        });
    }

    /**
     * Logout
     */
    async logout() {
        // Show confirmation modal
        const confirmed = await this.showConfirmModal(
            'logout',
            'are you sure you want to logout?',
            'any active session will be destroyed.'
        );

        if (confirmed) {
            // Destroy active session if exists
            if (window.TerminalController.sessionActive) {
                try {
                    await window.TerminalController.destroySession();
                } catch (error) {
                    console.error('App: Error destroying session during logout:', error);
                }
            }

            // Logout
            window.Auth.logout();
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
}

// Create app instance
const App = new AppController();
// Expose on window so other modules (launchpad active-session banner,
// future deep-link targets) can call App.returnToExistingTerminal
// without re-wiring via custom events.
window.App = App;

// Initialize on load
window.addEventListener('load', () => {
    App.init();
    // Item 9: kick off deep-link router AFTER App.init() so the
    // auth state is already being resolved. router.js listens
    // for the `authenticated` event to deliver stashed targets.
    if (window.Router && typeof window.Router.init === 'function') {
        window.Router.init();
    }
});
