/**
 * Auth Module - Handles authentication, token management, and auth UI
 */

console.log('[Auth Module] Loading...');

class Auth {
    constructor() {
        this.tokenKey = 'claude_tunnel_token';
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
            // Config missing - show setup instructions
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
        const totpCode = this.totpInput.value.trim();

        // Validate input
        if (totpCode.length !== 6) {
            this.showError('totp code must be 6 digits');
            return;
        }

        if (!/^\d{6}$/.test(totpCode)) {
            this.showError('totp code must contain only numbers');
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

            // Store token
            this.setToken(response.token);

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
     * Clear auth token
     */
    clearToken() {
        localStorage.removeItem(this.tokenKey);
    }

    /**
     * Check if user is authenticated
     */
    isAuthenticated() {
        return !!this.getToken();
    }

    /**
     * Logout user
     */
    logout() {
        console.log('Auth: Logging out');
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
