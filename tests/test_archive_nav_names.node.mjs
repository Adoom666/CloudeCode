// THE RAIL, MADE NAVIGABLE BY NAME: folder names instead of slugs, one
// node per project with the machine as a badge, an unattributed node
// that hides only on a known zero, fuzzy filtering, and labels that
// truncate instead of wrapping.
//
// Every fixture number here is measured on the live corpus 2026-09-01:
//   - 80 project rows merge to 77 nodes; exactly 3 projects exist on
//     both machines, with byte-identical observed_cwd on each.
//   - 3 folder names collide across those 77 ('.claude' x4, 'outputs'
//     x4, 'scripts' x3), covering 11 projects.
//   - corpus 1 has 0 unattributed transcripts, corpus 2 has 5.
//   - the slug is NOT invertible: '...-Production-bhpp-new-server' is
//     really '.../bhpp_new_server', so no test here may derive a folder
//     name by splitting a slug.
//
// Run with: node tests/test_archive_nav_names.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 *
 * IT AWAITS, and every call site must `await` it. A harness that calls
 * fn() without awaiting records a pass the instant the promise is
 * created; the assertions then run after the verdict and throw into an
 * unhandled rejection, leaving the suite green. That is a verification
 * step that cannot fail, and it has already cost this project six
 * fake passes once.
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
 * Load the rail modules into one vm context.
 * @returns {object} {nav, row, fuzzy, merged, document}
 */
function load() {
    const env = createEnvironment();
    const fakeWindow = { document: env.document };
    const context = {
        window: fakeWindow,
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-nav-fuzzy.js',
                        'archive-nav-row.js', 'archive-nav-card.js',
                        'archive-nav-info.js', 'archive-nav-tree.js',
                        'archive-nav-order.js', 'archive-nav-drill.js',
                        'archive-nav-merged.js', 'archive-nav.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return {
        nav: context.window.ArchiveNav,
        row: context.window.ArchiveNavRow,
        fuzzy: context.window.ArchiveNavFuzzy,
        merged: context.window.ArchiveNavMerged,
        document: env.document,
    };
}

/** A merged project node as the server sends it. */
function node(displayName, fullPath, cwd, hosts, count, members) {
    return {
        project_id: 1, display_name: displayName, full_path: fullPath,
        observed_cwd: cwd, hosts: hosts, host_count: hosts.length,
        transcript_count: count,
        members: members || hosts.map((h, i) => ({
            project_id: i + 1, corpus_id: i + 1, host_id: i + 1,
            host_display_name: h, slug: fullPath, transcript_count: count,
        })),
    };
}

/** Collect the rendered label text of every project row in a slot. */
function labels(slot) {
    return slot.querySelectorAll('.archive-nav__node--project')
        .map((li) => li.querySelector('.archive-nav__label').textContent);
}

// --- names, not slugs ----------------------------------------------------

await test('a project row shows its folder name, not its slug', () => {
    const { row, document } = load();
    const li = row.renderRow(document, 'project',
        node('Infrastructure',
             '-Users-jsugamele-Development-Assistants-Infrastructure',
             '/Users/jsugamele/Development/Assistants/Infrastructure',
             ['Joe-MBP-M1'], 12), {});
    const text = li.querySelector('.archive-nav__label').textContent;
    assert.equal(text, 'Infrastructure');
    assert.ok(!text.includes('-Users-'), 'the slug must not reach the label');
});

await test('the full slug and path stay reachable in the title attribute', () => {
    const { row, document } = load();
    const li = row.renderRow(document, 'project',
        node('Infrastructure', '-Users-j-Development-Infrastructure',
             '/Users/j/Development/Infrastructure', ['Joe-MBP-M1'], 12), {});
    const tip = li.querySelector('.archive-nav__row').getAttribute('title');
    assert.ok(tip.includes('/Users/j/Development/Infrastructure'));
    assert.ok(tip.includes('-Users-j-Development-Infrastructure'));
    assert.ok(tip.includes('Joe-MBP-M1'));
});

