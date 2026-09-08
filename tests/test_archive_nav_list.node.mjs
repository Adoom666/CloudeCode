// THE NAVIGATION SURFACE: an honest filter, an honest split, and a
// has_more that has three outcomes.
//
// Measured live 2026-08-31 and every fixture below is a real captured
// response from 127.0.0.1:5055:
//   - 19,588 of 21,039 transcripts (93.1%) are `agent`-scheme sidechain
//     files, not conversations. The `session_ref_scheme` filter shipped
//     2026-08-31 and is SERVER-side, so the list re-queries the scope
//     rather than splitting the pages it happens to hold.
//   - `session_ref` is not unique: `journal` names 14 transcripts.
//     Nothing may key on it.
//   - `has_more` comes back `null` on every failure path. null is not
//     false.
//   - corpus 2 reports 5 unattributed transcripts, invisible from the
//     project tree by construction.
//
// Run with: node tests/test_archive_nav_list.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures', 'archive');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 *
 * IT AWAITS. Half the bodies here are async, and a harness that calls
 * fn() without awaiting records a pass the instant the promise is
 * created - every assertion inside then runs after the verdict, throws
 * into an unhandled rejection, and the suite stays green. That is a
 * verification step that CANNOT FAIL, which is the worst place for one:
 * there is no outer check left to catch it. It was caught here only by
 * a mutation that should have gone red and did not.
 *
 * Every call site must therefore be `await test(...)`, which top-level
 * await in an ES module makes both possible and sequential.
 *
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing or rejecting marks it failed.
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
 * Load one captured live response.
 * @param {string} name - Basename without .json.
 * @returns {object} The parsed envelope.
 */
