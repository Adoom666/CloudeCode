// Node-based test for the launch picker's family grouping
// (client/js/provider-groups.js) and the wrappers screen's grouping
// (client/js/agent-wrappers-view.js groupByFamily).
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha, so the
// established pattern for testing client JS is a `vm`-sandboxed node script
// (see tests/test_session_row_actions.node.mjs, which this follows).
//
// The properties pinned here are the ones the launch picker regressed on
// before: one row per wrapper, NO duplicates, the default badged exactly
// once, and a model step offered only for accepts_model wrappers.
//
// Run with: node tests/test_provider_groups.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/**
 * Realm-safe deep comparison. Arrays and objects built INSIDE the vm
 * sandbox have that realm's prototypes, so assert.deepStrictEqual rejects
 * them on prototype identity even when the contents match. Comparing the
 * JSON projection is both sufficient here (plain data only) and immune to
 * the cross-realm trap.
 * Inputs: actual (any), expected (any), message (string).
 * Output: void - throws on mismatch.
 */
function deepEq(actual, expected, message) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
}

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording rather than throwing so a
 * failure does not hide the rest.
 * Inputs: name (string), fn (function) - throws on failure.
 * Output: void.
 */
function test(name, fn) {
    try {
        fn();
        passes += 1;
    } catch (err) {
        failures += 1;
        console.error(`FAIL: ${name}\n  ${err && err.message}`);
    }
}

// ---- sandbox ---------------------------------------------------------
const sandbox = { window: {}, globalThis: undefined, console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readClientJs('provider-groups.js'), sandbox);
const Groups = sandbox.window.ProviderGroups;

const FAMILIES = [
    { name: 'claude', label: 'claude' },
    { name: 'codex', label: 'codex' },
    { name: 'hermes', label: 'hermes' },
    { name: 'openclaw', label: 'openclaw' },
    { name: 'shell', label: 'shell' },
];

/** Build a wrapper object with test defaults. */
function w(id, family, extra) {
    return Object.assign({ id, family, label: id, default: false, accepts_model: false }, extra || {});
}

// ---- the live config: two claude wrappers ----------------------------
const LIVE = [
    w('claude-skip-permissions', 'claude', { label: 'claude', default: true }),
    w('cld', 'claude', { label: 'cld (keychain-backed)' }),
];

test('one row per wrapper, no duplicates', () => {
    const items = Groups.buildWrapperItems(LIVE, FAMILIES);
    assert.equal(items.length, 2);
    const ids = items.map((i) => i.wrapperId);
    deepEq(ids, ['claude-skip-permissions', 'cld']);
    assert.equal(new Set(ids).size, ids.length);
});

test('the default wrapper is badged, and only it', () => {
    const items = Groups.buildWrapperItems(LIVE, FAMILIES);
    const badged = items.filter((i) => i.label.includes('(default)'));
    assert.equal(badged.length, 1);
    assert.equal(badged[0].wrapperId, 'claude-skip-permissions');
    assert.equal(badged[0].label, 'claude (default)');
});

test('a single-family install gets no group headings', () => {
    const items = Groups.buildWrapperItems(LIVE, FAMILIES);
    assert.ok(items.every((i) => i.groupLabel === null));
});

test('there is never a synthetic bare claude row alongside wrappers', () => {
    const items = Groups.buildWrapperItems(LIVE, FAMILIES);
    assert.ok(items.every((i) => i.type === 'wrapper'));
});

// ---- multi-family ----------------------------------------------------
const MULTI = [
    w('cld', 'claude', { default: true }),
    w('cldor', 'claude', { accepts_model: true }),
    w('my-codex', 'codex', { default: true }),
    w('fancy-shell', 'shell'),
];

test('multi-family: still exactly one row per wrapper', () => {
    const items = Groups.buildWrapperItems(MULTI, FAMILIES);
    assert.equal(items.length, 4);
    assert.equal(new Set(items.map((i) => i.wrapperId)).size, 4);
});

