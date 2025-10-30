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
    }

    /**
     * Get auth token from localStorage
     */
    getToken() {
        return localStorage.getItem('claude_tunnel_token');
    }

    /**
     * Make authenticated API call
     * @param {string} endpoint - API endpoint (e.g., '/sessions')
     * @param {object} options - fetch options
     * @returns {Promise<any>} - Response data
     */
    async call(endpoint, options = {}) {
        const token = this.getToken();

        // Prepare headers
        const headers = options.headers || {};
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

            // Handle 401 Unauthorized - trigger re-authentication
            if (response.status === 401) {
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
     * Handle unauthorized response
     */
    handleUnauthorized() {
        // Clear token
        localStorage.removeItem('claude_tunnel_token');

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
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Invalid TOTP code');
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
            throw new Error(errorData.detail || 'Failed to get QR code');
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
     * Sessions: Create new session
     * @param {object} params - {working_dir?: string, auto_start_claude?: boolean, copy_templates?: boolean}
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
     * Sessions: Destroy current session
     * @returns {Promise<object>}
     */
    async destroySession() {
        return await this.call('/sessions', {
            method: 'DELETE'
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
     * Get WebSocket URL with token
     * @returns {string} - WebSocket URL with token
     */
    getWebSocketURL() {
        const token = this.getToken();
        return `${this.wsBaseURL}/ws/terminal${token ? `?token=${token}` : ''}`;
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
