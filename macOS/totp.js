// RFC 6238 TOTP + RFC 4648 base32 decode, zero deps, Node-native crypto.
// Used by server-manager.js to surface the currently-valid 6-digit code in
// the tray menu ("Copy OTP: 123456"). Matches pyotp byte-for-byte for the
// same secret at the same epoch second.
//
// Algorithm (TOTP = HOTP with time-derived counter):
//   counter = floor(unix_time / period)       // period = 30s
//   hmac    = HMAC-SHA1(base32decode(secret), big-endian 8-byte counter)
//   offset  = hmac[19] & 0x0f                 // dynamic truncation
//   code    = ((hmac[offset]   & 0x7f) << 24) // mask sign bit of MSB
//           | ((hmac[offset+1] & 0xff) << 16)
//           | ((hmac[offset+2] & 0xff) <<  8)
//           | ( hmac[offset+3] & 0xff)
//   return zero-pad-left( code % 10^digits, digits )

const crypto = require('crypto');

/**
 * RFC 4648 base32 decode. Case-insensitive. Pad `=` stripped. Whitespace
 * inside the string is tolerated (matches pyotp's lenient behavior). Any
 * character outside A-Z / 2-7 throws.
 *
 * Approach: accumulate 5-bit groups into a bit-string, then slice into
 * 8-bit bytes. Trailing partial bit-group (less than 8 bits leftover) is
 * discarded per spec — that group never represents a full output byte.
 */
function base32decode(input) {
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  const clean = String(input).toUpperCase().replace(/=+$/, '').replace(/\s+/g, '');
  let bits = '';
  for (const ch of clean) {
    const v = alpha.indexOf(ch);
    if (v < 0) throw new Error(`Invalid base32 char: ${ch}`);
    bits += v.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

/**
 * Current 6-digit TOTP for a base32-encoded secret. Optional overrides for
 * step (default 30s) and digit count (default 6). `nowMs` override is for
 * unit tests — production callers omit it and it defaults to Date.now().
 */
function currentTotp(secretBase32, step = 30, digits = 6, nowMs = Date.now()) {
  const key = base32decode(secretBase32);
  const counter = Math.floor(nowMs / 1000 / step);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter), 0);
  const hmac = crypto.createHmac('sha1', key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0xf;
  const code = ((hmac[offset]     & 0x7f) << 24)
             | ((hmac[offset + 1] & 0xff) << 16)
             | ((hmac[offset + 2] & 0xff) <<  8)
             | ( hmac[offset + 3] & 0xff);
  return String(code % (10 ** digits)).padStart(digits, '0');
}

/**
 * Seconds remaining in the current TOTP window (0–29 for 30s step).
 * Used for the optional "(rolls in Xs)" decoration on the menu label.
 */
function secondsUntilRollover(step = 30, nowMs = Date.now()) {
  const secs = Math.floor(nowMs / 1000);
  return step - (secs % step);
}

module.exports = { base32decode, currentTotp, secondsUntilRollover };
