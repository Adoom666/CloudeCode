// tls-status.js - is the current binding actually a secure connection?
//
// WHY THIS FILE EXISTS
//
// The user asked to know whether he is on a secure connection or not. The
// dangerous way to answer that is to render a padlock whenever the URL starts
// with https, because that reports the SCHEME, which is a thing the app typed
// itself, rather than the connection, which is a thing that has to be
// measured.
//
// THREE OUTCOMES, AND THE MIDDLE ONE IS NOT "PROBABLY FINE".
//
//   secure    - TLS is on, the certificate is in date, AND it is valid for
//               the exact hostname being used.
//   insecure  - plaintext, or TLS whose certificate does not actually cover
//               this connection. Both mean "do not tell him he is protected".
//   unknown   - TLS is on but the certificate could not be evaluated at all.
//               Not a padlock, and not an all-clear.
//
// A certificate for the WRONG NAME is deliberately classed insecure rather
// than unknown. It is not a future risk to keep an eye on; every browser
// rejects it today, so the connection is broken now. That distinction comes
// straight from an incident where a days-remaining expiry check reported OK
// forever on domains serving a certificate for a completely different
// hostname: the check only ever parsed notAfter and never compared the name
// it asked for against the name it got back.
//
// Name matching therefore happens BEFORE time. SAN is authoritative and CN is
// consulted only when there is no SAN at all, because browsers ignore CN
// whenever a SAN is present. Wildcards match exactly one label, per RFC 6125.

'use strict';

/** Security verdicts. Exported so callers cannot invent a fourth. */
const LEVEL_SECURE = 'secure';
const LEVEL_INSECURE = 'insecure';
const LEVEL_UNKNOWN = 'unknown';

/**
 * Split a URL into the parts that matter for this judgement.
 *
 * @param {string} url - Absolute URL, for example "http://10.0.1.86:8000".
 * @returns {{scheme: (string|null), host: (string|null)}} Lowercased scheme
 *   and hostname, or nulls when the URL could not be parsed. Nulls are a
 *   could-not-evaluate signal, never a pass.
 */
function parseUrlParts(url) {
  try {
    const parsed = new URL(String(url));
    return {
      scheme: parsed.protocol.replace(/:$/, '').toLowerCase(),
      host: parsed.hostname.toLowerCase(),
    };
  } catch (error) {
    return { scheme: null, host: null };
  }
}

/**
 * Whether a hostname is a bare IP address rather than a DNS name.
 *
 * Certificates for private IP addresses effectively do not exist from a
 * public CA, so an IP binding can never be name-matched. Saying that plainly
 * beats reporting a mismatch against an identity no certificate could carry.
 *
 * @param {string} host - Hostname to inspect.
 * @returns {boolean} True for an IPv4 or bracketed IPv6 literal.
 */
function isIpLiteral(host) {
  if (!host) return false;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return true;
  return host.includes(':');
}

/**
 * Match a hostname against one certificate name, honouring RFC 6125
 * wildcards.
 *
 * A wildcard matches exactly ONE label. "*.example.com" covers
 * "a.example.com" and does NOT cover "a.b.example.com" or "example.com".
 *
 * @param {string} host - Hostname being connected to.
 * @param {string} candidate - One SAN entry or CN from the certificate.
 * @returns {boolean} True when the certificate name covers the hostname.
 */
function nameMatches(host, candidate) {
  const name = String(candidate || '').toLowerCase().replace(/\.$/, '');
  const target = String(host || '').toLowerCase().replace(/\.$/, '');
  if (!name || !target) return false;
  if (name === target) return true;
  if (!name.startsWith('*.')) return false;

  const suffix = name.slice(2);
  if (!target.endsWith('.' + suffix)) return false;
  const label = target.slice(0, target.length - suffix.length - 1);
  return label.length > 0 && !label.includes('.');
}