test('multi-family: a heading rides on each group first row only', () => {
    const items = Groups.buildWrapperItems(MULTI, FAMILIES);
    const headed = items.filter((i) => i.groupLabel);
    deepEq(headed.map((i) => i.groupLabel), ['claude', 'codex', 'shell']);
    deepEq(headed.map((i) => i.wrapperId), ['cld', 'my-codex', 'fancy-shell']);
});

test('multi-family: rows come out in registry family order', () => {
    const items = Groups.buildWrapperItems(MULTI, FAMILIES);
    deepEq(items.map((i) => i.wrapperId), ['cld', 'cldor', 'my-codex', 'fancy-shell']);
});

test('only accepts_model wrappers advertise the model step', () => {
    const items = Groups.buildWrapperItems(MULTI, FAMILIES);
    const modelled = items.filter((i) => i.acceptsModel);
    deepEq(modelled.map((i) => i.wrapperId), ['cldor']);
});

test('a family with no wrappers contributes no rows and no heading', () => {
    const items = Groups.buildWrapperItems(MULTI, FAMILIES);
    assert.ok(items.every((i) => i.groupLabel !== 'hermes'));
    assert.ok(items.every((i) => i.groupLabel !== 'openclaw'));
});

// ---- resilience ------------------------------------------------------
test('a wrapper with no family field is treated as claude', () => {
    const items = Groups.buildWrapperItems([{ id: 'legacy', label: 'legacy' }], FAMILIES);
    assert.equal(items.length, 1);
    assert.equal(Groups.wrapperFamily({ id: 'legacy' }), 'claude');
});

test('a wrapper in an unknown family is still shown, never dropped', () => {
    const items = Groups.buildWrapperItems([w('cld', 'claude'), w('x', 'martian')], FAMILIES);
    assert.equal(items.length, 2);
    assert.ok(items.some((i) => i.wrapperId === 'x'));
});

test('empty inputs produce no rows rather than throwing', () => {
    deepEq(Groups.buildWrapperItems([], FAMILIES), []);
    deepEq(Groups.buildWrapperItems([], []), []);
});

test('missing families list still groups by the wrappers own families', () => {
    const items = Groups.buildWrapperItems(MULTI, []);
    assert.equal(items.length, 4);
    assert.equal(new Set(items.map((i) => i.wrapperId)).size, 4);
});

// ---- the settings screen's grouping ----------------------------------
const viewSandbox = {
    window: {},
    console,
    document: {
        // groupByFamily never touches the DOM; escapeHtml does, and is not
        // exercised here. A minimal stub keeps the module loadable.
        createElement: () => ({ set textContent(v) { this._v = v; }, get innerHTML() { return this._v; } }),
    },
};
viewSandbox.globalThis = viewSandbox;
vm.createContext(viewSandbox);
vm.runInContext(readClientJs('agent-wrappers-view.js'), viewSandbox);
const View = viewSandbox.window.AgentWrappersView;

const FAMILY_SUMMARIES = FAMILIES.map((f) => Object.assign({}, f, {
    command: '', description: '', wrapper_count: 0, in_use: true, command_field: `${f.name}_command`,
}));

test('settings: every family gets a group, even an empty one', () => {
    const groups = View.groupByFamily(LIVE, FAMILY_SUMMARIES);
    deepEq(groups.map((g) => g.family.name), ['claude', 'codex', 'hermes', 'openclaw', 'shell']);
    assert.equal(groups[0].wrappers.length, 2);
    assert.equal(groups[1].wrappers.length, 0);
});

test('settings: wrappers land in their declared family only', () => {
    const groups = View.groupByFamily(MULTI, FAMILY_SUMMARIES);
    const byName = Object.fromEntries(groups.map((g) => [g.family.name, g.wrappers.map((x) => x.id)]));
    deepEq(byName.claude, ['cld', 'cldor']);
    deepEq(byName.codex, ['my-codex']);
    deepEq(byName.shell, ['fancy-shell']);
    deepEq(byName.hermes, []);
});