function fixture(name) {
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

/**
 * Load the nav and list modules into one vm context.
 * @returns {{nav: object, list: object, document: object}} Modules and document.
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
    // archive-fuzzy.js and archive-tlist-filter.js are the list's own
    // header and its per-column matcher; the list builds one of each at
    // create() time, so a context without them cannot construct a list
    // at all.
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-nav-row.js',
                        'archive-nav-fuzzy.js', 'archive-nav-card.js', 'archive-nav-info.js', 'archive-nav-tree.js',
                        'archive-nav-order.js', 'archive-nav-drill.js',
                        'archive-nav-merged.js', 'archive-nav.js',
                        'archive-fuzzy.js', 'archive-tlist-row.js',
                        'archive-tlist-filter.js', 'archive-transcript-list.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return {
        nav: context.window.ArchiveNav,
        list: context.window.ArchiveTranscriptList,
        document: env.document,
    };
}

const { nav, list, document } = load();

/**
 * Build a fake api object that answers with prepared results.
 * @param {object} map - Method name to a callEnvelope-shaped result.
 * @returns {object} An api stand-in.
 */
function fakeApi(map) {
    const api = {};
    for (const [k, v] of Object.entries(map)) {
        api[k] = () => Promise.resolve(typeof v === 'function' ? v() : v);
    }
    return api;
}

/** A callEnvelope-shaped success wrapper. */
const okResult = (envelope) => ({ envelope, httpStatus: 200, headers: null, transportError: null });

// =====================================================================
// NAV
// =====================================================================

await test('the filter is a pure substring match over loaded rows', () => {
    const rows = [{ slug: 'alpha' }, { slug: 'BETA' }, { slug: 'gamma' }];
    assert.equal(nav.filterRows(rows, 'a', ['slug']).length, 3);
    assert.equal(nav.filterRows(rows, 'bet', ['slug']).length, 1);
    assert.equal(nav.filterRows(rows, '', ['slug']).length, 3);
    assert.equal(nav.filterRows(rows, 'zzz', ['slug']).length, 0);
});

await test('the filter note ALWAYS says it filtered fetched rows, not the corpus', () => {
    const note = nav.describeFilter(3, 71, 3416, 'projects');
    assert.ok(note.includes('3 of 71 loaded projects'), note);
    assert.ok(note.includes('3,416 exist'), note);
    assert.ok(note.includes('not the whole corpus'),
        'a filter that reads like a search of the archive is a false green');
});

await test('a count the server did not supply renders NOT KNOWN, never 0', () => {
    assert.equal(nav.renderCount(null), 'NOT KNOWN');
    assert.equal(nav.renderCount(undefined), 'NOT KNOWN');
    assert.equal(nav.renderCount(0), '0');
});

await test('non-ASCII display names survive: host 2 is "Joseph’s Mac mini (2)"', () => {
    const hosts = fixture('ok_hosts').result;
    const two = hosts.find((h) => h.host_id === 2);
    assert.ok(two, 'the captured hosts response must contain host 2');
    assert.equal(nav.labelFor(nav.NODE_KINDS.HOST, two), two.display_name);
    assert.ok(two.display_name.indexOf('’') !== -1,
        'this fixture is here BECAUSE it carries a U+2019');
});

await test('a failed expand renders the reason INLINE and never an empty branch', async () => {
    const n = nav.create({
        document,
        api: fakeApi({
            listArchiveHosts: okResult(fixture('ok_hosts')),
            listArchiveCorpora: { envelope: null, httpStatus: null, headers: null,
                                  transportError: 'no response in 10s' },
        }),
    });
    await n.loadHosts();
    const token = await n.expand(nav.NODE_KINDS.HOST, 1);
    assert.equal(token, 'transport-error');
    const block = n.element.querySelector('[data-outcome="transport-error"]');
    assert.ok(block, 'the failed branch must carry an outcome block at that node');
    assert.ok(n.element.textContent.includes('no response in 10s'),
        'the transport reason must be rendered, not swallowed');
    // Every sibling stays usable: the other host is still on screen.
    const rows = n.element.querySelectorAll('[data-node-kind="host"]');
    assert.equal(rows.length, 2, 'a failed branch must not remove its siblings');
});

await test('expanding a corpus always renders the unattributed node, count and all', async () => {
    const n = nav.create({
        document,
        api: fakeApi({
            listArchiveHosts: okResult(fixture('ok_hosts')),
            listArchiveCorpora: okResult(fixture('nav_corpora')),
            listArchiveProjects: okResult(fixture('nav_projects')),
        }),
    });
    await n.loadHosts();
    await n.expand(nav.NODE_KINDS.HOST, 1);
    await n.expand(nav.NODE_KINDS.CORPUS, 2);
    const node = n.element.querySelector('[data-node-kind="unattributed"]');
    assert.ok(node, 'transcripts with no project must be given a shape');
    const t = node.textContent;
    assert.ok(t.includes('5'), `the captured count is 5. Rendered: ${t}`);
    assert.ok(t.includes('invisible from the project tree'),
        'the node must say WHY it exists');
});

// =====================================================================
// TRANSCRIPT LIST
// =====================================================================

const TRANSCRIPTS = fixture('list_transcripts');

await test('the captured page really is dominated by agent-scheme rows', () => {
    const schemes = TRANSCRIPTS.result.map((r) => r.session_ref_scheme);
    assert.ok(schemes.includes('agent'),
        'this test is about the 93.1% agent-scheme problem being real');
});

// The scheme filter moved SERVER-SIDE on 2026-08-31. These three tests
// replace the ones that asserted the client-side split, and they assert
// the opposite property: that the note now claims scope coverage,
// because a note still saying "this filters what has been FETCHED"
// would understate a complete answer and be a lie in the other
// direction.

await test('"all" is an OMITTED parameter, never the literal string all', () => {
    // session_ref_scheme=all would be an UNKNOWN scheme and answer 400.
    assert.equal(list.wireScheme(list.SCHEME_FILTERS.ALL), null);
    assert.equal(list.wireScheme(''), null);
    assert.equal(list.wireScheme(undefined), null);
    assert.equal(list.wireScheme(list.SCHEME_FILTERS.CONVERSATIONS), 'uuid');
    assert.equal(list.wireScheme(list.SCHEME_FILTERS.SIDECHAINS), 'agent');
});

await test('an applied filter says the SERVER filtered the whole scope', () => {
    const note = list.describeFilter(50, {
        applied: true,
        session_ref_scheme: 'uuid',
        matched_in_scope: 77,
        scope_total_before_filter: 3416,
        counts_are: 'scanned_within_this_scope_only',
        session_ref_scheme_means: 'filters on the session_ref_scheme column only.'
    });
    assert.ok(note.includes('50'), note);
    assert.ok(note.includes('WHOLE scope'), note);
    assert.ok(note.includes('77'), note);
    assert.ok(note.includes('3,416'), note);
    // The old lie must be gone, in BOTH of its spellings.
    assert.ok(!note.includes('FETCHED'), note);
    assert.ok(!note.includes('no server-side scheme filter'), note);
    // And it must carry the server's caveat rather than overclaim.
    assert.ok(note.includes('session_ref_scheme column only'), note);
});

await test('a missing scoped count renders NOT KNOWN, never 0', () => {
    const note = list.describeFilter(50, {
        applied: true, session_ref_scheme: 'uuid', matched_in_scope: null
    });
    assert.ok(note.includes('NOT KNOWN'), note);
    assert.ok(!note.includes(' 0 rows with this scheme'), note);
});

await test('an UNAPPLIED filter says nothing, because nothing is hidden', () => {
    assert.equal(list.describeFilter(3, { applied: false }), '');
    assert.equal(list.describeFilter(3, null), '');
});

await test('choosing a scheme RE-QUERIES the scope, it does not repaint', async () => {
    // The whole point of the change. A repaint would silently restore
    // the client-side behaviour while the note claimed scope coverage.
    const calls = [];
    const view = list.create({
        document,
        api: {
            listArchiveTranscripts: (id, opts) => {
                calls.push(opts.sessionRefScheme);
                return Promise.resolve(okResult({
                    result: [], result_status: 'ok', scope_status: 'resolved',
                    unevaluated: [],
                    meta: {
                        paging: { limit: 50, returned: 0, has_more: false, next_cursor: null },
                        filters: { applied: true, session_ref_scheme: 'uuid',
                                   matched_in_scope: 0, scope_total_before_filter: 3416 }
                    }
                }));
            }
        }
    });
    await view.load({ kind: 'project', id: 12, inScope: 3416 });
    // The FIRST load now carries the default filter, because the default
    // is no longer 'all'. `wireScheme` still maps 'all' to null - "no
    // filter" has to be an OMITTED parameter, since the server answers
    // 400 for an unknown scheme value of 'all'.
    assert.deepEqual(calls, ['uuid'],
        'the first load must carry the default scheme filter');
    await view.setSchemeFilter(list.SCHEME_FILTERS.ALL);
    assert.deepEqual(calls, ['uuid', null],
        'changing the filter must issue a NEW request carrying it');
});

await test('a filter change discards the cursor rather than replaying it', async () => {
    // A cursor minted under one filter positions inside THAT result set.
    const cursors = [];
    const page = (cursor) => okResult({
        result: [{ transcript_id: 1, session_ref: 'a', session_ref_scheme: 'uuid',
                   line_count: 1, raw_byte_length: 1, ingested_at: 'x',
                   host_attribution: 'manifest_verified' }],
        result_status: 'ok', scope_status: 'resolved', unevaluated: [],
        meta: { paging: { limit: 50, returned: 1, has_more: true, next_cursor: 'CUR' },
                filters: { applied: false, session_ref_scheme: null } }
    });
    const view = list.create({
        document,
        api: {
            listArchiveTranscripts: (id, opts) => {
                cursors.push(opts.cursor);
                return Promise.resolve(page(opts.cursor));
            }
        }
    });
    await view.load({ kind: 'project', id: 12, inScope: 3416 });
    await view.loadMore();
    assert.deepEqual(cursors, [null, 'CUR']);
    await view.setSchemeFilter(list.SCHEME_FILTERS.SIDECHAINS);
    assert.equal(cursors[2], null,
        'the reload after a filter change must start at page one, not replay CUR');
});

await test('rows key and route on transcript_id, never on session_ref', () => {
    const row = list.renderRow(document, {
        transcript_id: 5767, session_ref: 'journal', session_ref_scheme: 'uuid',
        line_count: 10, raw_byte_length: 100, ingested_at: '2026-08-30T16:01:45.022332Z',
        host_attribution: 'manifest_verified',
    }, {});
    assert.equal(row.getAttribute('data-transcript-id'), '5767');
    const btn = row.querySelector('[data-action="open-transcript"]');
    assert.equal(btn.getAttribute('data-transcript-id'), '5767');
    // `journal` names 14 different transcripts. It may be shown, never keyed on.
    assert.equal(row.getAttribute('data-session-ref'), null);
    assert.ok(row.textContent.includes('journal'), 'it is still displayed as a label');
});

await test('NO PROJECT and HOST NOT ESTABLISHED are separate, never merged', () => {
    const base = {
        transcript_id: 1, session_ref: 'x', session_ref_scheme: 'uuid',
        line_count: 1, raw_byte_length: 1, ingested_at: null,
    };
    const noProject = list.renderRow(document,
        Object.assign({}, base, { host_attribution: 'manifest_verified' }),
        { unattributed: true });
    const noHost = list.renderRow(document,
        Object.assign({}, base, { host_attribution: 'cannot_determine' }), {});
    const both = list.renderRow(document,
        Object.assign({}, base, { host_attribution: 'cannot_determine' }),
        { unattributed: true });

    assert.ok(noProject.querySelector('.archive-tlist__badge--no-project'));
    assert.equal(noProject.querySelector('.archive-tlist__badge--host-unknown'), null);

    assert.equal(noHost.querySelector('.archive-tlist__badge--no-project'), null);
    assert.ok(noHost.querySelector('.archive-tlist__badge--host-unknown'));

    // A transcript can be both, and then it carries BOTH badges.
    assert.ok(both.querySelector('.archive-tlist__badge--no-project'));
    assert.ok(both.querySelector('.archive-tlist__badge--host-unknown'));
});

await test('isUnestablished does not over-match a real attribution value', () => {
    assert.equal(list.isUnestablished('cannot_determine'), true);
    assert.equal(list.isUnestablished('manifest_verified'), false);
    assert.equal(list.isUnestablished('derived'), false);
});

await test('has_more true renders a load-more control', async () => {
    const l = list.create({
        document,
        api: fakeApi({ listArchiveTranscripts: okResult(TRANSCRIPTS) }),
    });
    await l.load({ kind: 'project', id: 12, inScope: 3416 });
    assert.equal(l.hasMore(), true);
    assert.ok(l.element.querySelector('[data-action="load-more"]'));
    assert.equal(typeof l.cursor(), 'string');
});

await test('has_more NULL renders a stated unknown and NO load-more control', async () => {
    const nulled = JSON.parse(JSON.stringify(TRANSCRIPTS));
    nulled.meta.paging.has_more = null;
    nulled.meta.paging.next_cursor = null;
    const l = list.create({
        document,
        api: fakeApi({ listArchiveTranscripts: okResult(nulled) }),
    });
    await l.load({ kind: 'project', id: 12 });
    assert.equal(l.hasMore(), null);
    assert.equal(l.element.querySelector('[data-action="load-more"]'), null,
        'null is not false: a load-more control here would claim a cursor exists');
    assert.ok(l.element.textContent.includes('NOT KNOWN'),
        'the unknown must be stated in words');
});

await test('has_more FALSE says the end was reached, which is a different claim', async () => {
    const ended = JSON.parse(JSON.stringify(TRANSCRIPTS));
    ended.meta.paging.has_more = false;
    ended.meta.paging.next_cursor = null;
    const l = list.create({
        document,
        api: fakeApi({ listArchiveTranscripts: okResult(ended) }),
    });
    await l.load({ kind: 'project', id: 12 });
    assert.equal(l.hasMore(), false);
    assert.equal(l.element.querySelector('[data-action="load-more"]'), null);
    assert.ok(l.element.textContent.includes('End of the list'));
    assert.ok(!l.element.textContent.includes('NOT KNOWN'),
        'a real end must not be reported as an unknown');
});

await test('a malformed cursor does NOT silently restart paging', async () => {
    let calls = 0;
    const l = list.create({
        document,
        api: {
            listArchiveTranscripts: () => {
                calls++;
                return Promise.resolve(calls === 1
                    ? okResult(TRANSCRIPTS)
                    : { envelope: fixture('cannot_cursor'), httpStatus: 400,
                        headers: null, transportError: null });
            },
        },
    });
    await l.load({ kind: 'project', id: 12 });
    const firstCount = l.rows().length;
    const token = await l.loadMore();
    assert.equal(token, 'cannot-determine');
    assert.equal(l.rows().length, firstCount,
        'a failed page must not duplicate or drop the rows already held');
    assert.ok(l.element.querySelector('[data-outcome="cannot-determine"]'));
    assert.equal(l.cursor(), null, 'the dead cursor must not be reused');
});

await test('loadMore refuses rather than re-requesting page one when no cursor exists', async () => {
    let calls = 0;
    const l = list.create({
        document,
        api: {
            listArchiveTranscripts: () => {
                calls++;
                const noCursor = JSON.parse(JSON.stringify(TRANSCRIPTS));
                noCursor.meta.paging.next_cursor = null;
                noCursor.meta.paging.has_more = false;
                return Promise.resolve(okResult(noCursor));
            },
        },
    });
    await l.load({ kind: 'project', id: 12 });
    assert.equal(calls, 1);
    assert.equal(await l.loadMore(), 'no-cursor');
    assert.equal(calls, 1, 'a cursorless loadMore must issue no request at all');
});

// =====================================================================
// THE SCHEME TOGGLE GROUP, AND THE THREE THINGS THE COMPOSITION ROOT
// NEEDS FROM THE FILTER.
//
// Both were measured broken 2026-09-01. The three scheme buttons carried
// no `aria-pressed` at all, so which filter was on was announced
// nowhere; and the nav exposed no way to focus, read or clear the
// filter, which is why the composition root passed a hardcoded '' as the
// filter text and made rung 2 of the Escape ladder unreachable.
// =====================================================================

/**
 * The three scheme buttons and their pressed state, read off the DOM.
 * @param {object} l - a created transcript list.
 * @returns {Array<{value: string, pressed: string|null}>} in DOM order.
 */
function schemeButtons(l) {
    return [...l.element.querySelectorAll('[data-scheme-filter]')].map((b) => ({
        value: b.getAttribute('data-scheme-filter'),
        // SUPERSEDED CONTRACT, RENAMED RATHER THAN DROPPED. These were
        // <button aria-pressed>; the owner replaced the whole fake
        // dropdown with a real <select> ("i dont like the dropdown its
        // fake and doesnt match"), so the state now lives where the
        // platform puts it. The FIELD keeps its name so every assertion
        // below still reads as one contract; what it reads has moved
        // from aria-pressed to the option's own selected attribute.
        pressed: b.getAttribute('selected') === 'selected' ? 'true' : 'false',
        tag: b.tagName.toLowerCase(),
    }));
}

await test('C1: all three scheme options are real <option>s, exactly one selected, and it follows setSchemeFilter', async () => {
    const l = list.create({ document, api: fakeApi({}) });

    const initial = schemeButtons(l);
    assert.equal(initial.length, 3, 'three scheme buttons are expected');
    // Compared as a JOINED STRING, not with deepEqual. SCHEME_DEFS is an
    // array built inside the vm realm, so .map() on it returns a
    // vm-realm Array; assert.deepEqual compares prototypes and fails on
    // two structurally identical arrays from different realms.
    assert.equal(initial.map((b) => b.value).join(','),
        list.SCHEME_DEFS.map((d) => d.v).join(','),
        'the buttons must be built from SCHEME_DEFS, in that order');

    // EVERY OPTION IS A REAL <option> INSIDE A REAL <select>. That is
    // what replaced the aria-pressed contract this test used to check:
    // an <option> has no aria-pressed and needs none, because a select
    // announces its own two-state nature and its own current choice.
    // What still has to hold is that exactly ONE is marked.
    for (const b of initial) {
        assert.equal(b.tag, 'option',
            `the "${b.value}" choice is a <${b.tag}>, not a real option`);
    }
    assert.equal(l.element.querySelectorAll('select').length, 1,
        'the scheme chooser must be one real form control');
    assert.equal(initial.filter((b) => b.pressed === 'true').length, 1,
        'exactly one scheme option may be selected');
    // THE DEFAULT IS THE OWNER'S OWN SESSIONS, NOT EVERYTHING. Measured
    // 2026-08-31: 19,588 of 21,039 transcripts (93.1%) are agent
    // sidechain files, so opening on `all` opened on 93 percent noise.
    // Asserted against DEFAULT_SCHEME rather than against the literal
    // 'uuid', so the constant stays the single declaration of it.
    assert.equal(list.DEFAULT_SCHEME, list.SCHEME_FILTERS.CONVERSATIONS,
        'the shipped default must be the uuid (top-level session) scheme');
    assert.equal(initial.find((b) => b.pressed === 'true').value,
        list.DEFAULT_SCHEME, 'the default filter must be the selected one');

    // IT FOLLOWS THE FILTER. Nothing has been loaded, so the reload
    // refuses with 'no-scope' - and the button state must still move,
    // because the choice WAS made even though the query could not run.
    assert.equal(await l.setSchemeFilter(list.SCHEME_FILTERS.ALL),
        'no-scope');
    const after = schemeButtons(l);
    assert.equal(after.filter((b) => b.pressed === 'true').length, 1);
    assert.equal(after.find((b) => b.pressed === 'true').value,
        list.SCHEME_FILTERS.ALL,
        'the pressed button did not follow setSchemeFilter');
    assert.equal(
        after.find((b) => b.value === list.DEFAULT_SCHEME).pressed, 'false',
        'the previously selected option was not de-selected');

    await l.setSchemeFilter(list.SCHEME_FILTERS.SIDECHAINS);
    const third = schemeButtons(l);
    assert.equal(third.filter((b) => b.pressed === 'true').length, 1);
    assert.equal(third.find((b) => b.pressed === 'true').value,
        list.SCHEME_FILTERS.SIDECHAINS);
});

await test('C2: list.scheme() reflects the filter currently applied', async () => {
    const l = list.create({ document, api: fakeApi({}) });
    assert.equal(typeof l.scheme, 'function',
        'the list exposes no scheme(), so the composition root would have ' +
        'to keep a second copy of the value that could drift from this one');
    assert.equal(l.scheme(), list.DEFAULT_SCHEME);

    await l.setSchemeFilter(list.SCHEME_FILTERS.SIDECHAINS);
    assert.equal(l.scheme(), list.SCHEME_FILTERS.SIDECHAINS);

    // And it agrees with what the DOM announces, so the reader and the
    // caller cannot be told two different things.
    assert.equal(schemeButtons(l).find((b) => b.pressed === 'true').value,
        l.scheme(), 'scheme() and the selected option disagree');

    // An empty value falls back to the DEFAULT rather than to an unknown
    // scheme, which the server would refuse with a 400.
    await l.setSchemeFilter('');
    assert.equal(l.scheme(), list.DEFAULT_SCHEME);
});

await test('C3: the nav exposes filterInput, filterText() and clearFilter(), and clearFilter clears BOTH', () => {
    const n = nav.create({ document, api: fakeApi({}) });

    // The three things the composition root needs and had none of.
    assert.ok(n.filterInput, 'no filterInput to focus for the / key');
    assert.equal(n.filterInput.tagName.toLowerCase(), 'input');
    assert.equal(n.filterInput.getAttribute('type'), 'search');
    assert.equal(typeof n.filterText, 'function', 'no way to read the filter text');
    assert.equal(typeof n.clearFilter, 'function', 'no way to clear the filter');

    assert.equal(n.filterText(), '',
        'an unfiltered nav must report empty string, not null or undefined');

    // Driven the way a person drives it: typing into the box. Setting
    // state directly would pass against an input that is wired to
    // nothing at all.
    n.filterInput.value = 'nix';
    n.filterInput.dispatchEvent('input');
    assert.equal(n.filterText(), 'nix',
        'typing in the filter box did not reach the filter state');

    n.clearFilter();
    // BOTH, and this is the whole point of the assertion. Clearing only
    // the STATE leaves the box showing text that no longer filters
    // anything, which reads as a broken filter; clearing only the INPUT
    // leaves rows hidden with nothing on screen explaining why.
    assert.equal(n.filterText(), '', 'clearFilter left the filter state set');
    assert.equal(n.filterInput.value, '',
        'clearFilter left the text in the input box');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
