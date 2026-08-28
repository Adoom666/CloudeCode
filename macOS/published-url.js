/**
 * Which address the app may hand a browser, and when it must not guess.
 *
 * WHY THIS IS ITS OWN MODULE
 *
 * server-manager.js requires axios and electron, so nothing that imports it
 * can be tested without installing dependencies - and the node suites in
 * tests/*.node.mjs deliberately run with none. This rule is worth testing on
 * its own, so it lives here as a pure function with no imports at all, the
 * same shape as macOS/tray-status.js.
 *
 * THE BUG THIS ENCODES
 *
 * A fresh install has not finished setup, so src/core/setup_state.py pins the
 * server to 127.0.0.1 regardless of what HOST says. That is the lockdown, and
 * every new user is in it. The old rule preferred the MEASURED bind and fell
 * back to the CONFIGURED one, then mapped 0.0.0.0 to the primary LAN address.
 * So when the bind had not been measured yet - reportedBind is null because
 * the server was adopted rather than spawned, or its ready line had not been
 * parsed - a menu item handed the browser a LAN URL while the server was
 * listening only on loopback, and the user got a connection error from a
 * completely healthy server.
 *
 * Measured against a server in the lockdown shape:
 *     http://127.0.0.1:PORT/setup  -> 200
 *     http://<lan-ip>:PORT/setup   -> connection refused
 *
 * THREE OUTCOMES. "measured as 0.0.0.0" is a fact, and the LAN address is
 * genuinely right there - it is what the user shares with a phone.
 * "not measured" is the ABSENCE of that fact, not a weaker version of it.
 * Collapsing the two is what printed an aspiration as a measurement. Loopback
 * is correct under both (it is listening when the bind really is 0.0.0.0, and
 * it is the only thing listening under the lockdown), so the unmeasured case
 * resolves there instead of guessing.
 */

'use strict';

/**
 * Resolve the URL to open in the user's browser.
 *
 * @param {object} opts - Inputs.
 * @param {number|null} opts.port - The configured port, or null when it could
 *   not be determined.
 * @param {string|null} opts.configuredHost - The bind host from configuration.
 *   An aspiration, not a measurement.
 * @param {string|null} opts.measuredHost - The bind host the server REPORTED,
 *   or null when it has not said. Never substitute the configured value here.
 * @param {string|null} opts.lanIp - This machine's primary LAN address, or
 *   null when there is none.
 * @returns {string|null} The URL, or null when the port is undeterminable -
 *   an obviously-broken answer being the correct one, since a guessed-port
 *   URL would be opened and would fail confusingly.
 *
 * @example
 *   resolvePublishedUrl({port: 8000, configuredHost: '0.0.0.0',
 *                        measuredHost: null, lanIp: '10.0.1.86'})
 *   // 'http://127.0.0.1:8000'  - unmeasured, so no LAN guess
 */
function resolvePublishedUrl({ port, configuredHost, measuredHost, lanIp }) {
  if (port === null || port === undefined) return null;

  const host = measuredHost || configuredHost;

  if (host === '0.0.0.0') {
    // Unmeasured: the server may be locked down to loopback. Do not guess.
    if (!measuredHost) return `http://127.0.0.1:${port}`;
    return `http://${lanIp || '127.0.0.1'}:${port}`;
  }
  return `http://${host}:${port}`;
}

module.exports = { resolvePublishedUrl };
