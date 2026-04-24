/**
 * Auth Module - Handles authentication, token management, and auth UI
 */

console.log('[Auth Module] Loading...');

class Auth {
    constructor() {
        this.tokenKey = 'claude_tunnel_token';
        // Item 5: refresh token lives in its own localStorage slot so it
        // can be revoked (logout) or rotated independently of the access
        // token. We keep them in separate keys so a future migration to
        // HttpOnly cookies for the refresh token only touches this file.
        this.refreshKey = 'claude_refresh_token';
        this.authScreen = null;
        this.errorElement = null;
        this.infoElement = null;
        this.loginButton = null;
        this.totpInput = null;
    }

    /**
     * Initialize auth screen UI
     */
    init() {
        this.authScreen = document.getElementById('auth-screen');
        this.renderAuthUI();
    }

    /**
     * Render authentication UI
     */
    renderAuthUI() {
        this.authScreen.innerHTML = `
            <div class="auth-container">
                <div class="auth-prompt">☁️ Cloude Code Authentication</div>
                <div class="auth-description">
                    enter your 6-digit totp code from your authenticator app to access claude code sessions.
                </div>

                <div class="auth-input-group">
                    <label class="auth-input-label" for="totp-input">► totp code:</label>
                    <input
                        type="text"
                        id="totp-input"
                        class="auth-input"
                        placeholder="000000"
                        maxlength="6"
                        autocomplete="off"
                        inputmode="numeric"
                        pattern="[0-9]*"
                    />
                </div>

                <button class="auth-button" id="login-btn">login</button>

                <div id="auth-error" class="auth-error hidden"></div>
                <div id="auth-info" class="auth-info hidden"></div>
            </div>
        `;

        // Get elements
        this.totpInput = document.getElementById('totp-input');
        this.loginButton = document.getElementById('login-btn');
        this.errorElement = document.getElementById('auth-error');
        this.infoElement = document.getElementById('auth-info');

        // Auto-focus input
        setTimeout(() => this.totpInput.focus(), 100);

        // Event listeners
        this.loginButton.addEventListener('click', () => this.handleLogin());
        this.totpInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.handleLogin();
            }
        });

        // Filter non-numeric input and auto-submit on 6 digits
        this.totpInput.addEventListener('input', (e) => {
            e.target.value = e.target.value.replace(/[^0-9]/g, '');

            // Auto-submit when 6 digits are entered
            if (e.target.value.length === 6) {
                this.handleLogin();
            }
        });

        // Check if setup is needed
        this.checkSetupStatus();
    }

    /**
     * Check if initial setup is needed
     */
    async checkSetupStatus() {
        try {
            // Try to get QR code - if this fails, config doesn't exist
            await window.API.getQRCode();
        } catch (error) {
            // 403 = qr endpoint is locked because the device is already
            // paired (server enforces .totp_paired sentinel). That's the
            // happy path for the login screen — user just needs to type
            // their TOTP, no setup banner required. Bail silently.
            //
            // Any OTHER non-200 (404 missing config, 500 server error,
            // network failure, etc.) means setup truly hasn't been done
            // or the backend is broken — surface the setup banner so the
            // first-run user knows to run setup_auth.py.
            if (error && error.status === 403) {
                this.infoElement.classList.add('hidden');
                return;
            }
            this.showSetupInstructions();
        }
    }

    /**
     * Show setup instructions
     */
    showSetupInstructions() {
        this.infoElement.classList.remove('hidden');
        this.infoElement.innerHTML = `
            <strong>⚠️ initial setup required</strong><br><br>
            run the setup script to configure authentication:<br>
            <code>python3 setup_auth.py</code><br><br>
            this will generate your totp secret and qr code for your authenticator app.
        `;
    }

    /**
     * Handle login button click
     */
    async handleLogin() {
        // Re-entry guard: the `input` listener auto-submits at 6 digits AND
        // the button click handler also calls handleLogin. Without this
        // guard, a rapid sequence (auto-submit fires → fetch pending →
        // post-error input clear → re-entry) can run client validation
        // against an empty input, producing "must be 6 digits" that
        // clobbers the real server error (e.g. a 429 rate-limit message)
        // in the UI. Rule: exactly one in-flight submit at a time. Any
        // re-entry while a fetch is pending is ignored, NOT re-validated.
        if (this._submitting) {
            return;
        }
        this._submitting = true;

        const totpCode = this.totpInput.value.trim();

        // Validate input
        if (totpCode.length !== 6) {
            this.showError('totp code must be 6 digits');
            this._submitting = false;
            return;
        }

        if (!/^\d{6}$/.test(totpCode)) {
            this.showError('totp code must contain only numbers');
            this._submitting = false;
            return;
        }

        // Disable button during login
        this.loginButton.disabled = true;
        this.loginButton.textContent = 'verifying...';
        this.hideError();
        this.hideInfo();

        try {
            // Verify TOTP code
            const response = await window.API.verifyTOTP(totpCode);

            // Server returns an access+refresh pair under the new contract.
            // Fall back to the deprecated `response.token` alias so clients
            // built against a slightly older build still work during rollout.
            const access = response.access_token || response.token;
            this.setToken(access);
            if (response.refresh_token) {
                this.setRefreshToken(response.refresh_token);
            }

            // Success - trigger authenticated event
            console.log('Auth: Login successful');
            window.dispatchEvent(new CustomEvent('authenticated'));

        } catch (error) {
            console.error('Auth: Login failed:', error);
            const errorMsg = error?.message || String(error) || 'authentication failed - invalid code';
            this.showError(errorMsg);
            this.totpInput.value = '';
            this.totpInput.focus();
        } finally {
            this.loginButton.disabled = false;
            this.loginButton.textContent = 'login';
            this._submitting = false;
        }
    }

    /**
     * Show error message
     */
    showError(message) {
        this.errorElement.textContent = `✗ ${message}`;
        this.errorElement.classList.remove('hidden');
    }

    /**
     * Hide error message
     */
    hideError() {
        this.errorElement.classList.add('hidden');
    }

    /**
     * Hide info message
     */
    hideInfo() {
        this.infoElement.classList.add('hidden');
    }

    /**
     * Get stored auth token
     */
    getToken() {
        return localStorage.getItem(this.tokenKey);
    }

    /**
     * Set auth token
     */
    setToken(token) {
        localStorage.setItem(this.tokenKey, token);
    }

    /**
     * Clear auth token(s). By default clears BOTH access + refresh, which
     * is what you want on logout / 401-with-failed-refresh. Pass
     * ``{ accessOnly: true }`` for the rare case where only the access
     * token should be dropped.
     */
    clearToken(opts = {}) {
        localStorage.removeItem(this.tokenKey);
        if (!opts.accessOnly) {
            localStorage.removeItem(this.refreshKey);
        }
    }

    /**
     * Refresh-token accessors (Item 5).
     */
    getRefreshToken() {
        return localStorage.getItem(this.refreshKey);
    }

    setRefreshToken(token) {
        if (token) {
            localStorage.setItem(this.refreshKey, token);
        }
    }

    /**
     * Exchange the stored refresh token for a new access+refresh pair.
     *
     * Returns true on success (tokens rotated in localStorage), false on
     * any failure (401, network error, no stored refresh). The API layer's
     * 401 interceptor uses this to transparently recover from expired
     * access tokens.
     *
     * NOTE: callers MUST funnel through the single-flight mutex in
     * `api.js` — calling this in parallel from multiple in-flight requests
     * will race and burn the refresh-token chain via server-side reuse
     * detection.
     *
     * @returns {Promise<boolean>}
     */
    async refresh() {
        const refreshToken = this.getRefreshToken();
        if (!refreshToken) {
            console.warn('Auth: refresh() called with no stored refresh token');
            return false;
        }

        try {
            const response = await fetch(`${window.API.baseURL}/auth/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken })
            });

            if (!response.ok) {
                // 401 = refresh rejected (expired, revoked, reuse-detected).
                // Any other non-2xx means the server is sick — treat it as
                // a refresh failure so the caller can fall back to login.
                console.warn('Auth: refresh failed with status', response.status);
                return false;
            }

            const data = await response.json();
            const access = data.access_token || data.token;
            if (!access) {
                console.warn('Auth: refresh response missing access_token');
                return false;
            }
            this.setToken(access);
            if (data.refresh_token) {
                this.setRefreshToken(data.refresh_token);
            }
            console.log('Auth: refreshed tokens');
            return true;
        } catch (e) {
            console.error('Auth: refresh threw', e);
            return false;
        }
    }

    /**
     * Check if user is authenticated
     */
    isAuthenticated() {
        return !!this.getToken();
    }

    /**
     * Logout user. Best-effort server-side revocation of the refresh
     * token, then clear local state regardless. We do NOT await the
     * server call to keep logout snappy — the server-side revoke is a
     * defense-in-depth nicety, not a blocking dependency.
     */
    logout() {
        console.log('Auth: Logging out');
        const refreshToken = this.getRefreshToken();
        if (refreshToken) {
            // Fire-and-forget: we don't care if this fails (the token
            // will expire on its own schedule) and we definitely don't
            // want to block the UI on a network round-trip.
            fetch(`${window.API.baseURL}/auth/logout`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken })
            }).catch((e) => console.warn('Auth: logout revoke failed', e));
        }
        this.clearToken();
        window.dispatchEvent(new CustomEvent('logged-out'));
    }

    /**
     * Verify token is still valid
     */
    async verifyToken() {
        try {
            await window.API.checkAuthStatus();
            return true;
        } catch (error) {
            console.error('Auth: Token verification failed:', error);
            return false;
        }
    }
}

// Export singleton instance
window.Auth = new Auth();
console.log('[Auth Module] Exported as window.Auth:', window.Auth);