await test('a project with a null display_name shows its slug, never a guess', () => {
    // The server refuses to invent a folder name from a slug it cannot
    // invert. The rail must show the raw slug rather than fabricating.
    const { row, document } = load();
    const li = row.renderRow(document, 'project', {
        project_id: 9, display_name: null,
        full_path: '-Users-j-Production-bhpp-new-server', hosts: [],
    }, {});
    const text = li.querySelector('.archive-nav__label').textContent;
    assert.equal(text, '-Users-j-Production-bhpp-new-server');
    assert.ok(!text.includes('bhpp_new_server'),
        'the rail must not derive a folder name from a lossy slug');
});

// --- the machine is a badge, not a level ---------------------------------

// SUPERSEDED 2026-09-02: THE MACHINE PILLS ARE NOT ON THE CARD FACE ANY
// MORE. This test used to assert two `.archive-nav__host-badge` elements
// and a `data-multi-host` attribute on a project row. The owner removed
// them - "the machine pills are probably not necessary to display, but
// should fold into an info button" - so the machines moved INTO the info
// modal, where each one is a link back to that machine's list.
//
// The assertion is inverted rather than deleted, because "the pills are
// gone" is now a fact worth keeping a test on: a card that quietly grew
// them back would push every project row a line taller again, which is
// the density problem the merge was supposed to end. The machines are
// still asserted, in tests/test_archive_nav_cards.node.mjs, where they
// now live.
await test('a project on two machines is ONE card, and it carries NO pills', () => {
    const { merged, document } = load();
    const slot = document.createElement('ul');
    merged.paint(document, slot, {
        nodes: [node('Media', '-Users-j-Media', '/Users/j/Media',
                     ['Joe-MBP-M1', 'Mac mini'], 2896)],
        unattributed: [], hostId: null, filterText: '', onActivate() {},
    });
    const rows = slot.querySelectorAll('.archive-nav__node--project');
    assert.equal(rows.length, 1, 'one project, one card');
    assert.equal(rows[0].querySelectorAll('.archive-nav__host-badge').length, 0,
        'the machine pills must not be on the card face');
    assert.equal(rows[0].querySelectorAll('.archive-nav__hosts').length, 0,
        'the pill container must not be on the card face either');
    // What replaces them: an affordance that opens the modal.
    assert.ok(rows[0].querySelector('.archive-nav__info-btn'),
        'the card must carry the info affordance the pills folded into');
});

await test('the machine filter narrows to projects with a member there', () => {
    const { merged } = load();
    const nodes = [
        node('Media', '-m', '/m', ['H1', 'H2'], 5, [
            { project_id: 1, host_id: 1, host_display_name: 'H1' },
            { project_id: 2, host_id: 2, host_display_name: 'H2' },
        ]),
        node('Solo', '-s', '/s', ['H1'], 3, [
            { project_id: 3, host_id: 1, host_display_name: 'H1' },
        ]),
    ];
    assert.equal(merged.filterByHost(nodes, 2).length, 1);
    assert.equal(merged.filterByHost(nodes, 2)[0].display_name, 'Media');
    assert.equal(merged.filterByHost(nodes, 1).length, 2);
});

await test('no machine filter means every machine, not none', () => {
    // Returning [] for "all" would read as "this machine has no
    // projects" - the same false green as a filter that matches nothing.
    const { merged } = load();
    const nodes = [node('A', '-a', '/a', ['H1'], 1)];
    assert.equal(merged.filterByHost(nodes, null).length, 1);
    assert.equal(merged.filterByHost(nodes, '').length, 1);
});

// --- the unattributed node, hidden ONLY on a known zero -------------------

await test('the unattributed node is HIDDEN when the count is a known zero', () => {
    const { row } = load();
    const verdict = row.shouldShowUnattributed({
        unattributed_transcript_count: 0, counted: true });
    assert.equal(verdict.show, false);
    assert.equal(verdict.reason, 'known zero');
});

