/**
 * Terminal Module - Handles xterm.js terminal and WebSocket PTY connection
 */

console.log('[Terminal Module] Loading...');

class Terminal {
    constructor() {
        this.ws = null;
        this.term = null;
        this.fitAddon = null;
        this.sessionActive = false;

        // Auto-reconnect tracking
        this.reconnectAttempts = 0;
        this.reconnectTimeout = null;
        this.maxReconnectAttempts = 5;
        this.isReconnecting = false;

        // WebSocket keepalive
        this.keepaliveInterval = null;

        // Single-writer queue for PTY data
        this.queue = [];
        this.flushing = false;

        // Auto-scroll behavior
        this.autoScrollEnabled = true;
        this.resizeDebounceTimer = null;

        // UI elements
        this.destroySessionBtn = null;
        this.statusEl = null;
        this.sessionInfoEl = null;
    }

    /**
     * Initialize terminal
     */
    async init() {
        console.log('Terminal: Initializing xterm.js');

        this.destroySessionBtn = document.getElementById('destroySessionBtn');
        this.statusEl = document.getElementById('statusText');
        this.sessionInfoEl = document.getElementById('sessionInfo');

        // Add destroy session handler
        this.destroySessionBtn.addEventListener('click', () => this.destroySession());

        // Wait for xterm.js to load from CDN
        await this.waitForXterm();

        this.initTerminal();
    }

    /**
     * Wait for xterm.js CDN scripts to load
     */
    async waitForXterm() {
        const maxWait = 10000; // 10 seconds max
        const checkInterval = 50; // Check every 50ms
        const startTime = Date.now();

        while (Date.now() - startTime < maxWait) {
            // Check if all xterm.js modules are loaded (use window.Terminal to avoid shadowing)
            const terminalLoaded = typeof window.Terminal !== 'undefined' && window.Terminal !== Terminal;
            const fitLoaded = typeof FitAddon !== 'undefined' && typeof FitAddon.FitAddon !== 'undefined';
            const webglLoaded = typeof WebglAddon !== 'undefined' && typeof WebglAddon.WebglAddon !== 'undefined';
            const unicodeLoaded = typeof Unicode11Addon !== 'undefined' && typeof Unicode11Addon.Unicode11Addon !== 'undefined';

            if (terminalLoaded && fitLoaded && webglLoaded && unicodeLoaded) {
                console.log('Terminal: xterm.js loaded', {
                    windowTerminal: typeof window.Terminal,
                    FitAddon: typeof FitAddon?.FitAddon,
                    WebglAddon: typeof WebglAddon?.WebglAddon,
                    Unicode11Addon: typeof Unicode11Addon?.Unicode11Addon
                });
                return;
            }

            await new Promise(resolve => setTimeout(resolve, checkInterval));
        }

        console.error('Terminal: xterm.js failed to load', {
            windowTerminal: typeof window.Terminal,
            FitAddon: typeof FitAddon,
            WebglAddon: typeof WebglAddon,
            Unicode11Addon: typeof Unicode11Addon
        });
        throw new Error('xterm.js failed to load from CDN');
    }

