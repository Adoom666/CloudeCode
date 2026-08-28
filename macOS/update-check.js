/**
 * Update NOTIFIER - deliberately not an auto-updater.
 *
 * WHY NOT AUTO-UPDATE. macOS auto-update runs through Squirrel.Mac, which
 * refuses to apply an update whose signature it cannot validate. This app
 * is ad-hoc signed ("0 valid identities found"), so a real auto-updater
 * needs a paid Developer ID certificate plus notarization before it can
 * work at all - and it would then swap an app bundle whose Python child
 * process owns a schema-versioned database, where a mismatched pair is a
 * broken install rather than a failed download.
 *
 * A notifier has none of that surface: it reads a version, compares, and
 * tells the user. No certificate, nothing swapped, nothing to roll back.
 *
 * THREE OUTCOMES, because "no update available" and "could not check"
 * must never render the same. A notifier that silently reports
 * up-to-date whenever the network is down is worse than no notifier: it
 * actively tells the user a falsehood they will act on.
 */

const UPDATE_FEED_URL =
  'https://api.github.com/repos/ccsliinc/CloudeCode/releases/latest';

const CHECK_TIMEOUT_MS = 6000;

const RESULT_CURRENT = 'current';
const RESULT_AVAILABLE = 'available';
const RESULT_UNKNOWN = 'unknown';

/**
 * Compare two semver-ish strings.
 *
 * Description: numeric, component-wise, tolerant of a leading "v" and of
 *   differing lengths ("1.2" vs "1.2.0"). Returns null when either side
 *   cannot be parsed - a comparison against an unparseable version is not
 *   0, it is unknown, and returning 0 would report "current" for a
 *   version nobody understood.
 * Inputs: a (string), b (string).
 * Output: -1 | 0 | 1 | null
 */
function compareVersions(a, b) {
  const parse = (v) => {
    if (typeof v !== 'string') return null;
    const cleaned = v.trim().replace(/^v/i, '');
    if (!/^\d+(\.\d+)*$/.test(cleaned)) return null;
    return cleaned.split('.').map((n) => parseInt(n, 10));
  };
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return null;
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] === undefined ? 0 : pa[i];
    const y = pb[i] === undefined ? 0 : pb[i];
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/**
 * Ask the feed what the latest published version is.
 *
 * Description: NEVER THROWS. Every failure - offline, timeout, rate
 *   limit, unparseable body, a tag that is not a version - resolves to
 *   RESULT_UNKNOWN with a reason. The caller renders that as "could not
 *   check", never as "up to date".
 * Inputs: currentVersion (string) - typically app.getVersion().
 *   fetchImpl (function, optional) - injected for tests.
 * Output: Promise<{result, current, latest, url, detail}>
 */
async function checkForUpdate(currentVersion, fetchImpl) {
  const doFetch = fetchImpl || globalThis.fetch;
  if (typeof doFetch !== 'function') {
    return {
      result: RESULT_UNKNOWN, current: currentVersion, latest: null,
      url: null, detail: 'no fetch implementation available'
    };
  }
  let body;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CHECK_TIMEOUT_MS);
    let response;
    try {
      response = await doFetch(UPDATE_FEED_URL, {
        signal: controller.signal,
        headers: { Accept: 'application/vnd.github+json' }
      });
    } finally {
      clearTimeout(timer);
    }
    if (!response || !response.ok) {
      return {
        result: RESULT_UNKNOWN, current: currentVersion, latest: null,
        url: null,
        detail: `feed returned ${response ? response.status : 'no response'}`
      };
    }
    body = await response.json();
  } catch (err) {
    return {
      result: RESULT_UNKNOWN, current: currentVersion, latest: null,
      url: null, detail: (err && err.message) || String(err)
    };
  }

  const latest = body && (body.tag_name || body.name);
  const cmp = compareVersions(currentVersion, latest);
  if (cmp === null) {
    return {
      result: RESULT_UNKNOWN, current: currentVersion, latest: latest || null,
      url: (body && body.html_url) || null,
      detail: `could not compare "${currentVersion}" with "${latest}"`
    };
  }
  return {
    result: cmp < 0 ? RESULT_AVAILABLE : RESULT_CURRENT,
    current: currentVersion,
    latest: String(latest).replace(/^v/i, ''),
    url: (body && body.html_url) || null,
    detail: null
  };
}

module.exports = {
  UPDATE_FEED_URL,
  RESULT_CURRENT,
  RESULT_AVAILABLE,
  RESULT_UNKNOWN,
  compareVersions,
  checkForUpdate
};
