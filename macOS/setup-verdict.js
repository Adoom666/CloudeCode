// setup-verdict.js - is this instance set up, and who says so.
//
// WHY THIS FILE EXISTS
//
// The owner ran the setup script, it succeeded, the app restarted, and the
// menu bar still offered "Run Setup Script". It would have done so forever.
//
// ServerManager.checkConfiguration() had its own private definition of
// "configured", and it required CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID and
// CLOUDFLARE_DOMAIN - left over from a tunnel feature removed in plan v3.2.
// Nothing in the product writes those keys any more; measured, the only file
// in the repository that still named them was server-manager.js itself. The
// condition was unsatisfiable by construction, so running setup again could
// never clear it, and the row was not reporting a state - it was furniture.
//
// THE STALE LIST WAS THE SYMPTOM. THE THIRD OPINION WAS THE DEFECT.
//
// There were three independent answers to one question: the server's
// evaluate_setup_state (config parses, both secrets, an authenticator
// paired), the wizard's auth guard which agrees with it by construction, and
// this private one. The answer the user actually SAW was the wrong one.
// Deleting three variable names would have fixed his afternoon and left the
// shape that produced it - a second list, free to drift, with nothing
// watching.
//
// So: THE SERVER IS THE AUTHORITY. Whenever it has answered, its verdict is
// the verdict. The local evaluation here exists for exactly one case, the
// genuine one, which is that on a first run there is no server to ask yet
// and the tray still has to decide whether to offer the setup row. It reads
// THE SAME FACTS the server reads, and tests/test_setup_verdict_authority
// .node.mjs antijoins the two fact lists against src/core/setup_state.py in
// both directions so they cannot drift apart in silence. A fact the server
// requires and this does not would let the tray call an unfinished install
// finished; a fact this requires and the server does not is the Cloudflare
// bug happening again.
//
// AND A STOPPED SERVER IS NOT AN UNCONFIGURED ONE. "I could not ask" is a
// third outcome with its own name here, and the caller must not offer the
// setup row on it - sending somebody to re-run a setup that was never the
// problem is the same defect pointed the other way.

'use strict';

/**
 * Environment keys setup must have written, mirroring the secrets
 * src/core/setup_state.py evaluates.
 *
 * Deliberately short. Every entry is a condition setup itself can satisfy -
 * that is the property the removed Cloudflare keys did not have.
 *
 * @type {ReadonlyArray<string>}
 */
const REQUIRED_ENV_KEYS = Object.freeze(['TOTP_SECRET', 'JWT_SECRET']);

/**
 * The fact keys this module evaluates, named identically to the server's
 * SetupCheck keys in src/core/setup_state.py so the two can be antijoined.
 *
 * @type {ReadonlyArray<string>}
 */
const CHECK_KEYS = Object.freeze([
  'config_file',
  'totp_secret',
  'jwt_secret',
  'totp_paired',
]);

/** Verdict meaning every fact was checked and passed. @type {string} */
const COMPLETE = 'complete';
/** Verdict meaning a fact was checked and failed. @type {string} */
const INCOMPLETE = 'incomplete';
/** Verdict meaning a fact could not be checked at all. @type {string} */
const UNDETERMINED = 'undetermined';

/**
 * Read one key's value out of raw .env text.
 *
 * @param {string} envText - The file's contents.
 * @param {string} key - The variable name.
 * @returns {string|null} The trimmed value, or null when absent or empty.
 *   An empty assignment is absence: a key set to nothing configures nothing.
 */
function readEnvValue(envText, key) {
  const match = envText.match(new RegExp('^' + key + '=(.*)$', 'm'));
  if (!match) return null;
  const value = match[1].trim().replace(/^"(.*)"$/, '$1').trim();
  return value === '' ? null : value;
}

