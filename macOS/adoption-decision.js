/**
 * May this bundle adopt the Cloude Code server that is already on the port?
 *
 * WHY THIS EXISTS
 *
 * Measured on the user's machine, 2026-08-25:
 *
 *   Electron (v1.0.3) started            14:33:52
 *   the Python server on :8000 was pid   5491
 *   that pid started                     10:45:14
 *   its parent pid was                   1
 *
 * ppid 1 is launchd, and a process only arrives there by outliving its parent.
 * A v1.0.2 app had orphaned its server on quit; the freshly installed v1.0.3
 * started, found a healthy listener on 8000, and adopted it. The user ran
 * v1.0.2 SERVER code under a v1.0.3 bundle for four hours, with an in-memory
 * config cache four hours stale, which manufactured a false divergence report
 * and cost a separate investigation to unpick.
 *
 * ADOPTION IS NOT THE BUG. It is correct and wanted when Electron itself
 * crashed and left its own healthy server behind: double-spawning there
 * collides on the port and would take down live tmux sessions for nothing.
 * The bug is that an adopted server carries NO GUARANTEE it is running this
 * bundle's code, and start() never asked.
 *
 * WHAT THE APP CAN ACTUALLY KNOW
 *
 * The bundle knows its own version (`app.getVersion()`, from package.json in
 * the asar). The running server can be asked for its own - but only over an
 * UNAUTHENTICATED endpoint, because this decision is made before any user has
 * logged in. `GET /api/v1/version` is auth-gated
 * (`dependencies=[Depends(require_auth)]`), so a 401 would be the normal
 * answer there and the gate would be useless. `GET /api/v1/health` is
 * deliberately unauthenticated for exactly this reason, is already polled by
 * the tray, and now carries `version` - frozen at the server's own startup,
 * not re-resolved per request, because bootstrap.js rewrites the on-disk
 * VERSION file on every packaged launch and a fresh resolve would have the OLD
 * process report the NEW number. See src/core/version.py.
 *
 * FOUR OUTCOMES, NOT TWO
 *
 * The two unknown states are the point. An upgrade that CANNOT PROVE a match
 * is not a licence to adopt: the orphaned server was in exactly that state,
 * since no version was reported at all. "I could not tell" resolving to "yes"
 * is how every false green in this project has worked.
 *
 * This module has no imports so the decision can be driven in a plain node
 * test - server-manager.js requires electron and axios at module scope and is
 * otherwise only checkable by reading its source text. See
 * tests/test_adoption_is_version_gated.node.mjs.
 *
 * @module adoption-decision
 */

/** The running server is provably this bundle's code. Adopt. */
const ADOPT_MATCH = 'match';
/** The running server is provably DIFFERENT code. Refuse. */
const ADOPT_MISMATCH = 'mismatch';
/** The running server did not say. CANNOT DETERMINE. Refuse. */
const ADOPT_RUNNING_UNKNOWN = 'running-unknown';
/** This bundle does not know its own version. CANNOT DETERMINE. Refuse. */
const ADOPT_BUNDLE_UNKNOWN = 'bundle-unknown';

/**
 * Reduce a version to a comparable form, or to null when there is not one.
 *
 * Strips surrounding whitespace and a single leading "v", because the bundle
 * reads a bare "1.0.3" out of package.json while a server may have resolved
 * "v1.0.3" from a git tag. A refusal caused purely by punctuation would fire
 * on every launch, and the fix people reach for when a gate cries wolf is
 * turning the gate off.
 *
 * Everything that is not a non-empty string is null. Nothing is coerced: a
 * number 1.03 is not a version, it is a different type arriving where a
 * version was expected, and guessing what it meant is how a comparison starts
 * returning answers nobody measured.
 *
 * @param {unknown} value - Candidate version.
 * @returns {string|null} Normalised version, or null when there is none.
 *
 * @example
 * normalizeVersion(' v1.0.3 '); // '1.0.3'
 * normalizeVersion('');         // null
 */
function normalizeVersion(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim().replace(/^v/i, '').trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * Decide whether an already-running server may be adopted.
 *
 * Note that a build suffix counts as a real difference: "1.0.3-2-gabc1234" is
 * not "1.0.3". `describe_git_tag` produces that form and it means "this
 * install is not at a release tag", which is exactly the sort of thing worth
 * refusing over rather than rounding off.
 *
 * @param {object} [input] - What is known.
 * @param {unknown} [input.runningVersion] - Version reported by the server on
 *   the port, from GET /api/v1/health. Missing or blank is normal for any
 *   server predating this change.
 * @param {unknown} [input.bundleVersion] - This bundle's own version.
 * @returns {{adopt: boolean, outcome: string, reason: string,
 *   runningVersion: string|null, bundleVersion: string|null}} The verdict, and
 *   a reason written to be shown to a person.
 *
 * @example
 * decideAdoption({ runningVersion: '1.0.2', bundleVersion: '1.0.3' });
 * // { adopt: false, outcome: 'mismatch', reason: 'The server ...' }
 */
function decideAdoption(input) {
  const source = input || {};
  const running = normalizeVersion(source.runningVersion);
  const bundle = normalizeVersion(source.bundleVersion);

  // Order matters. Check the unknowns FIRST, because two nulls compare equal
  // and would otherwise sail through as a match - which is the single most
  // likely way this gate gets accidentally disabled by a later edit.
  if (running === null) {
    return {
      adopt: false,
      outcome: ADOPT_RUNNING_UNKNOWN,
      runningVersion: null,
      bundleVersion: bundle,
      reason:
        'A Cloude Code server is already running on this port, but it did not ' +
        'report which version it is. That is not proof it is out of date, and ' +
        'it is not proof it is current either - it cannot be determined. ' +
        'Adopting it anyway is how this app once spent four hours running an ' +
        'older version\'s server code.',
    };
  }

  if (bundle === null) {
    return {
      adopt: false,
      outcome: ADOPT_BUNDLE_UNKNOWN,
      runningVersion: running,
      bundleVersion: null,
      reason:
        `A Cloude Code server reporting version ${running} is already running ` +
        'on this port, but this app could not determine its OWN version, so ' +
        'there is nothing to compare it against. Comparing against an unknown ' +
        'is not a match.',
    };
  }

  if (running === bundle) {
    return {
      adopt: true,
      outcome: ADOPT_MATCH,
      runningVersion: running,
      bundleVersion: bundle,
      reason:
        `The server already on this port reports version ${running}, which is ` +
        'this app\'s own version, so it is this app\'s code. Adopting it ' +
        'rather than starting a second one.',
    };
  }

  return {
    adopt: false,
    outcome: ADOPT_MISMATCH,
    runningVersion: running,
    bundleVersion: bundle,
    reason:
      `The server already running on this port is version ${running}. This ` +
      `app is version ${bundle}. Adopting it would mean running ${running}'s ` +
      `server code under a ${bundle} app, which is not what an upgrade is ` +
      'supposed to do.',
  };
}

module.exports = {
  decideAdoption,
  normalizeVersion,
  ADOPT_MATCH,
  ADOPT_MISMATCH,
  ADOPT_RUNNING_UNKNOWN,
  ADOPT_BUNDLE_UNKNOWN,
};