test('settings: an unknown family gets a trailing group, never dropped', () => {
    const groups = View.groupByFamily([w('x', 'martian')], FAMILY_SUMMARIES);
    const last = groups[groups.length - 1];
    assert.equal(last.family.name, 'martian');
    deepEq(last.wrappers.map((g) => g.id), ['x']);
});

test('settings: a legacy wrapper with no family lands under claude', () => {
    const groups = View.groupByFamily([{ id: 'legacy', label: 'legacy' }], FAMILY_SUMMARIES);
    deepEq(groups[0].wrappers.map((g) => g.id), ['legacy']);
});

// ---- report ----------------------------------------------------------
// ---- pinned family rows (reserved families with no wrappers) ---------
//
// The registry as the SERVER actually ships it. The FAMILIES fixture
// above deliberately omits `pickable`, which is how an older server
// looks; these use the real shape.
const PICKABLE = [
    { name: 'claude', label: 'claude', pickable: true },
    { name: 'codex', label: 'codex', pickable: true },
    { name: 'hermes', label: 'hermes', pickable: true },
    { name: 'openclaw', label: 'openclaw', pickable: true },
    { name: 'shell', label: 'shell', pickable: false },
];

test('a pickable family with no wrappers gets exactly one pinned row', () => {
    const items = Groups.buildWrapperItems(LIVE, PICKABLE);
    const codex = items.filter((i) => i.agentType === 'codex');
    assert.equal(codex.length, 1);
    assert.equal(codex[0].type, 'family');
    assert.equal(codex[0].label, 'codex');
});

test('a pinned family row never advertises the model step', () => {
    const items = Groups.buildWrapperItems(LIVE, PICKABLE);
    items.filter((i) => i.type === 'family').forEach((i) => {
        assert.equal(i.acceptsModel, false);
    });
});

test('shell is never offered - it has its own new-console entry point', () => {
    const items = Groups.buildWrapperItems(LIVE, PICKABLE);
    assert.ok(items.every((i) => i.agentType !== 'shell'));
    assert.ok(items.every((i) => i.groupLabel !== 'shell'));
});

test('pinned rows follow registry order, so codex sits under claude', () => {
    const items = Groups.buildWrapperItems(LIVE, PICKABLE);
    const names = items.map((i) => i.agentType || 'claude-wrapper');
    assert.equal(names.indexOf('codex'), 2); // two claude wrappers first
    assert.ok(names.indexOf('codex') < names.indexOf('hermes'));
    assert.ok(names.indexOf('hermes') < names.indexOf('openclaw'));
});

test('a family WITH wrappers gets its wrappers, never a pinned row too', () => {
    const items = Groups.buildWrapperItems(MULTI, PICKABLE);
    const codexRows = items.filter((i) => Groups.wrapperFamily({ family: 'codex' }) && i.agentType === 'codex');
    // MULTI has a codex wrapper, so codex must contribute a wrapper row
    // and NOT a pinned one - offering both would launch the same family
    // two different ways from two adjacent rows.
    assert.equal(codexRows.length, 0);
});

test('every pinned row carries its own heading when groups are multiple', () => {
    const items = Groups.buildWrapperItems(LIVE, PICKABLE);
    const codex = items.filter((i) => i.agentType === 'codex')[0];
    assert.equal(codex.groupLabel, 'codex');
});

test('an older server that omits pickable changes nothing', () => {
    // THE COMPATIBILITY GUARANTEE, asserted rather than assumed. A client
    // talking to a server that predates the field must behave exactly as
    // it did before, not offer rows whose launch it cannot predict.
    const before = Groups.buildWrapperItems(LIVE, FAMILIES);
    assert.ok(before.every((i) => i.type === 'wrapper'));
    assert.equal(before.length, 2);
});

