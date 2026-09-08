// Call the SHIPPED `ledStateFor` with a row a real server produced.
//
// WHY THIS EXISTS. tests/test_led_real_hooks.py drives a real claude in a
// real tmux pane, reads the real `GET /sessions/list` row back, and then
// needs to know what the USER would see. Re-implementing the mapping in
// Python would assert that two implementations agree, which is not the
// question; the question is what client/js/status-led.js does with this
// exact row. So the row is piped here verbatim and the shipped module is
// loaded in a bare sandbox - the same `vm` idiom
// tests/test_status_led.node.mjs uses, for the same reason: status-led.js
// is pure by contract and must keep loading with none of the app around
// it.
//
// Usage:  echo '{"activity_status":"idle","unread":true}' | node tests/led_state_for.node.mjs
// Prints: {"inner":"done","outer":"unread"}
// Exits non-zero, with the reason on stderr, if the row cannot be mapped.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * Load client/js/status-led.js in a bare sandbox and return its exports.
 *
 * Inputs: none.
 * Output: Object - the module's published api (ledStateFor, ledHtml, ...).
 */
function loadStatusLed() {
    const src = fs.readFileSync(
        path.join(here, '..', 'client', 'js', 'status-led.js'),
        'utf8'
    );
    const sandbox = { console: { log() {}, warn() {}, error() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox, { filename: 'status-led.js' });
    if (!sandbox.StatusLed || typeof sandbox.StatusLed.ledStateFor !== 'function') {
        throw new Error('status-led.js did not publish StatusLed.ledStateFor');
    }
    return sandbox.StatusLed;
}

/**
 * Read the whole of stdin as text.
 *
 * Inputs: none.
 * Output: Promise<string>.
 */
async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return Buffer.concat(chunks).toString('utf8');
}

const raw = await readStdin();
let row;
try {
    row = JSON.parse(raw);
} catch (err) {
    process.stderr.write(`could not parse the row as JSON: ${err.message}\n`);
    process.exit(2);
}
const led = loadStatusLed().ledStateFor(row);
process.stdout.write(JSON.stringify({ inner: led.inner, outer: led.outer }));