await test('the unattributed node is SHOWN when the count could not be determined', () => {
    // The false green this whole codebase is written against: hiding on
    // a number nobody produced is indistinguishable from hiding real
    // transcripts, and these are the ones no other view can reach.
    const { row } = load();
    const verdict = row.shouldShowUnattributed({
        unattributed_transcript_count: null, counted: false });
    assert.equal(verdict.show, true);
    assert.ok(/could not be determined/.test(verdict.reason));
});

await test('the unattributed node is SHOWN when no count was reported at all', () => {
    const { row } = load();
    assert.equal(row.shouldShowUnattributed({}).show, true);
    assert.equal(row.shouldShowUnattributed({ unattributed_transcript_count: 'x' }).show,
                 true);
});

await test('the unattributed node is SHOWN when it holds anything', () => {
    const { row } = load();
    const verdict = row.shouldShowUnattributed({
        unattributed_transcript_count: 5, counted: true });
    assert.equal(verdict.show, true);
    assert.ok(verdict.reason.includes('5'));
});

await test('the real corpus split renders one node, not three and not none', () => {
    // Measured: corpus 1 = 0, corpus 2 = 5, corpus 3 = 0.
    const { merged } = load();
    const split = merged.partitionUnattributed([
        { corpus_id: 1, transcript_count: 0, counted: true },
        { corpus_id: 2, transcript_count: 5, counted: true },
        { corpus_id: 3, transcript_count: 0, counted: true },
    ]);
    assert.equal(split.shown.length, 1);
    assert.equal(split.shown[0].row.corpus_id, 2);
    assert.equal(split.hidden.length, 2);
});

await test('an uncounted corpus keeps its node even beside counted zeroes', () => {
    const { merged } = load();
    const split = merged.partitionUnattributed([
        { corpus_id: 1, transcript_count: 0, counted: true },
        { corpus_id: 2, transcript_count: null, counted: false },
    ]);
    // NOT deepStrictEqual on the array: `split.shown` is built inside the
    // vm realm, so its Array prototype is a different object from this
    // module's and deepStrictEqual fails on "same structure but not
    // reference-equal" while the value is correct. Assert the length and
    // the element instead.
    assert.equal(split.shown.length, 1);
    assert.equal(split.shown[0].row.corpus_id, 2);
    assert.equal(split.hidden.length, 1);
    assert.equal(split.hidden[0].reason, 'known zero');
});

// --- fuzzy filtering -----------------------------------------------------

await test('a non-contiguous subsequence matches where a substring filter cannot', () => {
    // THE CASE THAT PROVES IT IS FUZZY. 'dvtools' is not a substring of
    // 'dev_tools/scripts' anywhere, so the old indexOf filter returned
    // nothing for it.
    const { fuzzy } = load();
    assert.equal('dev_tools/scripts'.toLowerCase().indexOf('dvtools'), -1,
        'precondition: an exact-substring matcher MISSES this query');
    const hit = fuzzy.match('dev_tools/scripts', 'dvtools');
    assert.ok(hit, 'the fuzzy matcher must find it');
    assert.equal(hit.positions.length, 7);
});

await test('a query whose letters are out of order does NOT match', () => {
    // Fuzzy is a subsequence, not an anagram. Without this the filter
    // would match nearly everything and rank noise.
    const { fuzzy } = load();
    assert.equal(fuzzy.match('Infrastructure', 'rfni'), null);
});

await test('an empty query matches everything and ranks nothing', () => {
    const { fuzzy } = load();
    const hit = fuzzy.match('anything', '');
    assert.equal(hit.score, 0);
    assert.equal(hit.positions.length, 0);
    assert.equal(fuzzy.rank([{ display_name: 'A' }, { display_name: 'B' }], '').length, 2);
});