/**
 * Evaluate setup completeness from the same facts the server evaluates.
 *
 * Three outcomes, and the third is not a flavour of the other two. A
 * config.json that exists but will not parse is UNDETERMINED, never
 * incomplete - the difference is whether the answer is "you have work to do"
 * or "I could not tell", and only one of those should send somebody back
 * through a wizard.
 *
 * @param {{envText: (string|null), configText: (string|null),
 *   pairedExists: (boolean|null)}} facts - Raw inputs, already read from
 *   disk by the caller so this function stays pure and testable. A null
 *   envText or configText means the file is absent; a null pairedExists
 *   means the sentinel could not be looked at, which is undetermined rather
 *   than absent.
 * @returns {{status: string, checks: Array<{key: string, passed: (boolean|null),
 *   detail: string}>}} The verdict and one check per fact.
 *
 * @example
 * evaluateLocalSetup({envText: 'TOTP_SECRET=a\nJWT_SECRET=b',
 *   configText: '{}', pairedExists: true}).status  // 'complete'
 */
function evaluateLocalSetup(facts) {
  const envText = (facts && facts.envText) || null;
  const configText =
    facts && facts.configText !== undefined ? facts.configText : null;
  const pairedExists = facts ? facts.pairedExists : null;
  const checks = [];

  if (configText === null || configText === undefined) {
    checks.push({
      key: 'config_file',
      passed: false,
      detail: 'No configuration file yet. Setup writes one when you finish.',
    });
  } else {
    try {
      JSON.parse(configText);
      checks.push({
        key: 'config_file',
        passed: true,
        detail: 'Configuration file present and readable.',
      });
    } catch (err) {
      checks.push({
        key: 'config_file',
        passed: null,
        detail:
          'The configuration file exists but is not valid JSON (' +
          err.message +
          '), so whether this instance is set up cannot be determined.',
      });
    }
  }

  for (const key of REQUIRED_ENV_KEYS) {
    const present = envText !== null && readEnvValue(envText, key) !== null;
    checks.push({
      key: key.toLowerCase(),
      passed: present,
      detail: present
        ? key + ' is configured.'
        : key + ' is not configured, so setup has not finished.',
    });
  }

  checks.push({
    key: 'totp_paired',
    passed: pairedExists === null ? null : Boolean(pairedExists),
    detail:
      pairedExists === null
        ? 'Whether an authenticator has been paired could not be determined.'
        : pairedExists
          ? 'An authenticator has been paired with this instance.'
          : 'No authenticator has been paired yet.',
  });

  let status;
  if (checks.some((c) => c.passed === null)) status = UNDETERMINED;
  else if (checks.every((c) => c.passed === true)) status = COMPLETE;
  else status = INCOMPLETE;

  return { status, checks };
}

/**
 * Decide whose answer the menu uses.
 *
 * The server's, whenever it has given one, in BOTH directions - a server
 * saying incomplete overrides a local evaluation saying complete just as
 * firmly as the reverse. It reads the same facts from the same machine and
 * it is the thing enforcing the bind lockdown, so a disagreement means the
 * local reading is wrong, not that the tray has found something.
 *
 * @param {{serverStatus: (string|null),
 *   local: {status: string}}} input - The server's reported setup_status
 *   (null when it has not been asked or could not answer) and the local
 *   evaluation.
 * @returns {{status: string, source: string, reason: string}} The verdict,
 *   which of the two produced it, and a sentence naming why.
 */
function resolveSetupVerdict(input) {
  const serverStatus = (input && input.serverStatus) || null;
  const local = (input && input.local) || { status: UNDETERMINED };

  if (serverStatus) {
    return {
      status: serverStatus,
      source: 'server',
      reason: 'The server reported this directly.',
    };
  }

  return {
    status: local.status,
    source: 'local',
    reason:
      'The server has not answered, so this was read from the files on ' +
      'this machine instead.',
  };
}

module.exports = {
  REQUIRED_ENV_KEYS,
  CHECK_KEYS,
  COMPLETE,
  INCOMPLETE,
  UNDETERMINED,
  readEnvValue,
  evaluateLocalSetup,
  resolveSetupVerdict,
};
