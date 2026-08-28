// tray-api.js - the tray's authenticated read-only client for the server.
//
// WHY THIS FILE EXISTS
//
// The tray needs two facts it could not previously see: which sessions need
// attention, and whether an update is available. Both endpoints are
// TOTP-gated, so the question was whether the tray can authenticate at all.
//
// IT CAN, and the route is already sitting in the app. server-manager.js
// reads TOTP_SECRET out of the server's .env to render the "Copy OTP" menu
// item, so the tray holds the shared secret and can mint a valid code with
// the bundled totp.js. POST /api/v1/auth/verify exchanges that code for a
// JWT access token plus a refresh token. Verified live against the running
// server before this module was written.
//
// WHY THE TOKEN IS CACHED AND REFRESHED RATHER THAN RE-MINTED
//
// The server keeps a TOTP replay cache (src/api/auth.py, 90 second TTL) and
// rate limits verification per minute and per hour. A code is single use. So
// a tray that verified a fresh code on every poll would burn the rate limit,
// and worse, would consume codes the user is about to type into his browser,
// making his own login fail as a replay. This client therefore verifies ONCE,
// then rides the refresh token, and only falls back to a new TOTP code when
// refreshing fails.
//
// THREE OUTCOMES, NOT TWO
//
// Every method here distinguishes "I asked and the answer is X" from "I could
// not ask". A failed request returns reachable:false with the reason, never
// an empty list. An empty list means the server was asked and genuinely has
// no sessions. Collapsing those two is precisely how a tray icon ends up
// looking healthy while the server is unreachable.

'use strict';

/** Access tokens are minted with a 4 hour TTL; refresh well before that. */
const TOKEN_REFRESH_MARGIN_SECONDS = 300;

/** Network calls that hang would stall the whole poll loop. */
const REQUEST_TIMEOUT_MS = 8000;

/**
 * Read-only client the tray uses to poll session and update state.
 */
class TrayApiClient {
  /**
   * @param {{baseUrl: string, getOtp: () => (string|null),
   *   fetchImpl?: Function, now?: () => number}} options - `baseUrl` is the
   *   server origin including scheme. `getOtp` returns a currently valid
   *   6-digit TOTP code, or null when the secret is unavailable. `fetchImpl`
   *   and `now` exist for tests.
   */
  constructor(options) {
    this.baseUrl = String(options.baseUrl || '').replace(/\/+$/, '');
    this.getOtp = options.getOtp;
    this.fetchImpl = options.fetchImpl || global.fetch;
    this.now = options.now || (() => Date.now());

    this.accessToken = null;
    this.accessTokenExpiresAt = 0;
    this.refreshToken = null;
  }

  /**
   * Point the client at a different origin, discarding any cached token.
   *
   * The bind address is user-changeable from the menu, and a token minted
   * against one origin should not be replayed at another.
   *
   * @param {string} baseUrl - New server origin.
   * @returns {void}
   */
  setBaseUrl(baseUrl) {
    const next = String(baseUrl || '').replace(/\/+$/, '');
    if (next === this.baseUrl) return;
    this.baseUrl = next;
    this.forgetToken();
  }

  /**
   * Drop the cached credentials, forcing a fresh TOTP exchange next call.
   * @returns {void}
   */
  forgetToken() {
    this.accessToken = null;
    this.accessTokenExpiresAt = 0;
    this.refreshToken = null;
  }

