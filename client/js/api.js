/**
 * API Module - Handles all API calls with JWT token injection
 */

console.log('[API Module] Loading...');

class API {
    constructor() {
        const protocol = window.location.protocol === 'https:' ? 'https:' : 'http:';
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;

        this.baseURL = `${protocol}//${host}/api/v1`;
        this.wsBaseURL = `${wsProtocol}//${host}`;

        // Item 5: single-flight mutex for refresh-token rotation.
        //
        // When N requests race and all see 401 at roughly the same time,
        // they must NOT each fire their own /auth/refresh. The server
        // enforces reuse detection on the refresh token — so if two
        // refresh calls land on the same refresh_token, the second is
        // treated as a theft event and BOTH get revoked (chain burn).
        //
        // The fix is a classic Promise-based mutex: the first 401-victim
        // creates a refresh Promise and stores it here; subsequent
        // 401-victims await the SAME promise instead of starting their
        // own. When it settles, everyone sees the same outcome and
        // either all retry with the fresh access token or all fall
        // through to the re-auth path.
        //
        // A boolean flag would race (flag-then-set is two operations);
        // the Promise IS the primitive — storing it atomically captures
        // both the "in flight" and "eventual result" states.
        this._refreshPromise = null;
    }

    /**
     * Get auth token from localStorage
     */
    getToken() {
        return localStorage.getItem('claude_tunnel_token');
    }

    /**
     * Make authenticated API call.
     *
     * On a 401 from a protected endpoint we transparently run the
     * refresh-token rotation dance and replay the original request with
     * the new access token. If refresh fails (no refresh token stored,
     * server says 401, network error, ...) we fall through to the
     * handleUnauthorized() path so the UI can reauth via TOTP.
     *
     * @param {string} endpoint - API endpoint (e.g., '/sessions')
     * @param {object} options - fetch options
     * @param {object} [_meta] - internal; callers pass {_retrying: true}
     *                           to break the refresh-then-retry loop.
     * @returns {Promise<any>} - Response data
     */
    async call(endpoint, options = {}, _meta = {}) {
        const token = this.getToken();

        // Prepare headers
        const headers = { ...(options.headers || {}) };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }
        if (!headers['Content-Type'] && options.body && typeof options.body === 'object') {
            headers['Content-Type'] = 'application/json';
        }

        // Make request
        const url = `${this.baseURL}${endpoint}`;
        const fetchOptions = {
            ...options,
            headers
        };

        // Convert body to JSON if it's an object
        if (fetchOptions.body && typeof fetchOptions.body === 'object') {
            fetchOptions.body = JSON.stringify(fetchOptions.body);
        }