await test('a contiguous match outranks a scattered one', () => {
    const { fuzzy } = load();
    const ranked = fuzzy.rank([
        { display_name: 'M-e-d-i-a-scattered' },
        { display_name: 'Media' },
    ], 'media', [{ name: 'display_name', weight: 3 }]);
    assert.equal(ranked[0].row.display_name, 'Media');
});

await test('a hit in display_name outranks a STRONGER hit in full_path', () => {
    // Every full_path starts '-Users-jsugamele-', so unweighted paths
    // match almost any query and drown the names.
    //
    // The case is built so the field weight is the ONLY thing that
    // decides. Row A's full_path is the query exactly - a perfect,
    // fully-contiguous, whole-string match that outscores row B's
    // longer display_name on raw score. Only the x3 weighting on
    // display_name flips it. A gentler fixture (a strong display_name
    // against a weak path) passes with the weighting removed entirely,
    // which would make this a test that cannot fail for its own reason.
    const { fuzzy } = load();
    const FIELDS = [{ name: 'display_name', weight: 3 },
                    { name: 'full_path', weight: 1 }];
    const rows = [
        { display_name: 'zzz', full_path: 'media' },
        { display_name: 'media-x', full_path: 'qqq' },
    ];
    const raw = [fuzzy.match(rows[0].full_path, 'media').score,
                 fuzzy.match(rows[1].display_name, 'media').score];
    assert.ok(raw[0] > raw[1],
        'precondition: unweighted, the full_path hit is the stronger one');
    const ranked = fuzzy.rank(rows, 'media', FIELDS);
    assert.equal(ranked[0].row.display_name, 'media-x');
    assert.equal(ranked[0].field, 'display_name');
});

await test('a word-boundary match outranks a mid-word one', () => {
    const { fuzzy } = load();
    assert.equal(fuzzy.isBoundary('dev_tools', 0), true);
    assert.equal(fuzzy.isBoundary('dev_tools', 4), true, 'after an underscore');
    assert.equal(fuzzy.isBoundary('CloudeCode', 6), true, 'camel transition');
    assert.equal(fuzzy.isBoundary('hidden', 3), false);
});

await test('matched characters are highlighted as text nodes, never innerHTML', () => {
    const { fuzzy, document } = load();
    const frag = fuzzy.highlight(document, 'Media', [0, 1]);
    const marks = frag.childNodes.filter((n) => n.tagName === 'MARK');
    assert.equal(marks.length, 1);
    assert.equal(marks[0].textContent, 'Me');
});

await test('the rail finds a project by a non-prefix fragment', () => {
    // What the owner actually types: a fragment from the MIDDLE.
    const { merged, document } = load();
    const slot = document.createElement('ul');
    const result = merged.paint(document, slot, {
        nodes: [
            node('Infrastructure', '-a', '/a', ['H1'], 1),
            node('CloudeCode', '-b', '/b', ['H1'], 1),
            node('Media', '-c', '/c', ['H1'], 1),
        ],
        unattributed: [], hostId: null, filterText: 'struct', onActivate() {},
    });
    assert.equal(result.rendered, 1);
    assert.deepEqual(labels(slot), ['Infrastructure']);
});

await test('the fuzzy filter still says it only searched loaded rows', () => {
    // Making a filter cleverer without making it broader is exactly when
    // someone reads it as a search of the corpus.
    const { merged, document } = load();
    const slot = document.createElement('ul');
    merged.paint(document, slot, {
        nodes: [node('Media', '-c', '/c', ['H1'], 1)],
        unattributed: [], hostId: null, filterText: 'zzzz', onActivate() {},
    });
    const note = slot.querySelector('.archive-nav__filter-empty').textContent;
    assert.ok(/rows already fetched, not the whole corpus/.test(note));
});

// --- truncation, not wrapping --------------------------------------------