    /**
     * Initialize xterm.js terminal
     */
    initTerminal() {
        console.log('Terminal: Creating xterm Terminal instance', {
            windowTerminal: typeof window.Terminal,
            localTerminal: typeof Terminal,
            isXtermTerminal: window.Terminal !== Terminal
        });

        // Use window.Terminal to get xterm.js Terminal, not our wrapper class
        const XTerminal = window.Terminal;

        this.term = new XTerminal({
            cursorBlink: true,
            fontSize: 14,
            fontFamily: '"SF Mono", monospace',
            fontWeight: 'normal',
            fontWeightBold: 'bold',
            allowTransparency: false,
            theme: {
                background: '#1e1e1e',
                foreground: '#d4d4d4',
                cursor: '#d4d4d4',
                black: '#000000',
                red: '#cd3131',
                green: '#0dbc79',
                yellow: '#e5e510',
                blue: '#2472c8',
                magenta: '#bc3fbc',
                cyan: '#11a8cd',
                white: '#e5e5e5',
                brightBlack: '#666666',
                brightRed: '#f14c4c',
                brightGreen: '#23d18b',
                brightYellow: '#f5f543',
                brightBlue: '#3b8eea',
                brightMagenta: '#d670d6',
                brightCyan: '#29b8db',
                brightWhite: '#ffffff'
            },
            allowProposedApi: true,
            convertEol: false,
            scrollback: 10000,
            windowsMode: false
        });

        console.log('Terminal: Terminal instance created', {
            term: this.term,
            hasLoadAddon: typeof this.term?.loadAddon,
            allMethods: this.term ? Object.getOwnPropertyNames(Object.getPrototypeOf(this.term)).filter(m => typeof this.term[m] === 'function').slice(0, 20) : []
        });

        if (typeof this.term.loadAddon !== 'function') {
            console.error('Terminal methods available:', Object.getOwnPropertyNames(Object.getPrototypeOf(this.term)));
            throw new Error(`Terminal instance has no loadAddon method. Available methods: ${Object.getOwnPropertyNames(Object.getPrototypeOf(this.term)).join(', ')}`);
        }

        this.fitAddon = new FitAddon.FitAddon();
        this.term.loadAddon(this.fitAddon);

        // Load WebGL renderer
        try {
            const webglAddon = new WebglAddon.WebglAddon();
            this.term.loadAddon(webglAddon);
        } catch (e) {
            console.warn('WebGL addon not available, using canvas renderer', e);
        }

        // Load Unicode 11 addon
        try {
            const unicode11Addon = new Unicode11Addon.Unicode11Addon();
            this.term.loadAddon(unicode11Addon);
            this.term.unicode.activeVersion = '11';
        } catch (e) {
            console.warn('Unicode11 addon not available', e);
        }

        this.term.open(document.getElementById('terminal'));

        // Handle terminal input
        this.term.onData(data => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                // Convert special symbols for mobile keyboard shortcuts
                if (data === '¥') {
                    data = '\n';  // Yen = Newline
                } else if (data === '€') {
                    data = '\t';  // Euro = Tab
                } else if (data === '￡' || data === '£') {
                    data = '\x1b[Z';  // Pound = Shift+Tab
                }
                // Send input as binary frame
                this.ws.send(new TextEncoder().encode(data));
            }
        });

        // Handle window resize with debouncing
        window.addEventListener('resize', () => {
            if (this.resizeDebounceTimer) {
                clearTimeout(this.resizeDebounceTimer);
            }
            this.resizeDebounceTimer = setTimeout(() => {
                if (this.fitAddon && this.term) {
                    this.fitAddon.fit();
                    this.sendResize();
                }
            }, 100);
        });

        // Setup scroll event listener for auto-scroll detection
        this.setupScrollListener();

        // Auto-scroll terminal to bottom on focus (mobile keyboard fix)
        const terminalElement = document.getElementById('terminal');
        if (terminalElement) {
            terminalElement.addEventListener('focus', () => {
                const container = document.querySelector('.terminal-container');
                if (container) {
                    setTimeout(() => {
                        container.scrollIntoView({ behavior: 'smooth', block: 'end' });
                    }, 100);
                }
            }, true);

            terminalElement.addEventListener('click', () => {
                const container = document.querySelector('.terminal-container');
                if (container && window.innerWidth <= 768) {
                    setTimeout(() => {
                        container.scrollIntoView({ behavior: 'smooth', block: 'end' });
                    }, 100);
                }
            });
        }

        this.term.writeln('\x1b[1;32mCloude Code Terminal\x1b[0m');
        this.term.writeln('');
        this.term.writeln('\x1b[2;37mKeyboard shortcuts:\x1b[0m');
        this.term.writeln('  ¥  = Newline (Enter)');
        this.term.writeln('  €  = Tab');
        this.term.writeln('  £  = Shift+Tab');
        this.term.writeln('');
        this.term.writeln('Waiting for session...\n');
    }

    /**
     * Enqueue PTY data for writing
     */
    enqueue(bytes) {
        this.queue.push(bytes);
        if (!this.flushing) {
            this.flushing = true;
            requestAnimationFrame(() => this.flush());
        }
    }

    /**
     * Flush queued PTY data
     */
    flush() {
        let total = 0;
        for (const c of this.queue) total += c.length;
        const merged = new Uint8Array(total);
        let o = 0;
        while (this.queue.length) {
            const c = this.queue.shift();
            merged.set(c, o);
            o += c.length;
        }
        this.term.write(merged, () => {
            this.flushing = false;

            // Auto-scroll to bottom if enabled
            if (this.autoScrollEnabled && this.term) {
                this.term.scrollToBottom();
            }

            if (this.queue.length) requestAnimationFrame(() => this.flush());
        });
    }

    /**
     * Setup scroll event listener to detect manual scrolling
     */
    setupScrollListener() {
        // Wait for terminal to be fully initialized
        setTimeout(() => {
            const viewport = document.querySelector('.xterm-viewport');
            if (viewport) {
                let scrollTimeout = null;
                viewport.addEventListener('scroll', () => {
                    // Debounce scroll events
                    if (scrollTimeout) clearTimeout(scrollTimeout);

                    scrollTimeout = setTimeout(() => {
                        if (!this.term) return;

                        // Check if user scrolled to bottom
                        const isAtBottom = this.isScrolledToBottom(viewport);

                        if (isAtBottom) {
                            // User scrolled back to bottom, re-enable auto-scroll
                            this.autoScrollEnabled = true;
                        } else {
                            // User scrolled up, disable auto-scroll
                            this.autoScrollEnabled = false;
                        }
                    }, 100);
                });
            }
        }, 500);
    }

    /**
     * Check if viewport is scrolled to bottom
     */
    isScrolledToBottom(viewport) {
        if (!viewport) return true;
        const threshold = 10; // pixels from bottom
        return (viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) <= threshold;
    }

    /**
     * Scroll to bottom and re-enable auto-scroll (for D-pad)
     */
    scrollToBottomAndEnableAutoScroll() {
        if (this.term) {
            this.autoScrollEnabled = true;
            this.term.scrollToBottom();
        }
    }

    /**
     * Send resize event to server
     */
    sendResize() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN && this.term) {
            this.ws.send(JSON.stringify({
                type: 'pty_resize',
                cols: this.term.cols,
                rows: this.term.rows
            }));
        }
    }

    /**
     * Send key to terminal (for D-pad)
     * @param {string} keyData - ANSI escape sequence or character
     */
    sendKeyToTerminal(keyData) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(new TextEncoder().encode(keyData));
        } else {
            console.warn('Terminal: WebSocket not open, cannot send key');
        }
    }

    /**
     * Connect to session
     * @param {object} session - Session data
     */
    async connectToSession(session) {
        console.log('Terminal: Connecting to session:', session.id);

        this.sessionActive = true;
        this.sessionInfoEl.textContent =
            `Session: ${session.id} | PID: ${session.pty_pid}`;

        // Enable destroy button
        this.destroySessionBtn.disabled = false;

        this.term.writeln('\x1b[1;32m[Session created - connecting to WebSocket...]\x1b[0m');

        // Connect WebSocket
        setTimeout(() => this.connectWebSocket(), 500);

        // Load tunnels
        this.loadTunnels();
    }

    /**
     * Wait for fonts and layout to be ready
     */
    async waitForFontsAndLayout(container) {
        if (document.fonts?.ready) {
            try { await document.fonts.ready; } catch {}
        }
        const t0 = performance.now();
        while ((container.offsetWidth|0) === 0 || (container.offsetHeight|0) === 0) {
            if (performance.now() - t0 > 2000) break;
            await new Promise(r => setTimeout(r, 16));
        }
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
    }

    /**
     * Connect WebSocket with auth token
     */
    async connectWebSocket() {
        if (this.isReconnecting) {
            this.stopReconnecting();
            return;
        }

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            console.log('Terminal: Already connected');
            return;
        }

        this.updateStatus('Connecting to terminal...');

        // Wait for fonts and layout
        const container = document.getElementById('terminal');
        await this.waitForFontsAndLayout(container);

        // Fit terminal with multiple attempts to ensure proper sizing
        this.fitAddon.fit();
        await new Promise(resolve => setTimeout(resolve, 50));
        this.fitAddon.fit();

        console.log('Terminal size:', this.term.cols, 'x', this.term.rows);

        // Get WebSocket URL with token
        const wsURL = window.API.getWebSocketURL();
        console.log('Terminal: Connecting to WebSocket:', wsURL.replace(/token=[^&]+/, 'token=***'));

        this.ws = new WebSocket(wsURL);
        this.ws.binaryType = 'arraybuffer';
        this.setupWebSocketHandlers();
    }

    /**
     * Setup WebSocket event handlers
     */
    setupWebSocketHandlers() {
        if (!this.ws) return;

        this.ws.onopen = () => {
            console.log('Terminal: WebSocket connected');

            // Reset reconnect state
            this.reconnectAttempts = 0;
            this.isReconnecting = false;
            if (this.reconnectTimeout) {
                clearTimeout(this.reconnectTimeout);
                this.reconnectTimeout = null;
            }

            this.updateStatus('Connected', 'connected');

            if (this.term) {
                this.term.writeln('\x1b[1;32m[Connected to PTY terminal]\x1b[0m\n');
            }

            // Send initial resize
            this.sendResize();

            // Start keepalive ping
            if (this.keepaliveInterval) {
                clearInterval(this.keepaliveInterval);
            }
            this.keepaliveInterval = setInterval(() => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({type: "ping"}));
                }
            }, 30000);
        };

        this.ws.onmessage = (event) => {
            // Handle binary frames (PTY data)
            if (event.data instanceof ArrayBuffer) {
                this.enqueue(new Uint8Array(event.data));
                return;
            }

            // Handle JSON control messages
            try {
                const message = JSON.parse(event.data);
                this.handleWebSocketMessage(message);
            } catch (error) {
                console.error('Terminal: Failed to parse message:', error);
            }
        };

        this.ws.onerror = (error) => {
            console.error('Terminal: WebSocket error:', error);
            this.updateStatus('WebSocket error', 'error');
        };

        this.ws.onclose = () => {
            console.log('Terminal: WebSocket closed');
            this.ws = null;

            // Stop keepalive
            if (this.keepaliveInterval) {
                clearInterval(this.keepaliveInterval);
                this.keepaliveInterval = null;
            }

            if (this.term) {
                this.term.writeln('\n\x1b[1;31m[Disconnected from terminal]\x1b[0m');
            }

            // Attempt auto-reconnect if session still active
            if (this.sessionActive && !this.isReconnecting) {
                this.attemptReconnect();
            }
        };
    }

    /**
     * Handle WebSocket messages
     */
    handleWebSocketMessage(message) {
        const type = message.type;

        if (type === 'log') {
            if (this.term && message.content) {
                this.term.writeln(`\x1b[1;33m${message.content}\x1b[0m`);
            }
        } else if (type === 'tunnel_created') {
            if (this.term) {
                this.term.writeln(`\x1b[1;36m[Tunnel created: ${message.tunnel.public_url}]\x1b[0m`);
            }
            this.loadTunnels();
        } else if (type === 'error') {
            if (this.term) {
                this.term.writeln(`\x1b[1;31m[Error: ${message.message}]\x1b[0m`);
            }
        } else if (type === 'pong') {
            console.log('Terminal: Received pong');
        }
    }

    /**
     * Attempt to reconnect WebSocket
     */
    attemptReconnect() {
        if (!this.sessionActive || this.reconnectAttempts >= this.maxReconnectAttempts) {
            if (this.reconnectAttempts >= this.maxReconnectAttempts) {
                console.log('Terminal: Max reconnect attempts reached');
                this.updateStatus('Connection failed', 'error');
                if (this.term) {
                    this.term.writeln('\n\x1b[1;31m[Reconnection failed after ' + this.maxReconnectAttempts + ' attempts]\x1b[0m');
                }
            }
            this.stopReconnecting();
            return;
        }

        this.isReconnecting = true;
        this.reconnectAttempts++;

        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), 16000);

        console.log(`Terminal: Reconnect attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts} in ${delay}ms`);
        this.updateStatus('Reconnecting...');

        if (this.term) {
            this.term.writeln(`\n\x1b[1;33m[Reconnecting... Attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts}]\x1b[0m`);
        }

        this.reconnectTimeout = setTimeout(() => {
            this.connectWebSocket();
        }, delay);
    }

    /**
     * Stop reconnection attempts
     */
    stopReconnecting() {
        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }
        this.isReconnecting = false;
        this.reconnectAttempts = 0;
    }

    /**
     * Load tunnels
     */
    async loadTunnels() {
        try {
            const tunnels = await window.API.getTunnels();
            const container = document.getElementById('tunnelsContainer');
            const list = document.getElementById('tunnelsList');

            if (tunnels.length > 0) {
                container.style.display = 'block';
                list.innerHTML = tunnels.map(tunnel => `
                    <div class="tunnel-item">
                        <strong>Port ${tunnel.port}:</strong>
                        <a href="${tunnel.public_url}" target="_blank">${tunnel.public_url}</a>
                    </div>
                `).join('');
            } else {
                container.style.display = 'none';
            }
        } catch (error) {
            console.error('Terminal: Error loading tunnels:', error);
        }
    }

    /**
     * Destroy session
     */
    async destroySession() {
        try {
            this.updateStatus('Destroying session...');

            await window.API.destroySession();

            this.sessionActive = false;
            this.stopReconnecting();

            if (this.ws) {
                this.ws.close();
                this.ws = null;
            }

            this.destroySessionBtn.disabled = true;
            this.sessionInfoEl.textContent = 'No active session';

            if (this.term) {
                this.term.clear();
                this.term.writeln('\x1b[1;31mSession destroyed\x1b[0m\n');
            }

            // Trigger session-destroyed event
            window.dispatchEvent(new CustomEvent('session-destroyed'));

        } catch (error) {
            console.error('Terminal: Error destroying session:', error);
            this.updateStatus('Error: ' + error.message, 'error');
        }
    }

    /**
     * Insert text into terminal without pressing Enter
     * Used for slash commands
     */
    insertText(text) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('Terminal: Cannot insert text - WebSocket not connected');
            return;
        }

        // Send text to terminal without newline
        this.ws.send(new TextEncoder().encode(text));

        console.log('Terminal: Inserted text:', text);
    }

    /**
     * Update status
     */
    updateStatus(text, className) {
        if (this.statusEl) {
            this.statusEl.setAttribute('data-status', text);
            this.statusEl.className = 'status ' + className;
        }
    }
}

// Export singleton instance
window.TerminalController = new Terminal();
console.log('[Terminal Module] Exported as window.TerminalController:', window.TerminalController);