        try {
            const response = await fetch(url, fetchOptions);

            // Handle 401 Unauthorized.
            //
            // First 401: try to rotate the refresh token (single-flight —
            // see constructor comment). If refresh wins, replay the
            // original request once with the new access token.
            //
            // Second 401 (or refresh failure): give up, clear tokens,
            // fire auth-required so the shell re-prompts for TOTP.
            if (response.status === 401) {
                if (!_meta._retrying && window.Auth && window.Auth.getRefreshToken()) {
                    const refreshed = await this._singleFlightRefresh();
                    if (refreshed) {
                        console.log('API: 401 recovered via refresh, retrying original request');
                        return this.call(endpoint, options, { _retrying: true });
                    }
                }
                console.log('API: 401 Unauthorized - triggering re-auth');
                this.handleUnauthorized();
                throw new Error('Authentication required. Please log in again.');
            }

            // Handle other errors
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || errorData.message || `HTTP ${response.status}`);
            }

            // Return JSON response
            return await response.json();
        } catch (error) {
            console.error(`API Error [${endpoint}]:`, error);
            throw error;
        }
    }

    /**
     * Single-flight refresh wrapper. See constructor comment on
     * _refreshPromise for the "why".
     *
     * @returns {Promise<boolean>}
     */
    async _singleFlightRefresh() {
        if (this._refreshPromise) {
            // Another in-flight request already kicked off refresh.
            // Await the SAME promise so we don't burn the chain.
            return this._refreshPromise;
        }
        // Store the promise atomically BEFORE awaiting, so any sibling
        // 401 handler that checks `this._refreshPromise` on its next
        // event-loop tick sees the same value and joins in.
        this._refreshPromise = (async () => {
            try {
                return await window.Auth.refresh();
            } finally {
                // Clear the slot regardless of outcome so a subsequent
                // 401 (say, the just-rotated access token itself expired
                // a moment later) can trigger a fresh refresh.
                this._refreshPromise = null;
            }
        })();
        return this._refreshPromise;
    }

    /**
     * Handle unauthorized response. Clears BOTH access + refresh tokens
     * since we're bailing out to the TOTP prompt.
     */
    handleUnauthorized() {
        if (window.Auth) {
            window.Auth.clearToken();
        } else {
            // Fallback if Auth hasn't initialized yet.
            localStorage.removeItem('claude_tunnel_token');
            localStorage.removeItem('claude_refresh_token');
        }

        // Trigger auth required event
        window.dispatchEvent(new CustomEvent('auth-required'));
    }

    /**
     * Auth: Verify TOTP code
     * @param {string} totpCode - 6-digit TOTP code
     * @returns {Promise<{token: string}>}
     */
    async verifyTOTP(totpCode) {
        const response = await fetch(`${this.baseURL}/auth/verify`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: totpCode })
        });

        if (!response.ok) {
            // Normalize error shape across sources:
            //   - FastAPI HTTPException → { detail: "..." }
            //   - slowapi rate-limit (429) → { error: "Rate limit exceeded: ..." }
            //   - malformed / empty       → {}
            // Prefer `error` (slowapi), fall back to `detail` (FastAPI),
            // then a generic message. NEVER fall through to a hardcoded
            // client-side message — that would silently overwrite the
            // server's actual signal (e.g. hide a 429 behind "Invalid TOTP
            // code").
            const errorData = await response.json().catch(() => ({}));
            let message = errorData.error || errorData.detail || errorData.message || 'Unknown error';

            // RFC 7231: Retry-After is either integer seconds or an HTTP-date.
            // For rate-limit 429s slowapi emits integer seconds. Parse
            // defensively — if unparseable, skip the suffix rather than
            // showing "NaN".
            if (response.status === 429) {
                const retryAfterRaw = response.headers.get('Retry-After');
                const retrySec = parseInt(retryAfterRaw, 10);
                // slowapi's server body already includes "Try again in Ns."
                // so only append our own suffix if the server didn't.
                // Otherwise we end up with "... Try again in 58s. Try again in 58s."
                if (Number.isFinite(retrySec) && retrySec > 0 && !/try again/i.test(message)) {
                    message = `${message.replace(/\.$/, '')}. Try again in ${retrySec}s.`;
                }
            }

            const err = new Error(message);
            err.status = response.status;
            throw err;
        }

        return await response.json();
    }

    /**
     * Auth: Check authentication status
     * @returns {Promise<{authenticated: boolean}>}
     */
    async checkAuthStatus() {
        return await this.call('/auth/status');
    }

    /**
     * Auth: Get QR code for setup (no auth required)
     * @returns {Promise<{qr_code: string, secret: string}>}
     */
    async getQRCode() {
        const response = await fetch(`${this.baseURL}/auth/qr`);
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            // Attach HTTP status so callers can discriminate semantic
            // outcomes — e.g. 403 here means "qr endpoint locked because
            // pairing is already complete", which is success state for the
            // login screen, NOT a setup-required failure. Mirrors the
            // err.status pattern used in verifyTOTP() above.
            const err = new Error(errorData.detail || 'Failed to get QR code');
            err.status = response.status;
            throw err;
        }
        return await response.json();
    }

    /**
     * Config: Get common slash commands
     * @returns {Promise<object>} - {commands: Array<string>}
     */
    async getCommonCommands() {
        return await this.call('/config/common-commands');
    }

    /**
     * Projects: Get project list
     * @returns {Promise<Array>}
     */
    async getProjects() {
        return await this.call('/projects');
    }

    /**
     * Projects: Create new project
     * @param {object} params - {name: string, path: string, description?: string}
     * @returns {Promise<object>} - Project data
     */
    async createProject(params) {
        return await this.call('/projects', {
            method: 'POST',
            body: params
        });
    }

    /**
     * Projects: Delete project
     * @param {string} projectName - Name of the project to delete
     * @returns {Promise<object>}
     */
    async deleteProject(projectName) {
        return await this.call(`/projects/${encodeURIComponent(projectName)}`, {
            method: 'DELETE'
        });
    }

    /**
     * Projects: Rename / update description (display name only — never
     * touches the folder on disk). Pass only the fields you want to change.
     *
     * @param {string} currentName - Current display name (URL identifier).
     * @param {object} fields - {newName?: string, description?: string}.
     *   ``description: ""`` is honored as an intentional clear.
     * @returns {Promise<object>} - Updated project (canonical form).
     */
    async updateProject(currentName, { newName, description } = {}) {
        const body = {};
        if (newName !== undefined) body.new_name = newName;
        if (description !== undefined) body.description = description;
        return await this.call(`/projects/${encodeURIComponent(currentName)}`, {
            method: 'PATCH',
            body
        });
    }

    /**
     * Projects: Clone a GitHub repo (server runs `gh repo clone`) and
     * register the result as a project.
     *
     * @param {object} params
     * @param {string} params.repoUrl - GitHub URL or owner/repo shorthand.
     * @param {string} [params.parentDir] - Directory in which the cloned
     *   folder is created (server default: ~/projects).
     * @param {string} [params.projectName] - Override auto-detected name.
     * @param {string} [params.description] - Optional project description.
     * @returns {Promise<{name: string, path: string, description: ?string}>}
     */
    async cloneProjectFromGithub({ repoUrl, parentDir, projectName, description } = {}) {
        const body = { repo_url: repoUrl };
        if (parentDir !== undefined && parentDir !== '') body.parent_dir = parentDir;
        if (projectName !== undefined && projectName !== '') body.project_name = projectName;
        if (description !== undefined) body.description = description;
        return await this.call('/projects/clone', {
            method: 'POST',
            body
        });
    }

    /**
     * Filesystem: Browse a directory on the server
     * @param {string|null} path - Directory path to list, or null to start at the default location
     * @returns {Promise<{path: string, parent: string|null, entries: Array<{name: string, path: string}>}>}
     */
    async browseDirectory(path = null) {
        const query = path ? `?path=${encodeURIComponent(path)}` : '';
        return await this.call(`/filesystem/browse${query}`);
    }

    /**
     * Sessions: Create new session
     * @param {object} params - {working_dir?: string, auto_start_claude?: boolean, copy_templates?: boolean, cols?: number, rows?: number, project_name?: string|null}
     * @returns {Promise<object>} - Session data
     */
    async createSession(params = {}) {
        return await this.call('/sessions', {
            method: 'POST',
            body: params
        });
    }

    /**
     * Sessions: Get current session info
     * @returns {Promise<object>} - Session data
     */
    async getSession() {
        return await this.call('/sessions');
    }

    /**
     * Sessions: Fetch current session or null when none is active.
     *
     * Thin wrapper over ``getSession`` that translates the 404-on-no-session
     * into a ``null`` return so callers (e.g. the launchpad active-session
     * banner) can render without try/catch boilerplate. Any non-404 error
     * rethrows so the caller can surface or log it.
     *
     * @returns {Promise<object|null>} - SessionInfo or null on 404
     */
    async getCurrentSession() {
        try {
            return await this.getSession();
        } catch (error) {
            // Our ``call`` wrapper throws Error with a message that starts
            // with the backend's detail string. "No active session" is what
            // ``GET /sessions`` returns when none exists — treat it as null.
            const msg = (error && error.message) || '';
            if (/No active session|HTTP 404|404/i.test(msg)) {
                return null;
            }
            throw error;
        }
    }

    /**
     * Sessions: Destroy current session
     * @returns {Promise<object>}
     */
    async destroySession() {
        return await this.call('/sessions', {
            method: 'DELETE'
        });
    }

    /**
     * Sessions: Upload an image blob to the active session.
     *
     * Why this lives outside ``call()``: ``call()`` unconditionally
     * ``JSON.stringify``s any ``typeof body === 'object'`` payload (line
     * 79–81 above). ``FormData`` IS a typeof-object, so routing through
     * ``call()`` would mangle the multipart body to "[object FormData]".
     * Beyond that, the browser MUST set the multipart Content-Type with
     * its own boundary token — explicitly setting Content-Type would
     * destroy the boundary and the server would 422 the request.
     *
     * Auth + 401 retry mirrors ``call()`` exactly (single-flight refresh,
     * one replay, then handleUnauthorized) — no behavior drift.
     *
     * @param {Blob} blob - Image bytes from the clipboard / file picker.
     * @param {string} mimeType - e.g. ``'image/png'``. Used only for the
     *   multipart filename suffix; the server renames to ``<uuid>.<ext>``.
     * @param {object} [_meta] - internal; callers pass ``{_retrying: true}``
     *   to break the refresh-then-retry loop.
     * @returns {Promise<{path: string, filename: string, size: number}>}
     */
    async uploadImage(blob, mimeType, _meta = {}) {
        const ext = (mimeType && mimeType.split('/')[1]) || 'png';
        const filename = `paste.${ext}`;
        const form = new FormData();
        form.append('file', blob, filename);

        const token = this.getToken();
        const headers = {};
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const url = `${this.baseURL}/sessions/upload-image`;
        try {
            const response = await fetch(url, {
                method: 'POST',
                headers,
                body: form,
            });

            if (response.status === 401) {
                if (!_meta._retrying && window.Auth && window.Auth.getRefreshToken()) {
                    const refreshed = await this._singleFlightRefresh();
                    if (refreshed) {
                        console.log('API: 401 recovered via refresh, retrying uploadImage');
                        return this.uploadImage(blob, mimeType, { _retrying: true });
                    }
                }
                console.log('API: 401 Unauthorized on uploadImage - triggering re-auth');
                this.handleUnauthorized();
                throw new Error('Authentication required. Please log in again.');
            }

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || errorData.message || `HTTP ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            console.error('API Error [uploadImage]:', error);
            throw error;
        }
    }

    /**
     * Sessions: Destroy an external (non-active) tmux session by name.
     *
     * Direct kill via the server's `DELETE /sessions/external/{name}`
     * endpoint. Used by the launchpad "X" button when the target row
     * is NOT the currently-active backend — bypasses the old
     * adopt-then-destroy flow which 500'd on dead panes (foreground
     * process exited, e.g. user Ctrl-D'd `claude`).
     *
     * Idempotent: if the session is already gone server-side, the
     * server returns 200 with an "already gone" message.
     *
     * @param {string} sessionName - tmux session name (as seen in launchpad)
     * @returns {Promise<{success: boolean, message: string}>}
     */
    async destroyExternalSession(sessionName) {
        return await this.call(
            `/sessions/external/${encodeURIComponent(sessionName)}`,
            { method: 'DELETE' }
        );
    }

    /**
     * Sessions: Detach from the current session WITHOUT killing tmux.
     *
     * Soft counterpart to ``destroySession`` — the server tears down its
     * Python-side handles (reader task, idle watcher, pipe-pane) but
     * leaves the tmux session alive so it can be re-adopted later from
     * the Adopt list. Used by the "switch to a different project" flow
     * so the user doesn't lose their running Claude / shell state when
     * they swap projects from the launchpad.
     *
     * @returns {Promise<object>}
     */
    async detachSession() {
        return await this.call('/sessions/detach', {
            method: 'POST'
        });
    }

    /**
     * Sessions: List externally-started tmux sessions that can be adopted.
     *
     * Returns sessions on the `cloude` tmux socket that were NOT created by
     * this server (e.g. the user ran `tmux -L cloude new -s foo` themselves).
     * The server filters out the currently-active backend's name defensively
     * so we never present a self-adopt footgun; the client also filters in
     * the render pass as belt-and-suspenders.
     *
     * @returns {Promise<Array<{name: string, created_by_cloude: boolean,
     *   created_at_epoch: number, window_count: number}>>}
     */
    async listAttachableSessions() {
        return await this.call('/sessions/attachable');
    }

    /**
     * Sessions: Adopt an externally-started tmux session.
     *
     * Server-side this sets up `pipe-pane` on the target, captures the
     * visible scrollback, records the fifo byte offset for the WS tailer,
     * and returns the scrollback (base64) alongside the session metadata.
     * Client paints the scrollback into xterm BEFORE opening the WS so
     * the tailer's seek-to-offset doesn't cause a tear.
     *
     * If the user has an active session and `confirmDetach` is false,
     * the server returns 409 — caller should show a confirmation modal
     * and retry with `confirmDetach=true`. The prior session is detached
     * (tmux keeps running), never killed. Destruction is only via the
     * explicit destroy button.
     *
     * @param {string} sessionName - tmux session name (as seen in launchpad)
     * @param {boolean} confirmDetach - user consented to detaching from
     *   the current session so the adopted one can take the active slot.
     *   Required when any session is already active.
     * @returns {Promise<{session: object, initial_scrollback_b64: string,
     *   fifo_start_offset: number}>}
     */
    async adoptSession(sessionName, confirmDetach = false) {
        return await this.call('/sessions/adopt', {
            method: 'POST',
            body: {
                session_name: sessionName,
                confirm_detach: confirmDetach,
            },
        });
    }

    /**
     * Tunnels: Get all tunnels
     * @returns {Promise<Array>}
     */
    async getTunnels() {
        return await this.call('/tunnels');
    }

    /**
     * Tunnels: Create tunnel
     * @param {number} port - Port number
     * @returns {Promise<object>} - Tunnel data
     */
    async createTunnel(port) {
        return await this.call('/tunnels', {
            method: 'POST',
            body: { port }
        });
    }

    /**
     * Tunnels: Destroy tunnel
     * @param {string} tunnelId - Tunnel ID
     * @returns {Promise<object>}
     */
    async destroyTunnel(tunnelId) {
        return await this.call(`/tunnels/${tunnelId}`, {
            method: 'DELETE'
        });
    }

    /**
     * Get plain WebSocket base URL for the terminal endpoint.
     * Does NOT append a token — JWT auth is carried in the
     * Sec-WebSocket-Protocol header via openWebSocket() below.
     *
     * @param {string} path - WebSocket path (default '/ws/terminal')
     * @returns {string} - WebSocket URL (no query string, no token)
     */
    getWebSocketURL(path = '/ws/terminal') {
        return `${this.wsBaseURL}${path}`;
    }

    /**
     * Open an authenticated WebSocket to the backend.
     *
     * Uses the Sec-WebSocket-Protocol subprotocol header to carry the JWT
     * instead of a query string. The browser's WebSocket constructor accepts
     * an array of subprotocol tokens as its second argument and serializes
     * them into a comma-separated `Sec-WebSocket-Protocol` request header.
     * The server validates the JWT, then echoes back the `cloude.jwt.v1`
     * marker via the handshake response — required by RFC 6455 or the
     * browser drops the connection.
     *
     * Why this instead of `?token=<jwt>`:
     *   - JWTs in URLs leak into proxy/access logs, browser history, and
     *     Referer headers.
     *   - Subprotocol is a request header, not logged by default.
     *
     * Pattern modeled on the Kubernetes API server's WebSocket streams,
     * which use a similar two-element subprotocol array for bearer tokens.
     *
     * @param {string} path - WebSocket path (default '/ws/terminal')
     * @returns {WebSocket} - Open (pending) WebSocket
     */
    openWebSocket(path = '/ws/terminal') {
        const token = this.getToken();
        const url = `${this.wsBaseURL}${path}`;
        // Two-element subprotocol array: marker + token. The server parses
        // these out of the Sec-WebSocket-Protocol header and verifies the
        // JWT before accepting. Do NOT collapse into a single string —
        // the two-element form is what the server expects.
        return new WebSocket(url, ['cloude.jwt.v1', token]);
    }

    /**
     * Server: Reset server
     * @returns {Promise<object>}
     */
    async resetServer() {
        return await this.call('/server/reset', {
            method: 'POST'
        });
    }
}

// Export singleton instance
window.API = new API();
console.log('[API Module] Exported as window.API:', window.API);