await test('a long label is one text node with a title, not a wrapped block', () => {
    // The rail truncates with CSS; the JS must not pre-shorten the text,
    // or the title attribute would carry the same damage the label has.
    const { row, document } = load();
    const long = '-Users-jsugamele-Development-Assistants-Infrastructure-deep-nested';
    const li = row.renderRow(document, 'project',
        { project_id: 1, display_name: null, full_path: long, hosts: [] }, {});
    const label = li.querySelector('.archive-nav__label');
    assert.equal(label.textContent, long, 'no ellipsis is inserted by the JS');
    assert.ok(li.querySelector('.archive-nav__row').getAttribute('title').includes(long));
});

// --- the default view ----------------------------------------------------

await test('the rail defaults to the merged view', () => {
    const { nav } = load();
    const rail = nav.create({ api: {}, onSelect() {} });
    assert.equal(rail.view(), 'merged');
});

await test('the by-machine tree is still reachable, not removed', () => {
    const { nav } = load();
    let askedHosts = false;
    const rail = nav.create({
        api: {
            listArchiveHosts() {
                askedHosts = true;
                return Promise.resolve({ envelope: { result_status: 'ok', result: [] } });
            },
        },
        onSelect() {},
    });
    return Promise.resolve(rail.setView('hosts')).then(() => {
        assert.equal(rail.view(), 'hosts');
        assert.ok(askedHosts, 'switching to the by-machine view must load hosts');
    });
});

await test('a missing merged endpoint renders a refusal, not an empty rail', () => {
    // An empty branch and a failed request must never look the same.
    const { nav } = load();
    const rail = nav.create({ api: {}, onSelect() {} });
    return Promise.resolve(rail.loadMergedProjects()).then((token) => {
        assert.equal(token, 'transport-error');
        assert.ok(rail.element.querySelectorAll('.archive-nav__outcome').length > 0,
            'the failure must be rendered where the person is looking');
    });
});

await test('the DEFAULT view loads itself, without a toggle click', async () => {
    // THE RAIL OPENED EMPTY. applyRoute() called loadHosts() on every
    // route - the drill-down the user is NOT looking at - and nothing
    // ever fetched the merged list that is the default view. So the
    // rail painted 0 rows, made no request for them, and rendered no
    // error: an empty list and a list nobody asked for are the same
    // pixels. It only populated if you clicked "By machine" and back.
    const { nav } = load();
    const called = [];
    const api = {
        listArchiveMergedProjects() {
            called.push('merged');
            return Promise.resolve({ httpStatus: 200, transportError: null,
                envelope: { result: [], result_status: 'ok',
                            scope_status: 'resolved', unevaluated: [],
                            meta: { hosts: [], unattributed: { by_corpus: [] } } } });
        },
        listArchiveHosts() {
            called.push('hosts');
            return Promise.resolve({ httpStatus: 200, transportError: null,
                envelope: { result: [], result_status: 'ok',
                            scope_status: 'resolved', unevaluated: [], meta: {} } });
        },
    };
    const rail = nav.create({ api: api, onSelect() {} });
    assert.equal(rail.view(), 'merged', 'the merged list is the default view');
    await rail.ensureViewLoaded();
    assert.deepEqual(called, ['merged'],
        'the view on screen must be the view that gets fetched');
});

await test('ensureViewLoaded does not refetch a view it already has', async () => {
    const { nav } = load();
    let n = 0;
    const api = {
        listArchiveMergedProjects() {
            n++;
            return Promise.resolve({ httpStatus: 200, transportError: null,
                envelope: { result: [node('Infra', '-U-Infra', '/U/Infra', ['Joe-MBP-M1'], 2)],
                            result_status: 'ok', scope_status: 'resolved',
                            unevaluated: [],
                            meta: { hosts: [], unattributed: { by_corpus: [] } } } });
        },
    };
    const rail = nav.create({ api: api, onSelect() {} });
    await rail.ensureViewLoaded();
    await rail.ensureViewLoaded();
    await rail.ensureViewLoaded();
    assert.equal(n, 1, 'one request per view per session, not one per route');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
