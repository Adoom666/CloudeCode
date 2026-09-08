// Node test: the launchpad sends a label when it creates a session.
//
// WHAT THIS GUARDS. The server turns a non-empty `label` on the create
// request into `--name <label>` on the launch command, so the row title
// and the name claude calls itself are one string. That only happens if
// the client actually SENDS it, and the plain create endpoint was the one
// creator that never did - measured 2026-09-08, a project named
// "Punchlist Test" launched claude with no `--name` at all.
//
// WHY THIS ASSERTS AGAINST THE SOURCE TEXT AND NOT A LIVE CALL. There is
// no build step for client/, and launchpad.js is a 6000-line class whose
// create paths sit behind a provider modal, a folder chooser and a live
// terminal. Instantiating it here would test the harness. What matters is
// narrow and textual: each `createSession` payload literal in the file
// carries a `label:` key. A regex over the payload block answers exactly
// that question and nothing it cannot answer.
//
// THE NEGATIVE CONTROL IS THE POINT. A test that only checked "the file
// contains the word label" would pass against a comment mentioning it, so
// the check below finds each payload object by its own anchor and asserts
// the key inside THAT object.
//
// Run with: node tests/test_launchpad_create_label.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
    path.join(here, '..', 'client', 'js', 'launchpad.js'),
    'utf8'
);

/**
 * The text of the object literal a `const payload = {` starts.
 *
 * Description: walks braces from the opening `{` so a nested object in
 *   the payload cannot end the match early, which is what a lazy regex
 *   would do and why this is not one.
 * Inputs: text (string) - the whole file. fromIndex (number) - index of
 *   the `{` that opens the literal.
 * Output: string - the literal including both braces.
 * Example: payloadBody(source, source.indexOf('const payload = {') + 16)
 */
function payloadBody(text, fromIndex) {
    let depth = 0;
    for (let i = fromIndex; i < text.length; i += 1) {
        if (text[i] === '{') depth += 1;
        if (text[i] === '}') {
            depth -= 1;
            if (depth === 0) return text.slice(fromIndex, i + 1);
        }
    }
    throw new Error('unbalanced payload literal in launchpad.js');
}

/**
 * Every `const payload = { ... }` literal in the file, in order.
 *
 * Inputs: text (string).
 * Output: string[] - each literal's text.
 * Example: allPayloads(source).length
 */
function allPayloads(text) {
    const out = [];
    const marker = 'const payload = {';
    let at = text.indexOf(marker);
    while (at !== -1) {
        out.push(payloadBody(text, at + marker.length - 1));
        at = text.indexOf(marker, at + 1);
    }
    return out;
}

const payloads = allPayloads(source);

// ---------------------------------------------------------------------
// The positive: the two claude-launching create paths send a label.
// ---------------------------------------------------------------------

const newProject = payloads.find((p) => p.includes('project_parent_dir'));
assert.ok(
    newProject,
    'the "start empty" payload (the one carrying project_parent_dir) was not found - ' +
        'if that flow was restructured, this test needs re-pointing, not deleting'
);
assert.match(
    newProject,
    /\blabel:\s*projectDetails\.name\b/,
    'the new-project payload must send the project name as the session label, ' +
        'or claude launches with no --name and the TUI shows the directory'
);

const openProject = payloads.find(
    (p) => p.includes('working_dir: project.path')
);
assert.ok(
    openProject,
    'the "open existing project" payload was not found'
);
assert.match(
    openProject,
    /\blabel:\s*project\.name\b/,
    'the open-project payload must send the project name as the session label'
);

// ---------------------------------------------------------------------
// The negative control. A shell console is NOT a claude launch, so it
// deliberately sends no label - `--name` is a claude-family flag and the
// server drops it for other families anyway. Asserting its ABSENCE here
// is what proves the two assertions above are reading real per-payload
// keys rather than matching the file at large.
// ---------------------------------------------------------------------

const consolePayload = payloads.find((p) => p.includes("agent_type: 'shell'"));
assert.ok(consolePayload, 'the console payload was not found');
assert.ok(
    !/\blabel:/.test(consolePayload),
    'the shell console payload should not carry a label - if this is now ' +
        'deliberate, update this test and say why in the same change'
);

// ---------------------------------------------------------------------
// And the label must not have been folded into project_name, which is a
// different thing: a project is a folder shared by many sessions.
// ---------------------------------------------------------------------

assert.match(
    newProject,
    /\bproject_name:\s*projectDetails\.name\b/,
    'project_name must still be sent alongside label, not replaced by it'
);

console.log('test_launchpad_create_label.node.mjs: all assertions passed');