  /**
   * Issue one JSON request with a timeout.
   *
   * @param {string} routePath - Path beginning with a slash.
   * @param {{method?: string, body?: object, token?: (string|null)}} options -
   *   Request shape.
   * @returns {Promise<{ok: boolean, status: number, data: (object|null),
   *   error: (string|null)}>} Never throws; a transport failure is reported
   *   as ok:false with a reason.
   */
  async request(routePath, options) {
    const opts = options || {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    try {
      const headers = { Accept: 'application/json' };
      if (opts.body) headers['Content-Type'] = 'application/json';
      if (opts.token) headers.Authorization = `Bearer ${opts.token}`;

      const response = await this.fetchImpl(this.baseUrl + routePath, {
        method: opts.method || 'GET',
        headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });

      let data = null;
      try {
        data = await response.json();
      } catch (parseError) {
        data = null;
      }

      return {
        ok: response.ok,
        status: response.status,
        data,
        error: response.ok ? null : `HTTP ${response.status}`,
      };
    } catch (transportError) {
      return {
        ok: false,
        status: 0,
        data: null,
        error: String((transportError && transportError.message) || transportError),
      };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Store the credentials returned by verify or refresh.
   *
   * @param {object} data - Response body carrying access_token/expires_in.
   * @returns {boolean} True when an access token was present.
   */
  _adoptTokens(data) {
    if (!data || !data.access_token) return false;
    this.accessToken = data.access_token;
    const ttl = Number(data.expires_in) || 0;
    this.accessTokenExpiresAt = this.now() + Math.max(0, ttl) * 1000;
    if (data.refresh_token) this.refreshToken = data.refresh_token;
    return true;
  }

  /**
   * Return a usable access token, minting or refreshing one if needed.
   *
   * Order: reuse a token that is not near expiry, else refresh, else spend a
   * TOTP code. Spending a code is last because codes are single use and
   * shared with the user's own browser login.
   *
   * @returns {Promise<{token: (string|null), error: (string|null)}>} A null
   *   token always carries a reason, so the caller can report CANNOT
   *   DETERMINE rather than inventing a verdict.
   */
  async ensureToken() {
    const marginMs = TOKEN_REFRESH_MARGIN_SECONDS * 1000;
    if (this.accessToken && this.now() + marginMs < this.accessTokenExpiresAt) {
      return { token: this.accessToken, error: null };
    }

    if (this.refreshToken) {
      const refreshed = await this.request('/api/v1/auth/refresh', {
        method: 'POST',
        body: { refresh_token: this.refreshToken },
      });
      if (refreshed.ok && this._adoptTokens(refreshed.data)) {
        return { token: this.accessToken, error: null };
      }
      this.refreshToken = null;
    }

    let code = null;
    try {
      code = this.getOtp ? this.getOtp() : null;
    } catch (otpError) {
      return { token: null, error: 'could not compute a TOTP code' };
    }
    if (!code) return { token: null, error: 'no TOTP secret available' };

    const verified = await this.request('/api/v1/auth/verify', {
      method: 'POST',
      body: { code },
    });
    if (verified.ok && this._adoptTokens(verified.data)) {
      return { token: this.accessToken, error: null };
    }

    return {
      token: null,
      error: verified.error || 'TOTP verification was rejected',
    };
  }

  /**
   * Fetch a JSON resource with authentication, retrying once after a 401 in
   * case the cached token was revoked server-side.
   *
   * @param {string} routePath - Path beginning with a slash.
   * @returns {Promise<{reachable: boolean, data: (object|null),
   *   error: (string|null)}>} reachable:false means the value could not be
   *   determined, and is never to be rendered as a healthy value.
   */
  async getAuthed(routePath) {
    const first = await this.ensureToken();
    if (!first.token) return { reachable: false, data: null, error: first.error };

    let response = await this.request(routePath, { token: first.token });
    if (response.status === 401) {
      this.forgetToken();
      const second = await this.ensureToken();
      if (!second.token) {
        return { reachable: false, data: null, error: second.error };
      }
      response = await this.request(routePath, { token: second.token });
    }

    if (!response.ok) {
      return { reachable: false, data: null, error: response.error };
    }
    return { reachable: true, data: response.data, error: null };
  }

  /**
   * Fetch the live session list.
   *
   * @returns {Promise<{reachable: boolean, sessions: (Array<object>|null),
   *   error: (string|null)}>} An empty array means the server was asked and
   *   has no sessions. reachable:false means it could not be asked.
   */
  async fetchSessions() {
    const result = await this.getAuthed('/api/v1/sessions/list');
    if (!result.reachable) {
      return { reachable: false, sessions: null, error: result.error };
    }
    return {
      reachable: true,
      sessions: Array.isArray(result.data) ? result.data : [],
      error: null,
    };
  }

  /**
   * Fetch the update-availability verdict.
   *
   * src/core/update_check.py already models three outcomes ("current",
   * "update_available", "unknown") and deliberately never auto-updates, so
   * this is a pass-through rather than a new judgement.
   *
   * @returns {Promise<{reachable: boolean, status: (string|null),
   *   latestVersion: (string|null), error: (string|null)}>} status null with
   *   reachable false means the check could not be performed at all.
   */
  async fetchUpdateStatus() {
    const result = await this.getAuthed('/api/v1/version');
    if (!result.reachable) {
      return { reachable: false, status: null, latestVersion: null, error: result.error };
    }
    const update = (result.data && result.data.update) || {};
    return {
      reachable: true,
      status: update.status || 'unknown',
      latestVersion: update.latest_version || null,
      error: null,
    };
  }
}

module.exports = { TrayApiClient, TOKEN_REFRESH_MARGIN_SECONDS };
