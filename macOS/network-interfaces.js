// network-interfaces.js - which local addresses the server may bind to.
//
// WHY THIS FILE EXISTS
//
// The Bind IP submenu used to filter interfaces by NAME, with a blocklist of
// /^awdl/, /^llw/, /^utun/, /^anpi/, /^ap\d/ and a comment asserting those
// interfaces "never carry routable IPv4 even if one shows up".
//
// That assertion is false, and it is the reason the user could not bind to
// his Tailscale address. utun4 on his machine carries 100.113.217.77, a
// perfectly routable IPv4, and the menu silently refused to offer it. There
// was no error and no empty state to notice: the address simply was not in
// the list, which looks identical to not having one.
//
// The blocklist could not have been right in principle either. "utun" is
// just the macOS name for a userspace tunnel device; any VPN, not only
// Tailscale, can put a real address on one. A name tells you which driver
// created an interface, never what it carries.
//
// So the rule is now about the ADDRESS: an interface qualifies when it
// carries an IPv4 that is not loopback and not link-local, which is the
// precise definition of "an address a browser on another machine could
// reach". AirDrop (awdl/llw) and the Apple Silicon internal radios
// (anpi/ap1) fall out of that test for free, because they genuinely carry no
// IPv4 at all. Verified on the development machine: the only IPv4 interfaces
// present are lo0 (internal), en0 10.0.1.86 and utun4 100.113.217.77.

'use strict';

const os = require('os');

/** IPv4 link-local block (RFC 3927). Never a useful bind target. */
const LINK_LOCAL_V4_PREFIX = '169.254.';

/**
 * Whether one address entry is a usable bind target.
 *
 * @param {{family?: string, internal?: boolean, address?: string}} addr - One
 *   entry from os.networkInterfaces().
 * @returns {boolean} True when the address is a routable, non-loopback IPv4.
 */
function isBindableAddress(addr) {
  if (!addr) return false;
  // Node reports family as 'IPv4' on older releases and 4 on newer ones.
  const isV4 = addr.family === 'IPv4' || addr.family === 4;
  if (!isV4) return false;
  if (addr.internal) return false;
  if (typeof addr.address !== 'string') return false;
  if (addr.address.startsWith(LINK_LOCAL_V4_PREFIX)) return false;
  return true;
}

/**
 * List every local interface carrying a bindable IPv4 address.
 *
 * @param {object} [interfaces] - Result of os.networkInterfaces(). Injectable
 *   so tests can describe a machine without having to be on one.
 * @returns {Array<{iface: string, ip: string}>} Interface name and address
 *   pairs, sorted by interface name for a stable menu order.
 */
function listBindableIps(interfaces) {
  const source = interfaces || os.networkInterfaces() || {};
  const results = [];

  for (const [name, addrs] of Object.entries(source)) {
    if (!Array.isArray(addrs)) continue;
    for (const addr of addrs) {
      if (!isBindableAddress(addr)) continue;
      results.push({ iface: name, ip: addr.address });
    }
  }

  results.sort((a, b) => a.iface.localeCompare(b.iface));
  return results;
}

/**
 * Whether an address belongs to the Tailscale CGNAT range (100.64.0.0/10).
 *
 * Used only to LABEL the address in the menu, never to decide whether to
 * offer it. Tailscale addresses are ordinary routable IPv4 and are selected
 * by the same rule as everything else.
 *
 * @param {string} ip - Dotted-quad IPv4 address.
 * @returns {boolean} True when the address is inside 100.64.0.0/10.
 */
function isTailscaleIp(ip) {
  const parts = String(ip).split('.');
  if (parts.length !== 4) return false;
  const first = Number(parts[0]);
  const second = Number(parts[1]);
  if (!Number.isInteger(first) || !Number.isInteger(second)) return false;
  return first === 100 && second >= 64 && second <= 127;
}

module.exports = {
  LINK_LOCAL_V4_PREFIX,
  isBindableAddress,
  listBindableIps,
  isTailscaleIp,
};
