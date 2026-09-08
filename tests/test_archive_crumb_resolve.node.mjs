// NAMING A PROJECT NOBODY CLICKED - the deep-link breadcrumb.
//
// The defect this covers: the crumb learned a project's name ONLY from
// the rail row the user selected, so arriving by URL at /archive/p/<id>
// left it rendering `project NOT NAMED YET` permanently. Nothing errored
// and nothing looked broken - NOT NAMED YET is the correct, honest thing
// to say while a fact is still in flight, and it is indistinguishable
// from the case where the fact is never coming.
//
// Fixture numbers are measured on the live corpus 2026-09-01: 80 project
// rows merge to 77 nodes, so exactly 3 real project ids belong to a node
// that does NOT carry them at the top level - they survive only in
// `members`, and they are the 3 cross-machine projects.
//
// Run with: node tests/test_archive_crumb_resolve.node.mjs

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
 * Run one named assertion block. Awaited at every call site: a harness
 * that drops the promise records a pass before the assertions run.
 *
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body.
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
 * Load the crumb modules into one vm context.
 * @returns {object} {crumb, resolve}
 */
function load() {
    const context = {
        window: {},
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Array, String, Object,
    };
    vm.createContext(context);
    for (const file of ['archive-crumb.js', 'archive-crumb-resolve.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { crumb: context.window.ArchiveCrumb,
             resolve: context.window.ArchiveCrumbResolve };
}

/** The two-machine node: representative id 7, folded-up member id 42. */
const DUAL = {
    project_id: 7, display_name: 'Media', full_path: '-Users-j-Media',
    observed_cwd: '/Users/j/Media', hosts: ['Joe-MBP-M1', 'Mac mini'],
    host_count: 2, transcript_count: 9,
    members: [{ project_id: 7, host_id: 1, host_display_name: 'Joe-MBP-M1' },
              { project_id: 42, host_id: 2, host_display_name: 'Mac mini' }],
};

/** A single-machine node. */
const SOLO = {
    project_id: 3, display_name: 'Infrastructure', full_path: '-Users-j-Infra',
    observed_cwd: '/Users/j/Infra', hosts: ['Joe-MBP-M1'], host_count: 1,
    transcript_count: 4,
    members: [{ project_id: 3, host_id: 1, host_display_name: 'Joe-MBP-M1' }],
};

/** An api whose merged call resolves to a given callEnvelope result. */
function apiReturning(result) {
    return { listArchiveMergedProjects: () => Promise.resolve(result) };
}

/** A healthy envelope carrying the two fixture nodes. */
function okEnvelope(nodes) {
    return { httpStatus: 200, transportError: null,
             envelope: { result: nodes, result_status: 'ok', meta: {} } };
}

await test('every member id resolves, not just the representative', () => {
    const { crumb } = load();
    const index = crumb.indexNodes([DUAL, SOLO]);
    // 3 ids across 2 nodes - the fold must not strand id 42.
    assert.equal(Object.keys(index).length, 3);
    assert.equal(index['7'], DUAL);
    assert.equal(index['42'], DUAL, 'the folded-up member id must resolve');
    assert.equal(index['3'], SOLO);
});

await test('a deep link to a FOLDED member id renders a name, not NOT NAMED YET', async () => {
    // This is the exact live case: 3 of 80 ids sit only in `members`,
    // and they are the 3 cross-machine projects - the worst possible
    // sample to be silently wrong about.
    const { crumb, resolve } = load();
    const r = await resolve.createResolver(apiReturning(okEnvelope([DUAL, SOLO]))).resolve(42);

    assert.equal(r.status, 'name');
    const seg = crumb.projectSegment(r.node);
    assert.equal(seg.text, 'Media');
    assert.equal(seg.kind, 'name');
    assert.notEqual(seg.text, crumb.PROJECT_UNKNOWN);
    assert.ok(!crumb.hasNumericId([seg.text]), 'no database id may reach a crumb');
});

await test('an unreadable list is cannot_determine, never a name', async () => {
    const { resolve } = load();
    for (const bad of [
        { httpStatus: 0, transportError: 'network died', envelope: null },
        { httpStatus: 500, transportError: null, envelope: null },
        { httpStatus: 200, transportError: null,
          envelope: { result: [], result_status: 'datastore_unreadable', meta: {} } },
        { httpStatus: 200, transportError: null,
          envelope: { result: null, result_status: 'ok', meta: {} } },
    ]) {
        const r = await resolve.createResolver(apiReturning(bad)).resolve(42);
        assert.equal(r.status, 'cannot_determine');
        assert.equal(r.node, null, 'a failed read must never invent a name');
    }
});

await test('a thrown request is cannot_determine rather than an unhandled rejection', async () => {
    const { resolve } = load();
    const api = { listArchiveMergedProjects() { throw new Error('boom'); } };
    const r = await resolve.createResolver(api).resolve(42);
    assert.equal(r.status, 'cannot_determine');
    assert.equal(r.node, null);
});

await test('an id genuinely absent is unresolved, and distinct from unreadable', async () => {
    // The three-outcome rule: "the list does not contain this" and "I
    // could not read the list" are different findings, and only one of
    // them means the id is wrong.
    const { resolve } = load();
    const r = await resolve.createResolver(apiReturning(okEnvelope([DUAL, SOLO]))).resolve(999);
    assert.equal(r.status, 'unresolved');
    assert.equal(r.node, null);
});

await test('an empty-but-ok list is unresolved, not cannot_determine', async () => {
    const { resolve } = load();
    const r = await resolve.createResolver(apiReturning(okEnvelope([]))).resolve(42);
    assert.equal(r.status, 'unresolved');
});

await test('the merged list is fetched ONCE across many lookups', async () => {
    let calls = 0;
    const { resolve } = load();
    const api = {
        listArchiveMergedProjects() { calls++; return Promise.resolve(okEnvelope([DUAL, SOLO])); }
    };
    const r = resolve.createResolver(api);
    await Promise.all([r.resolve(7), r.resolve(42), r.resolve(3), r.resolve(999)]);
    assert.equal(calls, 1, 'a rail-sized request must not run once per crumb');
});

await test('a FAILED read is not cached, so the next navigation retries', async () => {
    // One bad minute must not poison the tab for its lifetime.
    let calls = 0;
    const { resolve } = load();
    const api = {
        listArchiveMergedProjects() {
            calls++;
            return Promise.resolve(calls === 1
                ? { httpStatus: 0, transportError: 'down', envelope: null }
                : okEnvelope([DUAL, SOLO]));
        }
    };
    const r = resolve.createResolver(api);
    assert.equal((await r.resolve(42)).status, 'cannot_determine');
    assert.equal((await r.resolve(42)).status, 'name');
    assert.equal(calls, 2);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
