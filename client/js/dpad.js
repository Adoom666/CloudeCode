/**
 * DPad Module - Virtual D-pad for mobile terminal navigation
 * Provides touch-friendly arrow keys, enter, and directional controls
 */

class DPad {
    constructor() {
        this.isOpen = false;
        this.floatingButton = null;
        this.overlay = null;

        // ANSI escape sequences for terminal navigation
        this.keys = {
            UP: '\x1b[A',
            DOWN: '\x1b[B',
            RIGHT: '\x1b[C',
            LEFT: '\x1b[D',
            ENTER: '\r',
            ESC: '\x1b',
            TAB: '\t',
            SHIFT_TAB: '\x1b[Z',
            SCROLL_BOTTOM: null  // Special action - no key code
        };
    }

    /**
     * Initialize the D-pad (only on mobile)
     */
    init() {
        if (!this.isMobile()) {
            console.log('DPad: Desktop detected, not initializing');
            return;
        }

        console.log('DPad: Mobile detected, initializing');
        this.createFloatingButton();
    }

    /**
     * Check if device is mobile
     */
    isMobile() {
        return window.innerWidth <= 768 ||
               ('ontouchstart' in window) ||
               (navigator.maxTouchPoints > 0);
    }

    /**
     * Create the floating button
     */
    createFloatingButton() {
        this.floatingButton = document.createElement('button');
        this.floatingButton.id = 'dpad-float-btn';
        this.floatingButton.className = 'dpad-float-button';
        this.floatingButton.innerHTML = `
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                <path d="M8 4H12V8H16V12H12V16H8V12H4V8H8V4Z" stroke="#d77757" stroke-width="1.5" stroke-linejoin="round"/>
                <circle cx="10" cy="10" r="1" fill="#d77757"/>
            </svg>
        `;

        this.floatingButton.addEventListener('click', (e) => {
            e.stopPropagation();
            if (this.isOpen) {
                this.close();
            } else {
                this.open();
            }
        });

        document.body.appendChild(this.floatingButton);
    }

    /**
     * Open the D-pad overlay
     */
    open() {
        if (this.isOpen) return;

        this.isOpen = true;
        this.createOverlay();

        // Change floating button to X
        if (this.floatingButton) {
            this.floatingButton.innerHTML = `
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                    <path d="M6 6L18 18M18 6L6 18" stroke="#d77757" stroke-width="2" stroke-linecap="round"/>
                </svg>
            `;
        }
    }

    /**
     * Close the D-pad overlay
     */
    close() {
        if (!this.isOpen) return;

        this.isOpen = false;

        if (this.overlay) {
            this.overlay.remove();
            this.overlay = null;
        }

        // Change floating button back to D-pad icon
        if (this.floatingButton) {
            this.floatingButton.innerHTML = `
                <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                    <path d="M8 4H12V8H16V12H12V16H8V12H4V8H8V4Z" stroke="#d77757" stroke-width="1.5" stroke-linejoin="round"/>
                    <circle cx="10" cy="10" r="1" fill="#d77757"/>
                </svg>
            `;
        }
    }