// ---- needs_model families (local / LM Studio) ------------------------

const WITH_LOCAL = [
    { name: 'claude', label: 'claude', pickable: true },
    { name: 'codex', label: 'codex', pickable: true },
    { name: 'local', label: 'local (lm studio)', pickable: true, needs_model: true },
    { name: 'shell', label: 'shell', pickable: false },
];

test('a needs_model family advertises the model step', () => {
    // It CANNOT launch bare - the server refuses rather than downgrading -
    // so the row must read "models >" and Enter must go to the model step,
    // not offer a launch that is going to fail.
    const items = Groups.buildWrapperItems(LIVE, WITH_LOCAL);
    const local = items.filter((i) => i.agentType === 'local')[0];
    assert.ok(local);
    assert.equal(local.needsModel, true);
    assert.equal(local.acceptsModel, true);
});

test('a modelless pinned family still launches on Enter', () => {
    const items = Groups.buildWrapperItems(LIVE, WITH_LOCAL);
    const codex = items.filter((i) => i.agentType === 'codex')[0];
    assert.equal(codex.needsModel, false);
    assert.equal(codex.acceptsModel, false);
});

test('familyNeedsModel reads false when the server omits the field', () => {
    // Same compatibility rule as pickable: an older server does not ship
    // it, and a client must not invent a model step it cannot fill.
    assert.equal(Groups.familyNeedsModel('local', FAMILIES), false);
    assert.equal(Groups.familyNeedsModel('local', WITH_LOCAL), true);
});

test('local is not offered at all when the server says it is not pickable', () => {
    const notPickable = WITH_LOCAL.map(
        (f) => (f.name === 'local' ? Object.assign({}, f, { pickable: false }) : f)
    );
    const items = Groups.buildWrapperItems(LIVE, notPickable);
    assert.ok(items.every((i) => i.agentType !== 'local'));
});

// ---- the real install: cldl is a WRAPPER inside a needs_model family --
// This is the live config on the mini, and it is the shape that broke:
// `local` HAS a wrapper, so the pinned-family branch never runs for it and
// the picker had nothing left that named the local catalog.
const WITH_CLDL = LIVE.concat([
    w('cldor', 'claude', { label: 'cldor (openrouter)', accepts_model: true }),
    w('cldl', 'local', { label: 'cldl (lm studio)', accepts_model: true }),
]);

test('a wrapper inherits its family needs_model', () => {
    const items = Groups.buildWrapperItems(WITH_CLDL, WITH_LOCAL);
    const cldl = items.filter((i) => i.wrapperId === 'cldl')[0];
    assert.ok(cldl, 'cldl must be offered');
    assert.equal(cldl.needsModel, true, 'cldl must route to the LOCAL catalog');
    assert.equal(cldl.acceptsModel, true);
});

test('an OpenRouter wrapper is NOT marked needs_model', () => {
    // The discriminating half. accepts_model is true for BOTH cldor and
    // cldl, so a test that only pinned cldl would pass just as happily
    // against code that marked every wrapper needsModel.
    const items = Groups.buildWrapperItems(WITH_CLDL, WITH_LOCAL);
    const cldor = items.filter((i) => i.wrapperId === 'cldor')[0];
    assert.ok(cldor);
    assert.equal(cldor.acceptsModel, true);
    assert.equal(cldor.needsModel, false, 'cldor must keep the OpenRouter catalog');
});

test('a family with a wrapper contributes no pinned row', () => {
    // Why the bug was reachable at all: `local` stops being a pinned family
    // the moment it has a wrapper, so familyNeedsModel had no row to land
    // on and the wrapper branch was the ONLY path left.
    const items = Groups.buildWrapperItems(WITH_CLDL, WITH_LOCAL);
    assert.ok(items.every((i) => i.agentType !== 'local'));
    assert.equal(items.filter((i) => i.wrapperId === 'cldl').length, 1);
});

console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
