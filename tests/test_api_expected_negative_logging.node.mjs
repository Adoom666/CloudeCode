// An EXPECTED negative answer must not be logged as an error.
//
// GET /api/v1/sessions answers 404 {"detail":"No active session"} when no
// session is live. That is the route's documented negative answer, not a
// fault: `APIClient.getCurrentSession()` catches it and returns null, and
// the launchpad's only caller (loadRunningSessions, the back-compat
// fallback taken when GET /sessions/list already returned an empty array)
// renders exactly the same empty list either way. Nothing user-visible
// depends on it.
//
// It was still reaching the console as `API Error [/sessions]:` from the
// catch-all in `APIClient.call()`, 5926 times in one server log on the
// deployed mini. An error line that is not an error trains people to
// ignore the console, which is the thing that hides the next real one.
//
// So the assertion here is on the CONSOLE, which is the surface that was
// wrong - not on the return value, which was always correct. A test that
// only asserted `getCurrentSession() === null` passes against the broken
// version, because the broken version returned null too.
//
// THREE ASSERTIONS, and the middle one is the positive control:
//   1. an expected 404 produces no console.error
//   2. an UNEXPECTED status on the same call site still does  (control -
//      a silence-everything implementation would fail here)
//   3. the value contract is unchanged: null on 404, throw otherwise
//
// Run with: node tests/test_api_expected_negative_logging.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
 * @returns {Promise<void>}
 */
async function test(name, fn) {
    try {
        await fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Load client/js/api.js in a vm sandbox with a canned fetch, and capture
 * everything the module writes to console.error / console.warn / debug.
 *
 * @param {{status: number, body: object}} response - what fetch answers.
 * @returns {{api: object, errors: Array<string>, quiet: Array<string>}}
 *   `api` is a live APIClient; `errors` is every console.error first
 *   argument; `quiet` is every console.debug/info first argument.
 */
function loadApi({ status, body }) {
    const errors = [];
    const quiet = [];
    const store = {};
    const sandbox = {
        console: {
            log() {},
            warn() {},
            debug(...a) { quiet.push(String(a[0])); },
            info(...a) { quiet.push(String(a[0])); },
            error(...a) { errors.push(String(a[0])); },
        },
        localStorage: {
            getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
            setItem(k, v) { store[k] = String(v); },
            removeItem(k) { delete store[k]; },
        },
        async fetch() {
            return {
                ok: status >= 200 && status < 300,
                status,
                async json() { return body; },
            };
        },
        location: { protocol: 'http:', host: '127.0.0.1:5199' },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    const src = fs.readFileSync(path.join(ROOT, 'client/js/api.js'), 'utf8');
    vm.runInContext(src, sandbox, { filename: 'api.js' });
    const Ctor = sandbox.APIClient || (sandbox.API && sandbox.API.constructor);
    assert.ok(Ctor, 'api.js did not expose an APIClient constructor into the sandbox');
    return { api: new Ctor(), errors, quiet };
}

await test('an expected 404 from GET /sessions is NOT logged as console.error', async () => {
    const { api, errors } = loadApi({ status: 404, body: { detail: 'No active session' } });
    const result = await api.getCurrentSession();
    assert.equal(result, null, 'getCurrentSession must still resolve to null on 404');
    const sessionErrors = errors.filter(line => line.includes('/sessions'));
    assert.deepEqual(
        sessionErrors, [],
        `expected no console.error for the documented 404, got: ${JSON.stringify(sessionErrors)}`
    );
});

await test('POSITIVE CONTROL: an UNEXPECTED status on the same call site still logs', async () => {
    const { api, errors } = loadApi({ status: 500, body: { detail: 'boom' } });
    await assert.rejects(
        () => api.getCurrentSession(),
        /boom/,
        'a 500 must still propagate to the caller'
    );
    const sessionErrors = errors.filter(line => line.includes('/sessions'));
    assert.ok(
        sessionErrors.length >= 1,
        'a 500 must still reach console.error - silencing every status would be worse '
        + 'than the bug being fixed'
    );
});

await test('an unrelated endpoint 404 is still logged as an error', async () => {
    const { api, errors } = loadApi({ status: 404, body: { detail: 'Not Found' } });
    await assert.rejects(() => api.call('/definitely-not-a-route'));
    assert.ok(
        errors.some(line => line.includes('definitely-not-a-route')),
        'only the call sites that DECLARE a status expected may go quiet'
    );
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