    /**
     * Create the D-pad overlay
     */
    createOverlay() {
        this.overlay = document.createElement('div');
        this.overlay.className = 'dpad-overlay';

        this.overlay.innerHTML = `
            <div class="dpad-container">
                <button class="dpad-esc dpad-key" data-key="ESC">
                    <span style="font-size: 14px; font-weight: bold; color: #d77757;">ESC</span>
                </button>

                <button class="dpad-shift-tab dpad-key" data-key="SHIFT_TAB">
                    <svg width="30" height="30" viewBox="0 0 30 30" fill="none">
                        <path d="M15 5L13 12L8 10L12 15L5 17L12 19L10 24L15 20L20 24L18 19L25 17L18 15L22 10L17 12L15 5Z" fill="#d77757" stroke="#d77757" stroke-width="1" stroke-linejoin="round"/>
                    </svg>
                </button>

                <button class="dpad-tab dpad-key" data-key="TAB">
                    <svg width="30" height="30" viewBox="0 0 30 30" fill="none">
                        <path d="M15 8C12 8 10 10 10 12C10 13 10.5 14 11 14.5C10 15 9 16 9 17.5C9 19.5 11 21 13 21C13 21 13 22 15 22C17 22 17 21 17 21C19 21 21 19.5 21 17.5C21 16 20 15 19 14.5C19.5 14 20 13 20 12C20 10 18 8 15 8Z" stroke="#d77757" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        <circle cx="13" cy="11.5" r="0.8" fill="#d77757"/>
                        <circle cx="17" cy="11.5" r="0.8" fill="#d77757"/>
                        <path d="M11 17C11 17 11.5 18 13 18.5M19 17C19 17 18.5 18 17 18.5" stroke="#d77757" stroke-width="1.2" stroke-linecap="round"/>
                    </svg>
                </button>

                <div class="dpad-grid">
                    <div class="dpad-row">
                        <div class="dpad-spacer"></div>
                        <button class="dpad-key" data-key="UP">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M16 10L16 22M10 16L16 10L22 16" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                        </button>
                        <div class="dpad-spacer"></div>
                    </div>

                    <div class="dpad-row">
                        <button class="dpad-key" data-key="LEFT">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M22 16L10 16M16 10L10 16L16 22" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                        </button>
                        <button class="dpad-key dpad-enter" data-key="ENTER">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M10 16h12M22 16l-4 4M22 16l-4-4" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                        </button>
                        <button class="dpad-key" data-key="RIGHT">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M10 16L22 16M16 10L22 16L16 22" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                        </button>
                    </div>

                    <div class="dpad-row">
                        <div class="dpad-spacer"></div>
                        <button class="dpad-key" data-key="DOWN">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M16 22L16 10M10 16L16 22L22 16" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                        </button>
                        <div class="dpad-spacer"></div>
                    </div>

                    <div class="dpad-row">
                        <div class="dpad-spacer"></div>
                        <button class="dpad-key dpad-scroll-bottom" data-key="SCROLL_BOTTOM">
                            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                                <path d="M10 10L16 16L22 10" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                                <path d="M10 14L16 20L22 14" stroke="#d77757" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                                <path d="M8 24H24" stroke="#d77757" stroke-width="2.5" stroke-linecap="round"/>
                            </svg>
                        </button>
                        <div class="dpad-spacer"></div>
                    </div>
                </div>
            </div>
        `;

        // Add event listeners
        this.overlay.addEventListener('click', (e) => {
            if (e.target === this.overlay) {
                this.close();
            }
        });

        // Key buttons
        this.overlay.querySelectorAll('.dpad-key').forEach(btn => {
            btn.addEventListener('touchstart', (e) => {
                e.preventDefault();
                e.stopPropagation();
                btn.classList.add('active');

                const key = btn.dataset.key;
                this.sendKey(key);
            });

            btn.addEventListener('touchend', (e) => {
                e.preventDefault();
                e.stopPropagation();
                btn.classList.remove('active');
            });

            // Also support click for testing on desktop
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const key = btn.dataset.key;
                this.sendKey(key);
            });
        });

        document.body.appendChild(this.overlay);
    }

    /**
     * Send key to terminal
     */
    sendKey(keyName) {
        if (!window.TerminalController) {
            console.error('DPad: TerminalController not found');
            return;
        }

        // Handle scroll to bottom button (special action - no key code)
        if (keyName === 'SCROLL_BOTTOM') {
            console.log('DPad: Scroll to bottom button pressed');
            window.TerminalController.scrollToBottomAndEnableAutoScroll();
            return;
        }

        // Get key code for regular keys
        const keyCode = this.keys[keyName];

        if (!keyCode) {
            console.error('DPad: Unknown key:', keyName);
            return;
        }

        // Send key to terminal
        console.log('DPad: Sending key:', keyName);
        window.TerminalController.sendKeyToTerminal(keyCode);
    }

    /**
     * Show the floating button
     */
    show() {
        if (this.floatingButton) {
            this.floatingButton.style.display = 'flex';
        }
    }

    /**
     * Hide the floating button
     */
    hide() {
        if (this.floatingButton) {
            this.floatingButton.style.display = 'none';
        }
        this.close();
    }

    /**
     * Destroy the D-pad
     */
    destroy() {
        this.close();

        if (this.floatingButton) {
            this.floatingButton.remove();
            this.floatingButton = null;
        }
    }
}

// Export as singleton
window.DPad = new DPad();