/**
 * Whether any name on the certificate covers the hostname.
 *
 * SAN wins outright. CN is consulted ONLY when the certificate carries no
 * SAN at all, matching what browsers actually do.
 *
 * @param {string} host - Hostname being connected to.
 * @param {{subjectAltNames?: Array<string>, commonName?: (string|null)}} cert -
 *   Certificate identity.
 * @returns {boolean} True when the certificate is valid for this hostname.
 */
function certCoversHost(host, cert) {
  const sans = (cert && cert.subjectAltNames) || [];
  if (sans.length > 0) return sans.some((san) => nameMatches(host, san));
  if (cert && cert.commonName) return nameMatches(host, cert.commonName);
  return false;
}

/**
 * Judge the security of the current binding.
 *
 * @param {{url: string, cert?: (object|null), certError?: (string|null),
 *   now?: number}} input - The URL in use, the certificate observed on that
 *   endpoint (null when TLS is not configured or the probe failed),
 *   an optional probe error, and the current epoch ms for testing.
 * @returns {{level: string, label: string, detail: string}} A verdict, a
 *   short menu label, and a sentence naming what was or was not measured.
 */
function evaluateBinding(input) {
  const now = (input && input.now) || Date.now();
  const { scheme, host } = parseUrlParts(input && input.url);

  if (!scheme) {
    return {
      level: LEVEL_UNKNOWN,
      label: 'Connection: cannot determine',
      detail: 'The server URL could not be parsed, so its security was never measured.',
    };
  }

  if (scheme !== 'https') {
    return {
      level: LEVEL_INSECURE,
      label: 'Connection: not secure (plain HTTP)',
      detail:
        'Traffic to ' + host + ' is unencrypted. Anything on the network path ' +
        'can read it, including the TOTP code and the session token.',
    };
  }

  if (input && input.certError) {
    return {
      level: LEVEL_UNKNOWN,
      label: 'Connection: cannot determine',
      detail: 'TLS is configured but the certificate could not be read: ' + input.certError,
    };
  }

  const cert = input && input.cert;
  if (!cert) {
    return {
      level: LEVEL_UNKNOWN,
      label: 'Connection: cannot determine',
      detail: 'TLS is configured but no certificate was observed on the endpoint.',
    };
  }

  // IDENTITY BEFORE TIME. An in-date certificate for the wrong name is an
  // outage today, not a risk for later.
  if (isIpLiteral(host)) {
    return {
      level: LEVEL_UNKNOWN,
      label: 'Connection: cannot determine',
      detail:
        'The server is bound to the IP address ' + host + ', which a ' +
        'certificate cannot be issued for. Bind to a hostname to get a ' +
        'verifiable identity.',
    };
  }

  if (!certCoversHost(host, cert)) {
    const names = (cert.subjectAltNames || []).join(', ') || cert.commonName || 'none';
    return {
      level: LEVEL_INSECURE,
      label: 'Connection: not secure (wrong certificate)',
      detail:
        'The certificate served for ' + host + ' is valid for ' + names +
        ' instead. Every browser rejects this, so it is broken now, not later.',
    };
  }

  const notAfter = Number(cert.notAfter);
  if (!Number.isFinite(notAfter)) {
    return {
      level: LEVEL_UNKNOWN,
      label: 'Connection: cannot determine',
      detail: 'The certificate for ' + host + ' has no readable expiry date.',
    };
  }

  if (notAfter <= now) {
    return {
      level: LEVEL_INSECURE,
      label: 'Connection: not secure (certificate expired)',
      detail: 'The certificate for ' + host + ' expired on ' +
        new Date(notAfter).toISOString().slice(0, 10) + '.',
    };
  }

  const daysLeft = Math.floor((notAfter - now) / 86400000);
  return {
    level: LEVEL_SECURE,
    label: 'Connection: secure (HTTPS)',
    detail:
      'TLS to ' + host + ' using a certificate valid for that exact name, ' +
      daysLeft + ' days remaining.',
  };
}

module.exports = {
  LEVEL_SECURE,
  LEVEL_INSECURE,
  LEVEL_UNKNOWN,
  parseUrlParts,
  isIpLiteral,
  nameMatches,
  certCoversHost,
  evaluateBinding,
};
