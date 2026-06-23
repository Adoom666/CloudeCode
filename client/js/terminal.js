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
        this._programmaticScrollLock = 0;
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
            scrollback: 50000,
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

        // Wire the capture-phase wheel interceptor (see _applyWheelHandler).
        // DOM listener on term.element survives term.reset() since the
        // element isn't recreated, so a single attachment is sufficient.
        this._applyWheelHandler();

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
     * Attach a capture-phase wheel listener that scrolls xterm's own
     * scrollback instead of letting xterm translate the wheel into cursor
     * (up/down arrow) keystrokes — which it does on the alternate screen
     * buffer (active during a Claude Code TUI), where Claude reads those
     * arrows as "cycle previous prompts" and the scrollback never moves.
     * Capture phase + stopPropagation runs before xterm's own bubble-phase
     * wheel handler, so the arrow-key path never fires.
     */
    _applyWheelHandler() {
        if (!this.term || !this.term.element || this._wheelHandlerAttached) return;
        this.term.element.addEventListener('wheel', (e) => {
            if (e.deltaY === 0) return;
            this.autoScrollEnabled = false;  // bypass the 100ms scroll-listener debounce race
            const lines = Math.ceil(Math.abs(e.deltaY) / 40) * (e.deltaY > 0 ? 1 : -1);
            this.term.scrollLines(lines || (e.deltaY > 0 ? 1 : -1));
            e.preventDefault();
            e.stopPropagation();
        }, { capture: true });
        this._wheelHandlerAttached = true;
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
            // Multi-session: scope the upload to THIS tab's session so the
            // image lands in the right project's working dir.
            const sessionId = this._sessionId();
            const result = await window.API.uploadImage(blob, mimeType, sessionId);
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

                        if (this._programmaticScrollLock > 0) return;

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

    _forceScrollToBottom(holdMs = 400) {
        if (!this.term) return;
        this._programmaticScrollLock++;
        this.autoScrollEnabled = true;
        const pin = () => {
            if (!this.term) return;
            try { this.term.scrollToBottom(); } catch (_) { /* */ }
            const vp = document.querySelector('.xterm-viewport');
            if (vp) vp.scrollTop = vp.scrollHeight;
        };
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                pin();
                setTimeout(pin, 50);
                setTimeout(pin, 150);
                setTimeout(() => {
                    pin();
                    this._programmaticScrollLock = Math.max(0, this._programmaticScrollLock - 1);
                }, holdMs);
            });
        });
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
            // Let layout settle (screen-swap CSS toggle in app.js needs a
            // paint tick before clientWidth/clientHeight read truthful
            // values). Double-rAF is the canonical "wait for layout" guard.
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

            // Fit xterm to the container BEFORE painting scrollback so the
            // captured bytes land at the correct column width. xterm.js
            // doesn't reflow already-buffered content on resize, so painting
            // at the default 80-col geometry leaves the scrollback wrong
            // even after a later fit. If the container isn't visible yet,
            // fit() may throw or compute zeros — we swallow and continue;
            // the resize pipeline / handshake fit will still recover the
            // live screen, just not the already-painted scrollback rows.
            try {
                if (this.fitAddon && typeof this.fitAddon.fit === 'function') {
                    this.fitAddon.fit();
                }
            } catch (e) {
                console.warn('pre-paint fit failed (continuing):', e);
            }

            try {
                const bin = atob(initialScrollbackB64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) {
                    bytes[i] = bin.charCodeAt(i) & 0xff;
                }
                // Exit any alt-screen state + clear + home cursor so the captured bytes
                // paint into a known-clean screen instead of on top of stale parser
                // state (the bytes carry escape sequences relative to the tmux pane's
                // screen state at capture time — we have none of that here).
                this.term.write('\x1b[?1049l\x1b[2J\x1b[H');
                this.term.write(bytes, () => {
                    this._forceScrollToBottom();
                });
                console.log(`Terminal: painted ${bytes.length} bytes of adopt scrollback`);
                // Flag to send Ctrl+L in ws.onopen after dims handshake settles
                this._needsReplayCtrlL = true;
                this._pendingPostConnectScroll = true;
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

        // Load any locally-detected dev servers for this session
        this.loadLocalServers();
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
    async reconnectToExistingSession(session) {
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

        // Launchpad-rejoin scrollback replay — same treatment as the adopt
        // path in connectToSession(). The launchpad asks the server for
        // ``initial_scrollback_b64`` on the SessionInfo (via
        // ``getSession(..., { includeScrollback: true })``); when present we
        // paint those bytes into the freshly-reset xterm BEFORE the WS opens
        // so the user sees the pre-existing history immediately. The Ctrl+L
        // follow-up after the WS handshake (gated on ``_needsReplayCtrlL``)
        // forces the foreground app to redraw the live screen at the new
        // dims, on top of the painted history.
        const initialScrollbackB64 = session && session.initial_scrollback_b64;
        if (initialScrollbackB64) {
            // Let layout settle (screen-swap CSS toggle in app.js needs a
            // paint tick before clientWidth/clientHeight read truthful
            // values). Double-rAF is the canonical "wait for layout" guard.
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

            // Fit xterm to the container BEFORE painting scrollback so the
            // captured bytes land at the correct column width. xterm.js
            // doesn't reflow already-buffered content on resize, so painting
            // at the default 80-col geometry leaves the scrollback wrong
            // even after a later fit. If the container isn't visible yet,
            // fit() may throw or compute zeros — we swallow and continue;
            // the resize pipeline / handshake fit will still recover the
            // live screen, just not the already-painted scrollback rows.
            try {
                if (this.fitAddon && typeof this.fitAddon.fit === 'function') {
                    this.fitAddon.fit();
                }
            } catch (e) {
                console.warn('pre-paint fit failed (continuing):', e);
            }

            try {
                const bin = atob(initialScrollbackB64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) {
                    bytes[i] = bin.charCodeAt(i) & 0xff;
                }
                // Exit any alt-screen state + clear + home cursor so the
                // captured bytes paint into a known-clean parser state.
                this.term.write('\x1b[?1049l\x1b[2J\x1b[H');
                this.term.write(bytes, () => {
                    this._forceScrollToBottom();
                });
                console.log(`Terminal: painted ${bytes.length} bytes of rejoin scrollback`);
                this._needsReplayCtrlL = true;
                this._pendingPostConnectScroll = true;
            } catch (e) {
                // Non-fatal: fall through to the clean-screen rejoin. The
                // live stream over WS still works; user just misses the
                // pre-existing history paint.
                console.warn('reconnectToExistingSession: failed to paint initial scrollback', e);
            }
        }

        // Always reopen a fresh WS after teardown above, on the same delay
        // connectToSession uses, so
        // the terminal screen transition has time to settle and the
        // fit/font readiness dance in connectWebSocket() has a stable
        // container to measure.
        setTimeout(() => this.connectWebSocket(), 500);

        // Refresh local-servers panel in case dev servers came up or
        // shut down while the user was away on the launchpad.
        this.loadLocalServers();
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
        // redaction is needed when logging the URL. Multi-session: the
        // session id goes in the ``?session_id=`` query param so the
        // server scopes this stream to OUR session — another tab on a
        // different session keeps its own WS undisturbed. ``_currentSession``
        // may be a bare Session ({id}) or a SessionInfo ({session:{id}}).
        const sessionId = this._sessionId();
        const wsURL = window.API.getWebSocketURL(sessionId);
        console.log('Terminal: Connecting to WebSocket:', wsURL);

        this.ws = window.API.openWebSocket(sessionId);
        this.ws.binaryType = 'arraybuffer';
        this.setupWebSocketHandlers();
    }

    /**
     * Resolve THIS tab's session id from ``_currentSession``, which may be
     * a bare Session ({id}) or a SessionInfo ({session:{id}}). Returns null
     * when not yet known (server falls back to "the" current session).
     */
    _sessionId() {
        const s = this._currentSession;
        if (!s) return null;
        if (s.id) return s.id;
        if (s.session && s.session.id) return s.session.id;
        return null;
    }

    /**
     * Resolve the current tmux session name from ``_currentSession``
     * (bare Session or SessionInfo shape).
     */
    _currentTmuxName() {
        const s = this._currentSession;
        if (!s) return null;
        if (s.tmux_session) return s.tmux_session;
        if (s.session && s.session.tmux_session) return s.session.tmux_session;
        return null;
    }

    /**
     * v0.7.1 — swap the in-session header title span for an inline input
     * so the user can edit the session name. Triggered by the pencil
     * button next to #header-title-text. Enter/blur saves; Esc cancels.
     *
     * Idempotent: if a rename input is already showing, this is a no-op.
     */
    _enterHeaderRename() {
        const titleEl = document.getElementById('header-title-text');
        if (!titleEl) return;
        if (titleEl.style.display === 'none') return; // already editing
        const current = this._currentTmuxName();
        if (!current) return;

        // Build the input.
        const input = document.createElement('input');
        input.type = 'text';
        input.id = 'header-rename-input';
        input.className = 'header-rename-input';
        input.value = current;
        input.maxLength = 64;
        input.spellcheck = false;
        input.autocomplete = 'off';
        input.setAttribute('aria-label', 'New session name');

        // Inline error label (hidden until needed). Sits below the input.
        const err = document.createElement('span');
        err.id = 'header-rename-error';
        err.className = 'header-rename-error';
        err.style.display = 'none';

        // Hide the title span + pencil button while editing.
        titleEl.style.display = 'none';
        const pencilEl = document.getElementById('header-rename-pencil');
        if (pencilEl) pencilEl.style.display = 'none';

        // Insert input + error label right after the (now hidden) title.
        titleEl.insertAdjacentElement('afterend', input);
        input.insertAdjacentElement('afterend', err);

        // Track whether we already saved/cancelled so blur after Enter
        // doesn't double-fire.
        let settled = false;

        const cleanup = () => {
            try {
                if (input.parentNode) input.parentNode.removeChild(input);
            } catch (_) { /* non-fatal */ }
            try {
                if (err.parentNode) err.parentNode.removeChild(err);
            } catch (_) { /* non-fatal */ }
            titleEl.style.display = '';
            if (pencilEl) pencilEl.style.display = '';
        };

        const cancel = () => {
            if (settled) return;
            settled = true;
            cleanup();
        };

        const save = async () => {
            if (settled) return;
            const raw = (input.value || '').trim();
            // Empty / unchanged → cancel.
            if (!raw || raw === current) {
                cancel();
                return;
            }
            // Client-side pre-flight; server is still authoritative on the
            // regex. We mirror the server regex so the user gets immediate
            // feedback without a round-trip on obvious typos.
            if (!/^[A-Za-z0-9_-]{1,64}$/.test(raw)) {
                err.textContent = 'Use 1-64 chars: A-Z a-z 0-9 _ -';
                err.style.display = '';
                input.focus();
                input.select();
                return;
            }
            const sid = this._sessionId();
            if (!sid) {
                err.textContent = 'No active session';
                err.style.display = '';
                return;
            }
            settled = true;
            try {
                await window.API.renameSession(sid, raw);
                // On success the server's WS broadcast (session.renamed)
                // will repaint the header + tab title + launchpad row.
                // We tear down the input either way via _exitHeaderRename
                // (which is also invoked by the WS handler). Calling here
                // covers the case where the broadcast races us.
                cleanup();
            } catch (e) {
                settled = false; // let the user retry
                let msg = (e && e.message) ? e.message : 'Rename failed';
                // Surface common error codes more readably.
                if (/409/.test(msg) || /already in use/i.test(msg)) {
                    msg = 'Name already in use';
                } else if (/400/.test(msg) || /Invalid session name/i.test(msg)) {
                    msg = 'Invalid name';
                } else if (/404/.test(msg)) {
                    msg = 'Session not found';
                }
                err.textContent = msg;
                err.style.display = '';
                input.focus();
                input.select();
            }
        };

        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                save();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                cancel();
            }
        });
        input.addEventListener('blur', () => {
            // A blur immediately after a successful save would no-op
            // (settled=true short-circuits both branches), so we just
            // call save() — if the user blurred with an unchanged value
            // it cancels; otherwise we attempt the rename.
            save();
        });

        // Focus + select so the user can immediately type a replacement.
        setTimeout(() => { input.focus(); input.select(); }, 0);
    }

    /**
     * Tear down the inline rename input (if any). Called from the WS
     * ``session.renamed`` handler so the input swaps back to display
     * mode with the new value already painted via setHeaderIdentity.
     */
    _exitHeaderRename(newName) {
        const input = document.getElementById('header-rename-input');
        const err = document.getElementById('header-rename-error');
        const titleEl = document.getElementById('header-title-text');
        const pencilEl = document.getElementById('header-rename-pencil');
        if (input && input.parentNode) {
            try { input.parentNode.removeChild(input); } catch (_) { /* */ }
        }
        if (err && err.parentNode) {
            try { err.parentNode.removeChild(err); } catch (_) { /* */ }
        }
        if (titleEl) {
            titleEl.style.display = '';
            if (newName) titleEl.textContent = newName;
        }
        if (pencilEl) pencilEl.style.display = '';
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

            // v0.7.0 Part 2 — backfill any unacked toasts for THIS session
            // that fired while this browser was disconnected. Fire-and-forget;
            // failure here is logged but doesn't block the terminal coming up.
            const sidForToasts = this._sessionId();
            if (sidForToasts && window.API && window.ToastManager &&
                typeof window.API.getSessionToasts === 'function') {
                window.API.getSessionToasts(sidForToasts, { unackedOnly: true })
                    .then((toasts) => {
                        if (Array.isArray(toasts) && toasts.length) {
                            window.ToastManager.backfill(toasts);
                        }
                    })
                    .catch((err) => {
                        console.warn('[Toast] backfill failed', err && err.message);
                    });
            }

            // Send initial resize (legacy fallback path — the server's
            // request_dims handshake will also arrive and trigger a
            // handshake-tagged sendResize which dedupes if dims match).
            this.sendResize('ws.onopen');

            // DO NOT send Ctrl+L (0x0c) from the client here. The server's
            // resize handshake already writes a single 0x0c to the PTY after
            // SIGWINCH settles (src/api/websocket.py — success path ~:363,
            // degraded fallback ~:381), and that is the authoritative redraw
            // that repaints the live screen on top of our replayed scrollback
            // at the correct post-resize geometry.
            //
            // Claude Code's TUI debounces Ctrl+L: a SINGLE 0x0c forces a safe
            // redraw, but TWO within ~2s (in fullscreen/alt-screen rendering)
            // are interpreted as the `/clear` chord gesture and WIPE THE
            // CONTEXT. A client 0x0c here lands ~+50ms after WS open while the
            // server's lands ~+200ms (post dims + 150ms SIGWINCH sleep) — two
            // 0x0c <2s apart → accidental /clear on every launchpad rejoin.
            // The viewport snap-to-bottom is a purely LOCAL xterm op handled
            // below via _pendingPostConnectScroll/_forceScrollToBottom and
            // needs no wire write, so dropping this send loses nothing.
            this._needsReplayCtrlL = false;

            if (this._pendingPostConnectScroll) {
                this._pendingPostConnectScroll = false;
                this._forceScrollToBottom(800);
            }

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

        this.ws.onclose = (event) => {
            const closeCode = (event && typeof event.code === 'number') ? event.code : null;
            console.log('Terminal: WebSocket closed', { code: closeCode });
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

            // Auth-fail close from server (src/api/websocket.py — code 4401
            // is emitted when JWT verification fails on the WS handshake or
            // when the access token expires mid-stream). Don't reconnect
            // with the same stale token — that would spin the close/4401
            // loop until we exhaust maxReconnectAttempts. Instead, proactively
            // refresh first so the next openWebSocket() picks up a fresh
            // token via getToken().
            if (closeCode === 4401 && this.sessionActive && !this.isReconnecting) {
                this._handleAuthFailedClose();
                return;
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
        } else if (type === 'local_server_detected') {
            // Plan v3.2 — A dev server was detected on the host and
            // confirmed as a live TCP listener. Merge into local state
            // and re-render.
            if (this.term && message.url) {
                this.term.writeln(`\x1b[1;36m[Local server detected: ${message.url}]\x1b[0m`);
            }
            this._mergeLocalServer({ port: message.port, url: message.url });
        } else if (type === 'local_server_lost') {
            // The janitor sweep stopped seeing this listener — drop it.
            this._dropLocalServer(message.port);
        } else if (type === 'error') {
            if (this.term) {
                this.term.writeln(`\x1b[1;31m[Error: ${message.message}]\x1b[0m`);
            }
        } else if (type === 'pong') {
            console.log('Terminal: Received pong');
        } else if (type === 'toast.new') {
            // v0.7.0 Part 2 — new toast fired for this session. Hand to
            // ToastManager which dedupes by id, animates entry, and
            // applies the per-session accent color from message.toast.color.
            if (window.ToastManager && message && message.toast) {
                window.ToastManager.add(message.toast);
            }
        } else if (type === 'toast.ack') {
            // Another browser (or this one's POST) acked a toast. Dismiss
            // the local card without re-syncing to the server.
            if (window.ToastManager && message && message.toast_id) {
                window.ToastManager.dismiss(message.toast_id, { syncToServer: false });
            }
        } else if (type === 'session.renamed') {
            // v0.7.1 — server broadcast: this session was renamed (could be
            // us OR another browser tab that initiated the PATCH). Update
            // local copies + the in-session header + the browser tab title
            // when the rename targets THIS attached session. We always
            // poke the launchpad poller so it refreshes immediately rather
            // than waiting on its 5s tick.
            try {
                const myId = this._sessionId();
                const sess = this._currentSession;
                if (message && message.session_id === myId && sess && message.new_name) {
                    // Update the in-memory session record so the rest of the
                    // controller (active-name resolver, header re-paint on
                    // reconnect) sees the new value immediately.
                    if (sess.session && typeof sess.session === 'object') {
                        sess.session.tmux_session = message.new_name;
                    }
                    sess.tmux_session = message.new_name;
                    if (typeof window.setHeaderIdentity === 'function') {
                        window.setHeaderIdentity({
                            icon: 'cloude',
                            title: message.new_name,
                        });
                    }
                    if (typeof window.setPageTitle === 'function') {
                        window.setPageTitle(message.new_name);
                    }
                    // If a rename input is showing in the header, swap it
                    // back to display mode so the user sees the new name
                    // reflected even when the broadcast originated here.
                    if (typeof this._exitHeaderRename === 'function') {
                        this._exitHeaderRename(message.new_name);
                    }
                }
                // Force the launchpad to re-render its running-sessions
                // list immediately (it polls every 5s, but a rename should
                // appear instantly).
                if (window.Launchpad && typeof window.Launchpad.loadRunningSessions === 'function') {
                    try { window.Launchpad.loadRunningSessions(); } catch (_) { /* non-fatal */ }
                }
            } catch (err) {
                console.warn('Terminal: session.renamed handling failed:', err);
            }
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
     * Handle a WS close caused by server-side auth failure (code 4401).
     * Refresh the access token BEFORE the next reconnect attempt so the
     * fresh WS handshake carries a valid JWT — otherwise reconnects would
     * loop on 4401 until maxReconnectAttempts and force a TOTP re-prompt.
     *
     * Uses API._singleFlightRefresh when available so a concurrent HTTP
     * 401 path that's already rotating doesn't burn the refresh chain.
     */
    async _handleAuthFailedClose() {
        if (this.isReconnecting) return;
        this.isReconnecting = true;
        this.updateStatus('Refreshing auth...');

        let refreshed = false;
        try {
            const api = window.API;
            if (api && typeof api._singleFlightRefresh === 'function') {
                refreshed = await api._singleFlightRefresh();
            } else if (window.Auth && typeof window.Auth.refresh === 'function') {
                refreshed = await window.Auth.refresh();
            }
        } catch (e) {
            console.warn('Terminal: refresh during 4401 reconnect threw', e);
            refreshed = false;
        }

        this.isReconnecting = false;

        if (refreshed === true) {
            console.log('Terminal: refresh ok after 4401, reconnecting');
            this.attemptReconnect();
            return;
        }
        if (refreshed === 'network-error') {
            console.warn('Terminal: refresh network error after 4401, short-delay retry');
            if (this.term) {
                this.term.writeln('\x1b[1;33m[Network blip — retrying in 4s]\x1b[0m');
            }
            if (this.reconnectTimeout) clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = setTimeout(() => {
                this.reconnectTimeout = null;
                if (this.sessionActive) this._handleAuthFailedClose();
            }, 4000);
            return;
        }
        console.warn('Terminal: refresh failed after 4401, escalating to re-auth');
        if (window.API && typeof window.API.handleUnauthorized === 'function') {
            window.API.handleUnauthorized();
        } else {
            window.dispatchEvent(new CustomEvent('auth-required'));
        }
    }

    /**
     * Resolve the tmux session name to query the local-servers endpoint
     * for. Returns null when no session is active or the name can't be
     * read (e.g. fresh session view, server not yet replied).
     */
    _activeSessionName() {
        const sess = this._currentSession;
        if (!sess) return null;
        return sess.tmux_session || sess.id || null;
    }

    /**
     * Load locally-detected dev servers for the active session and paint
     * them into the Local Servers panel. Detection is server-side only;
     * this call is a pure read.
     */
    async loadLocalServers() {
        const name = this._activeSessionName();
        if (!name) {
            this._localServers = [];
            this._renderLocalServers();
            return;
        }
        try {
            const list = await window.API.getLocalServers(name);
            this._localServers = Array.isArray(list) ? list : [];
            this._renderLocalServers();
        } catch (error) {
            console.error('Terminal: Error loading local servers:', error);
        }
    }

    /**
     * Merge a single local-server entry into local state (idempotent on
     * port). Triggered by the `local_server_detected` WS event.
     */
    _mergeLocalServer(entry) {
        if (!entry || !entry.port) return;
        if (!Array.isArray(this._localServers)) this._localServers = [];
        const idx = this._localServers.findIndex(s => s.port === entry.port);
        if (idx === -1) {
            this._localServers.push({ port: entry.port, url: entry.url });
        } else {
            this._localServers[idx] = { ...this._localServers[idx], url: entry.url };
        }
        this._localServers.sort((a, b) => a.port - b.port);
        this._renderLocalServers();
    }

    /**
     * Drop a local-server entry by port. Triggered by `local_server_lost`.
     */
    _dropLocalServer(port) {
        if (!Array.isArray(this._localServers)) return;
        this._localServers = this._localServers.filter(s => s.port !== port);
        this._renderLocalServers();
    }

    /**
     * Repaint the Local Servers panel from `this._localServers`. Hides
     * the container when no entries are tracked.
     */
    _renderLocalServers() {
        const container = document.getElementById('localServersContainer');
        const list = document.getElementById('localServersList');
        if (!container || !list) return;

        const entries = Array.isArray(this._localServers) ? this._localServers : [];
        if (entries.length === 0) {
            container.style.display = 'none';
            list.innerHTML = '';
            return;
        }
        container.style.display = 'block';
        list.innerHTML = entries.map(entry => `
            <div class="local-server-item">
                <span class="local-server-port">${entry.port}</span>
                <a class="local-server-url" href="${entry.url}" target="_blank" rel="noopener">${entry.url}</a>
            </div>
        `).join('');
    }

    /**
     * Destroy session
     */
    async destroySession() {
        try {
            this.updateStatus('Destroying session...');

            // Multi-session: destroy THIS tab's session only — other tabs'
            // sessions are untouched.
            const sessionId = this._sessionId();
            await window.API.destroySession(sessionId);

            // v0.7.0 Part 2 — drop any ghost toasts for the destroyed
            // session. Server-side state is already gone with the session,
            // so we don't sync; just clear our local UI.
            if (sessionId && window.ToastManager &&
                typeof window.ToastManager.dismissBySession === 'function') {
                window.ToastManager.dismissBySession(sessionId);
            }

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
