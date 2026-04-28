/**
 * Terminal Module - Handles xterm.js terminal and WebSocket PTY connection
 */

console.log('[Terminal Module] Loading...');

/**
 * Default xterm palette — used if window.Themes hasn't initialized yet
 * (e.g. /api/v1/themes failed AND no synchronous fallback ran). Phase 4-5:
 * the actual theme assigned to xterm comes from
 *   Themes.getActiveGlobal()?.xterm ?? DEFAULT_XTERM_THEME
 * at construction time, then the registry's xtermThemeChange listener
 * swaps it on subsequent applyGlobal()/applySession()/clearSession() calls.
 *
 * Keep these values in lock-step with the Claude fallback in registry.js
 * and the :root block in client/css/styles.css. If one drifts the others
 * should follow on the same commit.
 */
const DEFAULT_XTERM_THEME = {
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
};

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

        // Track last-sent dims so we only log + ship when they actually
        // change. Multiple event sources (window.resize + visualViewport +
        // ResizeObserver + orientationchange) can all fire for a single
        // physical layout change; dedupe at the sendResize gate.
        this.lastSentCols = null;
        this.lastSentRows = null;

        // ResizeObserver tracking the xterm container. Listener-lifetime is
        // tied to the Terminal object; cleaned up in destroy paths.
        this._resizeObserver = null;

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

        // Phase 4-5: theme drawn from registry. If Themes hasn't initialized
        // yet (registry init is post-auth) we fall back to DEFAULT_XTERM_THEME.
        // The `xtermThemeChange` subscription below picks up subsequent
        // applyGlobal/applySession/clearSession calls and swaps the palette
        // live without re-creating the Terminal.
        const initialXtermTheme =
            (window.Themes && window.Themes.getActiveGlobal && window.Themes.getActiveGlobal()?.xterm)
            || DEFAULT_XTERM_THEME;

        this.term = new XTerminal({
            cursorBlink: true,
            fontSize: 14,
            fontFamily: '"SF Mono", monospace',
            fontWeight: 'normal',
            fontWeightBold: 'bold',
            allowTransparency: false,
            theme: initialXtermTheme,
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

        // Load WebGL renderer (hardened against context loss).
        //
        // iOS Safari (and any GPU under memory pressure) can drop the WebGL
        // context at any time. Without an onContextLoss handler the xterm
        // viewport silently goes black and stays that way for the rest of
        // the session. The recovery path is documented by the xterm.js
        // maintainers since 2021:
        //   1. dispose() the addon — it cannot recover the lost context
        //   2. xterm transparently falls back to its built-in DOM renderer
        //      (the renderer in use when no canvas/webgl addon is loaded)
        //
        // We don't auto-reload a fresh WebglAddon here: a context-loss
        // event implies system pressure, and re-creating the GL context
        // is what got us into trouble in the first place. The DOM renderer
        // is slower but stable, which is the right tradeoff under pressure.
        // A page reload (user-initiated) is the clean path back to WebGL.
        try {
            this._webglAddon = new WebglAddon.WebglAddon();
            this.term.loadAddon(this._webglAddon);
            this._webglAddon.onContextLoss(() => {
                console.warn('Terminal: WebGL context lost — disposing addon, falling back to DOM renderer');
                try { this._webglAddon.dispose(); } catch (_) { /* idempotent */ }
                this._webglAddon = null;
            });
        } catch (e) {
            console.warn('Terminal: WebGL addon unavailable — using DOM renderer', e);
            this._webglAddon = null;
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

        // Phase 4-5: subscribe to theme palette changes from the registry.
        // applyGlobal() / applySession() / clearSession() all funnel through
        // here. xterm.js (with WebglAddon since 2021) listens to its own
        // optionsChanged event and re-uploads the glyph atlas automatically
        // — we do NOT need to call term.refresh() preemptively. If stale
        // paint is observed empirically we add an explicit refresh here,
        // but the spec calls out the YAGNI on this and current xterm
        // versions handle it cleanly.
        if (window.Themes && typeof window.Themes.onXtermThemeChange === 'function') {
            this._unsubscribeXtermTheme = window.Themes.onXtermThemeChange((newXtermTheme) => {
                if (!this.term || !newXtermTheme) return;
                try {
                    this.term.options.theme = newXtermTheme;
                } catch (e) {
                    console.warn('Terminal: failed to apply xterm theme', e);
                }
            });
        }

        // Wire Shift+Enter interceptor. Handler body lives in
        // _applyKeyHandlers() so we can re-attach after term.reset()
        // (xterm wipes the custom key handler during core reset on
        // session swap, which would otherwise leave Shift+Enter dead).
        this._applyKeyHandlers();

        // IMG-PASTE — wire image-paste pipeline. Both the paste listener
        // (DOM event on #terminal container) and the mobile attach button
        // are attached to the document/container, NOT to xterm's custom
        // handler slot, so term.reset() during session swap does not wipe
        // them — single attachment in initTerminal() is sufficient.
        this._applyPasteHandler();
        this._applyImageAttachButton();

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

        // ---- Dynamic resize pipeline ----
        //
        // All four sources funnel into a single 100ms debounced callback:
        //   window.resize            - desktop viewport / browser window
        //   orientationchange        - mobile device rotation
        //   visualViewport.resize    - mobile keyboard popup / browser chrome
        //                              show+hide / pinch-zoom. Provides more
        //                              accurate viewport dims than window
        //                              on iOS Safari.
        //   ResizeObserver           - ANY layout change of the xterm
        //                              container (sidebar collapse, split
        //                              view, CSS transitions, font load).
        //
        // Single debounce gate means redundant fires during one layout
        // change collapse to a single fit()+sendResize() call, and the
        // sendResize dedup further suppresses duplicate frames when the
        // cell grid hasn't actually changed. Graceful degradation: if any
        // API is unavailable (old browser) the remaining listeners still
        // catch their share of events.
        const scheduleResize = (source) => {
            if (this.resizeDebounceTimer) {
                clearTimeout(this.resizeDebounceTimer);
            }
            this.resizeDebounceTimer = setTimeout(() => {
                if (this.fitAddon && this.term) {
                    this.fitAddon.fit();
                    this.sendResize(source);
                }
            }, 100);
        };

        window.addEventListener('resize', () => scheduleResize('window.resize'));
        window.addEventListener('orientationchange', () => scheduleResize('orientationchange'));

        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', () => scheduleResize('visualViewport.resize'));
        }

        const termContainer = document.getElementById('terminal');
        if (termContainer && typeof ResizeObserver !== 'undefined') {
            try {
                this._resizeObserver = new ResizeObserver(() => scheduleResize('ResizeObserver'));
                this._resizeObserver.observe(termContainer);
            } catch (e) {
                console.warn('Terminal: ResizeObserver setup failed', e);
            }
        }

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
     * Attach the Shift+Enter custom key handler to the current xterm
     * instance. Called from initTerminal() on first boot and from every
     * term.reset() site on session swap — xterm's core reset wipes the
     * custom key event handler slot, so without re-attachment Shift+Enter
     * silently goes back to default (submit) behavior for the rest of
     * the session's life.
     *
     * Payload: 2-byte ESC+CR (`\x1b\r`) — the VSCode / Alacritty
     * convention documented by Claude Code's /terminal-setup guide for
     * "insert newline without submitting". Claude Code's Ink input
     * parser recognizes ESC+CR as Meta+Enter without requiring kitty
     * keyboard protocol negotiation (which CSI u `\x1b[13;2u` depends
     * on, and which our node-pty/tmux stack does not reliably forward).
     */
    _applyKeyHandlers() {
        if (!this.term) return;
        this.term.attachCustomKeyEventHandler((ev) => {
            if (ev.type === 'keydown' && ev.key === 'Enter' && ev.shiftKey &&
                !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
                ev.preventDefault();
                ev.stopPropagation();
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    const bytes = new Uint8Array([0x1b, 0x0d]);  // \x1b\r — VSCode/Alacritty pattern from Claude Code's /terminal-setup docs
                    console.log('[SHIFT-ENTER] sending ESC+CR (\\x1b\\r), bytes:', bytes);
                    this.ws.send(bytes);
                }
                return false;  // swallow the event so xterm doesn't also emit \r
            }
            return true;  // all other keys pass through to default handling
        });
    }

    /**
     * IMG-PASTE — desktop clipboard paste interceptor.
     *
     * Listens on the #terminal container in capture phase so we see the
     * paste BEFORE xterm's internal handler. Iterates clipboardData.items
     * looking for the first ``kind === 'file'`` item with an ``image/*``
     * type. If found, we suppress xterm's default text paste, upload the
     * blob, and inject the returned absolute path with a trailing space
     * (NOT a newline — preserves Claude Code's native UX where the user
     * keeps typing the prompt). If no image item is present we let the
     * event fall through to xterm's text-paste path unchanged.
     *
     * Capture phase + stopPropagation matter: xterm registers its own
     * paste listener on the same container in bubble phase; without
     * capture-first interception the text-paste path would still fire
     * for an image (which xterm renders as the literal text "[object
     * File]" garbage in the prompt buffer).
     */
    _applyPasteHandler() {
        const container = document.getElementById('terminal');
        if (!container) return;
        container.addEventListener('paste', async (e) => {
            const items = (e.clipboardData && e.clipboardData.items) || [];
            let imageItem = null;
            for (const item of items) {
                if (item.kind === 'file' && item.type && item.type.startsWith('image/')) {
                    imageItem = item;
                    break;
                }
            }
            if (!imageItem) return;

            e.preventDefault();
            e.stopPropagation();
            const blob = imageItem.getAsFile();
            if (!blob) return;

            await this._uploadAndInjectImage(blob, imageItem.type);
        }, true);
    }

    /**
     * IMG-PASTE — mobile / iOS attach-button wire-up.
     *
     * iOS Safari does NOT reliably fire ``paste`` events for image data
     * outside focused contenteditable elements, so we surface an explicit
     * 📎 button (gated to ``pointer: coarse`` via CSS). On tap we try
     * ``navigator.clipboard.read()`` first as a bonus path — it can
     * succeed on platforms where the implicit paste event would not —
     * and fall through to the hidden file input on any failure (denied
     * permission, no clipboard image, API absent, etc.).
     *
     * The file input has ``accept="image/*,image/heic,image/heif"`` so
     * the OS picker offers both Photos library + Files; the server
     * rejects HEIC at validation time with a "convert to PNG/JPEG"
     * message (intentional v1 scope).
     */
    _applyImageAttachButton() {
        const btn = document.getElementById('cloude-image-attach-button');
        const input = document.getElementById('cloude-image-attach-input');
        if (!btn || !input) return;

        btn.addEventListener('click', async () => {
            try {
                if (navigator.clipboard && typeof navigator.clipboard.read === 'function') {
                    const items = await navigator.clipboard.read();
                    for (const item of items) {
                        if (item.types && item.types.includes('image/png')) {
                            const blob = await item.getType('image/png');
                            await this._uploadAndInjectImage(blob, 'image/png');
                            return;
                        }
                    }
                }
            } catch (err) {
                console.log('[IMG-PASTE] clipboard.read unavailable, falling back to file picker:', err && err.message);
            }
            input.click();
        });

        input.addEventListener('change', async () => {
            const file = input.files && input.files[0];
            if (!file) return;
            await this._uploadAndInjectImage(file, file.type || 'image/jpeg');
            input.value = '';
        });
    }

    /**
     * IMG-PASTE — shared upload + path-injection routine.
     *
     * Trailing SPACE (not Enter) is intentional: Claude Code's CLI
     * auto-attaches any absolute image path that appears in its prompt
     * buffer once the user submits, so we want the path to land in the
     * buffer with a space separator and let the user keep typing their
     * prompt. Auto-Enter would submit a path-only message and waste the
     * round-trip.
     */
    async _uploadAndInjectImage(blob, mimeType) {
        this._showStatusPill('Uploading image...', 'info');
        try {
            const result = await window.API.uploadImage(blob, mimeType);
            this.insertText(result.path + ' ');
            this._showStatusPill('Pasted: ' + result.filename, 'success');
        } catch (err) {
            console.error('[IMG-PASTE] upload failed', err);
            this._showStatusPill('Upload failed: ' + (err && err.message ? err.message : 'unknown'), 'error');
        }
    }

    /**
     * IMG-PASTE — inline status pill.
     *
     * Lazy-creates the pill the first time it is needed. The element is
     * positioned ``fixed`` near the top center via CSS, so its DOM
     * insertion point is irrelevant. Auto-dismisses after 3s for
     * info/success and 5s for errors so the user has time to read the
     * failure reason.
     */
    _showStatusPill(message, kind) {
        let pill = document.getElementById('cloude-status-pill');
        if (!pill) {
            pill = document.createElement('div');
            pill.id = 'cloude-status-pill';
            pill.className = 'cloude-status-pill';
            document.body.appendChild(pill);
        }
        pill.textContent = message;
        pill.dataset.kind = kind || 'info';
        pill.classList.add('visible');
        if (this._statusPillTimeout) clearTimeout(this._statusPillTimeout);
        this._statusPillTimeout = setTimeout(() => {
            pill.classList.remove('visible');
        }, kind === 'error' ? 5000 : 3000);
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
     * Send resize event to server.
     *
     * Dedups on (cols, rows) so the four-source funnel doesn't ship
     * redundant frames when a layout event fires but the cell grid
     * didn't actually change (zoom-neutral pinch, background chrome
     * collapse that stays within the same cell count, etc.).
     *
     * @param {string} source - Origin tag for the [TERM-RESIZE] log line.
     *   Values: 'window.resize' | 'orientationchange' |
     *   'visualViewport.resize' | 'ResizeObserver' | 'handshake' |
     *   'ws.onopen'. Defaults to 'unknown' for callers that don't tag.
     * @param {boolean} force - Bypass the dedup gate. Used by the
     *   request_dims handshake so the server always gets a fresh frame
     *   on reconnect even if the grid happens to match the last send.
     */
    sendResize(source = 'unknown', force = false) {
        if (!(this.ws && this.ws.readyState === WebSocket.OPEN && this.term)) return;

        const cols = this.term.cols;
        const rows = this.term.rows;

        if (!force && cols === this.lastSentCols && rows === this.lastSentRows) {
            return;
        }

        this.ws.send(JSON.stringify({
            type: 'pty_resize',
            cols,
            rows,
        }));

        console.log(`[TERM-RESIZE] ${cols}x${rows} source=${source}`);

        this.lastSentCols = cols;
        this.lastSentRows = rows;
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
     * @param {object} [opts]
     * @param {string} [opts.initialScrollbackB64] - Base64-encoded bytes
     *   captured server-side from `tmux capture-pane` for the external
     *   session being adopted. Painted into xterm BEFORE the WS opens so
     *   the server's WS tailer can seek the fifo to `fifoStartOffset`
     *   without risking a tear or duplicate output. Ignored on normal
     *   (non-adopt) session creates.
     * @param {number} [opts.fifoStartOffset] - Byte offset into the
     *   pipe-pane fifo that the server's tailer should begin streaming
     *   from. Client doesn't consume this directly; it's the server's
     *   contract — we accept it for symmetry and logging only.
     */
    async connectToSession(session, opts = {}) {
        const { initialScrollbackB64 = '', fifoStartOffset = null } = opts;
        console.log('Terminal: Connecting to session:', session.id, {
            adopted: !!initialScrollbackB64,
            fifoStartOffset,
        });

        // If a prior session was active, tear it down cleanly before painting the new one.
        // Prevents stale scrollback, stacked "[Session created...]" banners, and ghost
        // WebSocket readers competing for the same backend FIFO.
        if (this.ws) {
            try {
                // Flag so our onclose handler doesn't trigger a reconnect loop.
                this._intentionalClose = true;
                this.ws.close();
            } catch (e) {
                console.warn('Terminal: error closing prior WS:', e);
            }
            this.ws = null;
        }
        // Reset the xterm buffer and cursor. term.reset() clears scrollback +
        // alt-buffer + wraps state; term.clear() only clears the visible screen.
        // We want reset() so the VT parser starts fresh for the new session.
        if (this.term) {
            try {
                this.term.reset();
            } catch (e) {
                console.warn('Terminal: xterm reset failed:', e);
            }
            // term.reset() wipes xterm's custom key handler slot.
            // Re-attach so Shift+Enter continues to emit ESC+CR for the
            // new session instead of silently falling back to default \r.
            this._applyKeyHandlers();
        }
        this._currentSession = null;
        this.sessionActive = false;
        this.reconnectAttempts = 0;

        // Stash session on the controller so other modules (launchpad
        // self-adopt filter, debug) can introspect without refetching.
        this._currentSession = session;

        this.sessionActive = true;
        this.sessionInfoEl.textContent =
            `Session: ${session.id} | PID: ${session.pty_pid}`;

        // Enable destroy button
        this.destroySessionBtn.disabled = false;

        // Adopt path: paint server-captured scrollback into xterm BEFORE
        // the WS opens. Must be synchronous relative to the WS connect so
        // the VT parser state is correct when the first streamed byte
        // arrives at fifoStartOffset. atob() decodes to a binary string
        // whose charCodeAt values are the raw bytes — we MUST NOT run
        // these through TextDecoder, which would mangle non-UTF8 ANSI
        // escape bytes. xterm.write() accepts Uint8Array directly and
        // feeds the parser without re-encoding.
        if (initialScrollbackB64) {
            try {
                const bin = atob(initialScrollbackB64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) {
                    bytes[i] = bin.charCodeAt(i) & 0xff;
                }
                this.term.write(bytes);
                console.log(`Terminal: painted ${bytes.length} bytes of adopt scrollback`);
            } catch (e) {
                // Non-fatal — if the b64 is malformed we still want the
                // session to come up. The user will just miss the pre-
                // adopt scrollback, not the live stream.
                console.warn('Terminal: scrollback paint failed, continuing without it:', e);
            }
        } else {
            this.term.writeln('\x1b[1;32m[Session created - connecting to WebSocket...]\x1b[0m');
        }

        // Connect WebSocket
        setTimeout(() => this.connectWebSocket(), 500);

        // Load tunnels
        this.loadTunnels();
    }

    /**
     * Reconnect to an ALREADY-ACTIVE backend session.
     *
     * Used when the user returns to the launchpad while a session is
     * running and clicks "return to terminal". The backend is already
     * alive — we must NOT POST /sessions (would try to create) or
     * POST /sessions/adopt (would re-pipe-pane the tmux session). We
     * just re-open the WebSocket against the existing backend.
     *
     * Contract parity with connectToSession(): stashes the session on
     * the controller, marks it active, wires the destroy button, then
     * opens the WS on the same delay so the UI transition settles first.
     *
     * Safe to call multiple times. If a live WS is already open, we
     * do nothing beyond re-painting the status (the server stream is
     * unaffected). If xterm already holds state from the previous
     * session view, we leave it alone — returning to an existing
     * session should feel seamless, not like a reload.
     *
     * @param {object} session - Session object (shape matches what
     *   GET /sessions returns under the ``session`` key).
     */
    reconnectToExistingSession(session) {
        console.log('Terminal: Reconnecting to existing session:', session && session.id);

        // If a prior session was active, tear it down cleanly before painting the new one.
        // Prevents stale scrollback, stacked "[Session created...]" banners, and ghost
        // WebSocket readers competing for the same backend FIFO.
        if (this.ws) {
            try {
                // Flag so our onclose handler doesn't trigger a reconnect loop.
                this._intentionalClose = true;
                this.ws.close();
            } catch (e) {
                console.warn('Terminal: error closing prior WS:', e);
            }
            this.ws = null;
        }
        // Reset the xterm buffer and cursor. term.reset() clears scrollback +
        // alt-buffer + wraps state; term.clear() only clears the visible screen.
        // We want reset() so the VT parser starts fresh for the new session.
        if (this.term) {
            try {
                this.term.reset();
            } catch (e) {
                console.warn('Terminal: xterm reset failed:', e);
            }
            // term.reset() wipes xterm's custom key handler slot.
            // Re-attach so Shift+Enter continues to emit ESC+CR for the
            // new session instead of silently falling back to default \r.
            this._applyKeyHandlers();
        }
        this._currentSession = null;
        this.sessionActive = false;
        this.reconnectAttempts = 0;

        // Stash so launchpad self-adopt filter + debug can introspect.
        this._currentSession = session;
        this.sessionActive = true;

        if (this.sessionInfoEl) {
            this.sessionInfoEl.textContent =
                `Session: ${session.id} | PID: ${session.pty_pid || '?'}`;
        }
        if (this.destroySessionBtn) {
            this.destroySessionBtn.disabled = false;
        }

        // Always reopen a fresh WS after teardown above, on the same delay
        // connectToSession uses, so
        // the terminal screen transition has time to settle and the
        // fit/font readiness dance in connectWebSocket() has a stable
        // container to measure.
        setTimeout(() => this.connectWebSocket(), 500);

        // Refresh tunnels panel in case tunnels were created/destroyed
        // while the user was away on the launchpad.
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

        // Open WebSocket via subprotocol auth (Item 3). JWT is carried in
        // the Sec-WebSocket-Protocol header, NOT in the URL — so no token
        // redaction is needed when logging the URL.
        const wsURL = window.API.getWebSocketURL();
        console.log('Terminal: Connecting to WebSocket:', wsURL);

        this.ws = window.API.openWebSocket();
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
            // Clear intentional-close flag now that a fresh WS is open —
            // any FUTURE close is a natural disconnect and should reconnect.
            this._intentionalClose = false;
            if (this.reconnectTimeout) {
                clearTimeout(this.reconnectTimeout);
                this.reconnectTimeout = null;
            }

            this.updateStatus('Connected', 'connected');

            if (this.term) {
                this.term.writeln('\x1b[1;32m[Connected to PTY terminal]\x1b[0m\n');
            }

            // Send initial resize (legacy fallback path — the server's
            // request_dims handshake will also arrive and trigger a
            // handshake-tagged sendResize which dedupes if dims match).
            this.sendResize('ws.onopen');

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

            // If the close was triggered by a deliberate session swap,
            // skip the disconnect banner + reconnect loop — the new
            // session's connect flow will paint its own state.
            if (this._intentionalClose) {
                console.log('Terminal: intentional close, skipping reconnect');
                this._intentionalClose = false;
                return;
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
        } else if (type === 'request_dims') {
            // Server-driven resize handshake. Fit and reply IMMEDIATELY —
            // bypass the 100ms debounce because the server is waiting in
            // a bounded timeout window (2s). Any debounce here would eat
            // into that budget and risk the server proceeding with stale
            // birth dims.
            if (this.fitAddon && this.term) {
                try {
                    this.fitAddon.fit();
                } catch (e) {
                    console.warn('[TERM-RESIZE] handshake fit failed', e);
                }
                this.sendResize('handshake', true /* force: always ship on handshake */);
            }
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
