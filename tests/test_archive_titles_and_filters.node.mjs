// A ROW LEADS WITH A NAME, A CRUMB NAMES THINGS, AND A FILTER THAT
// MATCHES CHARACTERS OUT OF ORDER STILL FINDS THE ROW.
//
// THE FOUR DEFECTS THIS PINS. Every one of them rendered plausibly and
// none of them errored:
//   1. THE ROW LED WITH A BARE UUID - the single field on it nobody
//      recognises - while the session's actual name went unrendered.
//   2. A HUMAN-CHOSEN NAME AND A MACHINE GUESS LOOKED IDENTICAL. A
//      `custom-title` is a name somebody typed; a `last-prompt` is an
//      excerpt the ingest lifted off the last prompt, which is often a
//      sentence fragment and is sometimes actively wrong about what the
//      session was for. Presenting the second in the first's treatment
//      lends a guess the authority of a fact.
//   3. THE BREADCRUMB SAID `project 48 > transcript 5767`. Two database
//      primary keys, reading like information.
//   4. THE ONLY FILTER WAS SERVER-SIDE AND COARSE. Nothing could find
//      `ee039f7f-...-30a4e28483bb` from `e483bb`, because that is not a
//      contiguous substring of it - which is exactly how a person
//      remembers a UUID.
//
// AND THE FALLBACK IS THE SUBTLE ONE. When a row has NO title it leads
// with its session_ref, and that must not be dressed as a name: an
// unnamed session has to look unnamed, or every one of them reads as
// though somebody named it after its own UUID.
//
// Run with: node tests/test_archive_titles_and_filters.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * IT AWAITS - see test_archive_nav_list.node.mjs for what a
 * non-awaiting harness silently does to an async body.
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
 * Load the row, fuzzy, crumb, filter and list modules into one context.
 * @returns {object} The exported modules plus the document.
 */
function load() {
    const env = createEnvironment();
    const context = {
        window: { document: env.document },
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-fuzzy.js',
                        'archive-crumb.js', 'archive-tlist-row.js',
                        'archive-tlist-filter.js', 'archive-transcript-list.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return {
        row: context.window.ArchiveTlistRow,
        fuzzy: context.window.ArchiveFuzzy,
        crumb: context.window.ArchiveCrumb,
        list: context.window.ArchiveTranscriptList,
        document: env.document,
    };
}

const { row, fuzzy, crumb, list, document } = load();

/** A row with a human-chosen name. @returns {object} */
function named() {
    return {
        transcript_id: 11, session_ref: 'ee039f7f-cfac-4688-86dc-30a4e28483bb',
        session_ref_scheme: 'uuid', title: 'Fleet digest liveness',
        title_source: 'custom-title', line_count: 5073,
        raw_byte_length: 1024, ingested_at: '2026-08-29T18:28:32Z',
        host_attribution: 'manifest_verified',
    };
}

/** The same row with a derived title. @returns {object} */
function guessed() {
    const r = named();
    r.title = 'fix the thing that broke last night when';
    r.title_source = 'last-prompt';
    return r;
}

/** The same row with no title at all. @returns {object} */
function unnamed() {
    const r = named();
    delete r.title;
    r.title_source = null;
    return r;
}

/**
 * Render one row and return handles onto its parts.
 * @param {object} r - A transcript row.
 * @param {object} spans - Optional fuzzy spans.
 * @returns {object} {li, title, source, ref}
 */
function render(r, spans) {
    const li = row.renderRow(document, r, { spans: spans || {} });
    const q = (c) => li.querySelector('.archive-tlist__' + c);
    return { li, title: q('title'), source: q('source'), ref: q('ref') };
}

// ---- POSITIVE CONTROL --------------------------------------------------
// Every assertion below reads a class out of a rendered row. If the
// renderer emitted nothing, or emitted different class names, each
// querySelector would return null and a `.textContent` comparison
// against another null would pass.

await test('POSITIVE CONTROL: a rendered row actually has the parts read below', () => {
    const r = render(named());
    for (const [k, v] of Object.entries(r)) {
        assert.notEqual(v, null, `the renderer emitted no ${k}`);
    }
    assert.ok(r.title.textContent.length > 0, 'the title element is empty');
});

// ---- 1. THE NAME LEADS -------------------------------------------------

await test('a row leads with its TITLE when it has one', () => {
    const r = render(named());
    assert.equal(r.title.textContent, 'Fleet digest liveness');
    assert.equal(r.title.getAttribute('data-is-title'), 'true');
    // And the ref is still present - it is what the file on disk and the
    // export filename are called - just no longer the headline.
    assert.equal(r.ref.textContent, 'ee039f7f-cfac-4688-86dc-30a4e28483bb');
});

await test('a MISSING title falls back to the ref WITHOUT implying a name exists', () => {
    const r = render(unnamed());
    assert.equal(r.title.textContent, 'ee039f7f-cfac-4688-86dc-30a4e28483bb',
        'the fallback did not render the ref');
    // THE WHOLE POINT. The fallback is flagged as not-a-title, so the
    // stylesheet drops it out of the name treatment. Without this, every
    // unnamed session reads as though it were named after its own UUID.
    assert.equal(r.title.getAttribute('data-is-title'), 'false',
        'the ref fallback is presented as though it were a title');
    assert.match(r.source.textContent, /NOT NAMED/,
        'nothing on the row states that there is no name');
});

await test('a row with NEITHER a title nor a ref states the absence rather than rendering blank', () => {
    const bare = unnamed();
    delete bare.session_ref;
    const r = render(bare);
    assert.ok(r.title.textContent.length > 0,
        'an empty cell is indistinguishable from a row that has not loaded');
    assert.equal(r.title.getAttribute('data-is-title'), 'false');
});

// ---- 2. A NAME AND A GUESS DO NOT LOOK ALIKE ---------------------------

await test('custom-title and last-prompt are DISTINGUISHABLE, in text and in class', () => {
    const a = render(named());
    const b = render(guessed());

    assert.notEqual(a.source.textContent, b.source.textContent,
        'a chosen name and a derived guess carry the same label, so a ' +
        'guess is presented with the authority of a name');
    assert.notEqual(a.source.getAttribute('class'), b.source.getAttribute('class'),
        'the two sources share a class, so no stylesheet can tell them apart');

    // The distinction is carried in WORDS, not only in a colour - three
    // themes here zero every radius token and one is two shades of green.
    assert.match(a.source.textContent, /NAMED/);
    assert.match(b.source.textContent, /NOT A NAME/i);
    // And the hint says what a derived title actually is.
    assert.match(b.source.getAttribute('title') || '', /NOT a title/i,
        'the hint does not say what a last-prompt title actually is');
});

await test('all FOUR of the server\'s title sources are known, and ranked', () => {
    // Measured on the live server 2026-09-01: project 8 alone returns
    // `custom-title` and `ai-title`, and src/core/archive_titles.py
    // declares four in precedence order. A client that knows only two
    // renders the other two as NAME SOURCE NOT KNOWN - technically the
    // correct third outcome, and still wrong, because the source IS
    // known and the server documented it.
    for (const src of ['custom-title', 'ai-title', 'summary', 'last-prompt']) {
        const r = named();
        r.title_source = src;
        assert.notEqual(row.titleSource(r).kind, 'cannot-determine',
            `"${src}" is a documented server source but renders as unknown`);
    }
    // THE RANKING SURVIVES: a name a person chose, a machine-generated
    // one, and the weak last-prompt fallback are three different looks.
    const kindOf = (src) => { const r = named(); r.title_source = src; return row.titleSource(r).kind; };
    assert.equal(kindOf('custom-title'), 'human');
    assert.equal(kindOf('ai-title'), 'derived');
    assert.equal(kindOf('summary'), 'derived');
    assert.equal(kindOf('last-prompt'), 'weak',
        'last-prompt is the WEAK fallback - the server records measured ' +
        'values of "yes" and "exirt" - and must not read like a title');
    assert.notEqual(kindOf('last-prompt'), kindOf('ai-title'),
        'a generated title and the last thing somebody typed look alike');
});

await test('a FAILED title lookup is not reported as "this session has no name"', () => {
    // The server ships `title: null, title_source: "cannot_determine"`
    // when it could not READ the title records, and `title: null,
    // title_source: null` when it looked and found none. Testing the
    // empty title FIRST collapses the two and reports a measurement the
    // server explicitly declined to make. This is the archive's own
    // three-outcome rule applied to the title lookup itself.
    const failed = unnamed();
    failed.title_source = 'cannot_determine';
    assert.equal(row.titleSource(failed).kind, 'cannot-determine',
        'a FAILED lookup was rendered as an established absence');
    assert.notEqual(row.titleSource(failed).kind, row.titleSource(unnamed()).kind,
        '"no name exists" and "the name could not be looked up" render alike');
    assert.match(render(failed).source.textContent, /FAILED/,
        'nothing on the row says the lookup failed');
    assert.match(render(unnamed()).source.textContent, /NOT NAMED/);
});

await test('an UNRECOGNISED title_source is a could-not-determine, not a silent third guess', () => {
    const weird = named();
    weird.title_source = 'some-source-shipped-next-quarter';
    const r = render(weird);
    assert.equal(row.titleSource(weird).kind, 'cannot-determine',
        'an unknown source classified as a known one');
    assert.match(r.source.textContent, /NOT KNOWN/,
        'an unknown provenance was rendered as a fact');
    // It is its OWN state - not merged into "no name", which is a
    // measurement, and not into "named", which is a claim.
    assert.notEqual(row.titleSource(weird).kind, row.titleSource(unnamed()).kind);
    assert.notEqual(row.titleSource(weird).kind, row.titleSource(named()).kind);
});

await test('every title_source descriptor names a DISTINCT modifier class', () => {
    // The four SOURCES collapse to three visual TIERS on purpose -
    // ai-title and summary are both "generated about the session" and
    // the server ranks them adjacently - so the assertion is about the
    // tiers being distinct, not about there being one class per source.
    const tiers = new Set([row.TITLE_SOURCES['custom-title'].mod,
                           row.TITLE_SOURCES['ai-title'].mod,
                           row.TITLE_SOURCES['last-prompt'].mod,
                           row.TITLE_SOURCE_NONE.mod, row.TITLE_SOURCE_UNKNOWN.mod]);
    assert.equal(tiers.size, 5,
        'two states share a modifier, so they cannot be styled apart: ' +
        [...tiers].join(', '));
    assert.equal(row.TITLE_SOURCES['ai-title'].mod,
        row.TITLE_SOURCES['summary'].mod,
        'ai-title and summary are one tier by design; if that changes, ' +
        'the stylesheet needs a fifth rule');
});

// ---- 3. TRUNCATION AND THE TOOLTIP -------------------------------------

await test('a long label carries its FULL text in a title attribute', () => {
    const long = named();
    long.title = 'a session name long enough that no 22rem column will ever show all of it, '
        + 'which is the entire reason the tooltip has to exist';
    const r = render(long);
    assert.equal(r.title.getAttribute('title'), long.title,
        'the full text is not reachable, so a clipped label is unreadable ' +
        'with no way back - which is strictly worse than wrapping');
    assert.equal(r.ref.getAttribute('title'), long.session_ref,
        'the ref clips with no tooltip');
});

await test('the stylesheet CLIPS these labels rather than wrapping them', () => {
    // Read declarations, never prose: asserting over raw source finds
    // the string inside the comment that documents its removal.
    const css = fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'archive-panes.css'), 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, '');
    for (const cls of ['title', 'ref']) {
        const rx = new RegExp('\\.archive-tlist__' + cls + '\\s*\\{([^}]*)\\}');
        const block = rx.exec(css);
        assert.ok(block, `no rule for .archive-tlist__${cls}`);
        assert.match(block[1], /text-overflow:\s*ellipsis/,
            `.archive-tlist__${cls} does not ellipsise`);
        assert.match(block[1], /white-space:\s*nowrap/,
            `.archive-tlist__${cls} still wraps`);
    }
    // The crumb too - it is a label in one of these panes.
    const screen = fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'archive-screen.css'), 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, '');
    const item = /\.archive-screen__crumb-item\s*\{([^}]*)\}/.exec(screen);
    assert.ok(item, 'no rule for the crumb item');
    assert.match(item[1], /text-overflow:\s*ellipsis/, 'the breadcrumb still wraps');

    // AND THE EXEMPTIONS ARE REAL. The outcome blocks and the two honesty
    // notes must NOT be clipped: a truncated "COULD NOT EVALUATE ..."
    // reason is indistinguishable from one with nothing to say.
    assert.doesNotMatch(css, /\.archive-outcome[^{]*\{[^}]*text-overflow/,
        'an outcome block was made to truncate, which is exactly the ' +
        'indistinguishability the three-outcome design exists to prevent');
});

// ---- 4. THE BREADCRUMB NAMES THINGS ------------------------------------

await test('a breadcrumb NEVER renders a numeric id', () => {
    const routes = [
        { view: 'project', projectId: 48, transcriptId: null },
        { view: 'transcript', projectId: 48, transcriptId: 5767 },
        { view: 'transcript', projectId: null, transcriptId: 5767 },
        { view: 'line', projectId: 48, transcriptId: 5767, lineNo: 1695 },
    ];
    for (const r of routes) {
        // The hardest case: NOTHING has been learned yet, which is what a
        // fresh deep link looks like before its header request resolves.
        const parts = crumb.labels(r, { project: null, transcript: null });
        assert.equal(crumb.hasNumericId(parts), false,
            'a bare id reached the crumb for ' + JSON.stringify(r) +
            ': ' + JSON.stringify(parts));
        for (const p of parts) assert.ok(p.length > 0, 'a blank crumb segment');
    }
});

await test('POSITIVE CONTROL: hasNumericId can actually return true', () => {
    // An assertion of absence over a detector that never fires is not a
    // measurement. This is the detector's own positive control.
    assert.equal(crumb.hasNumericId(['project 48']), true);
    assert.equal(crumb.hasNumericId(['transcript 5767']), true);
    assert.equal(crumb.hasNumericId(['5767']), true);
    assert.equal(crumb.hasNumericId(['Infrastructure']), false);
});

await test('a crumb prefers a NAME, labels a REFERENCE as one, and states an unknown', () => {
    assert.equal(crumb.projectSegment({ display_name: 'Infrastructure' }).kind, 'name');
    assert.equal(crumb.projectSegment({ display_name: 'Infrastructure' }).text,
        'Infrastructure');

    // `full_path` is the raw slug. It is shown, because it is the only
    // thing known - and LABELLED, so it does not read as a chosen name.
    const slug = crumb.projectSegment({ full_path: '-Users-jsugamele-Development' });
    assert.equal(slug.kind, 'ref');
    assert.match(slug.text, /^ref /);

    assert.equal(crumb.projectSegment(null).kind, 'unknown');
    assert.equal(crumb.sessionSegment({ title: 'Nightly sweep' }).text, 'Nightly sweep');
    assert.equal(crumb.sessionSegment({ session_ref: 'journal' }).kind, 'ref');
    assert.equal(crumb.sessionSegment(null).kind, 'unknown');
});

await test('the tracker only answers about the id the route names', () => {
    const t = crumb.createTracker();
    t.learnTranscript(5767, { title: 'The right one' });
    // A DIFFERENT transcript. Returning the remembered name here would
    // render the previous session's title over the current one - a wrong
    // name, which is worse than the id it replaced, because it is
    // believable.
    const parts = t.labelsFor({ view: 'transcript', projectId: null, transcriptId: 9999 });
    assert.ok(!parts.some((p) => p.includes('The right one')),
        'a fact about transcript 5767 was rendered for transcript 9999');
    const right = t.labelsFor({ view: 'transcript', projectId: null, transcriptId: 5767 });
    assert.ok(right.some((p) => p.includes('The right one')),
        'the tracker did not answer for the id it was told about');
});

// ---- 5. THE TYPE FILTER: COMPACT, AND DEFAULTED TO SESSIONS ------------

await test('the filter DEFAULTS to the owner\'s own sessions, not to everything', () => {
    assert.equal(list.DEFAULT_SCHEME, list.SCHEME_FILTERS.CONVERSATIONS,
        'the default is not the uuid (top-level session) scheme, so the ' +
        'list still opens on the 93 percent of the corpus that is agent ' +
        'sidechain files');
    const l = list.create({ document, api: {} });
    assert.equal(l.scheme(), list.DEFAULT_SCHEME);
});

await test('the chooser is ONE control, and it is a REAL one', () => {
    // SUPERSEDED, 2026-09-01. This used to assert a hand-built trigger
    // plus a floating menu, and a comment here used to argue that a
    // <select> "would have been smaller and would have silently dropped"
    // the aria-pressed contract. The owner overruled that on sight: "i
    // dont like the dropdown its fake and doesnt match." The argument
    // was correct about ARIA and never addressed the thing that actually
    // mattered - a div dressed as a select inherits none of the
    // platform's behaviour and none of the app's control styling.
    const l = list.create({ document, api: {} });

    const select = l.element.querySelector('select.archive-tlist__scheme');
    assert.notEqual(select, null, 'the scheme chooser is not a real select');
    assert.equal(l.element.querySelectorAll('select').length, 1,
        'one control, not three rows of buttons and not two selects');

    // THE FAKE IS GONE, and its absence is asserted rather than assumed:
    // a leftover trigger would be a second, stale control for the same
    // choice, which is worse than either shape on its own.
    assert.equal(l.element.querySelectorAll('.archive-tlist__scheme-trigger').length, 0);
    assert.equal(l.element.querySelectorAll('.archive-tlist__scheme-menu').length, 0);
    assert.equal(l.element.querySelectorAll('[aria-haspopup]').length, 0);
    assert.equal(l.element.querySelectorAll('[data-action="open-scheme-menu"]').length, 0);

    // IT STILL NAMES THE ACTIVE OPTION without anybody opening it. That
    // was the real requirement behind the old trigger, and a <select>
    // satisfies it natively through its value plus the selected option.
    const activeLabel = list.SCHEME_DEFS
        .find((d) => d.v === list.DEFAULT_SCHEME).label;
    assert.equal(select.value, list.DEFAULT_SCHEME);
    assert.equal(select.getAttribute('data-scheme-active'), list.DEFAULT_SCHEME);
    const on = [...l.element.querySelectorAll('[data-scheme-filter]')]
        .find((o) => o.getAttribute('selected') === 'selected');
    assert.notEqual(on, null, 'no option is marked as the current choice');
    assert.equal(on.textContent, activeLabel);
});

await test('the state contract SURVIVES the swap to a native control', () => {
    // The old contract was aria-pressed on EVERY option, so the off
    // state stayed a state. An <option> has no aria-pressed and needs
    // none - a <select> announces its own current choice - so what is
    // asserted now is the platform's own equivalent, in the two
    // independent places this view writes it: exactly one option carries
    // `selected`, and the select's value and data attribute agree with
    // it. That is a REAL change from the previous contract, not a
    // rewording of it, and it is recorded rather than glossed.
    const l = list.create({ document, api: {} });
    const opts = [...l.element.querySelectorAll('[data-scheme-filter]')];
    assert.equal(opts.length, list.SCHEME_DEFS.length,
        'not every option is reachable from the chooser');
    for (const o of opts) {
        assert.equal(o.tagName.toLowerCase(), 'option',
            'an option is not a real <option>, so the control is still fake');
    }
    const on = opts.filter((o) => o.getAttribute('selected') === 'selected');
    assert.equal(on.length, 1, 'exactly one option may be current');
    assert.equal(on[0].getAttribute('data-scheme-filter'), list.DEFAULT_SCHEME);

    const select = l.element.querySelector('select.archive-tlist__scheme');
    assert.equal(select.value, on[0].getAttribute('data-scheme-filter'),
        'the select value and the marked option disagree, so a reader and ' +
        'a caller would be told two different things');
});

// ---- 6. FUZZY MATCHING -------------------------------------------------

await test('fuzzy matching finds what an exact-substring matcher CANNOT', () => {
    const hay = 'ee039f7f-cfac-4688-86dc-30a4e28483bb';
    for (const q of ['e483bb', 'ee0383bb', 'eecfac86']) {
        assert.equal(hay.includes(q), false,
            `"${q}" IS a substring, so it proves nothing about fuzzy matching`);
        assert.notEqual(fuzzy.match(q, hay), null,
            `fuzzy matching missed "${q}", which a person would type from ` +
            'memory of the two ends of a UUID');
    }
    // A date typed the way people say it, not the way it is formatted.
    assert.notEqual(fuzzy.match('0829 1828', '2026-08-29 18:28:32'), null);
    // And a genuine non-match is still null, not a zero score.
    assert.equal(fuzzy.match('zzz', hay), null);
});

await test('a no-match is null and never a zero score, because zero is a real score', () => {
    const m = fuzzy.match('a', 'a');
    assert.notEqual(m, null);
    assert.equal(typeof m.score, 'number');
    // An empty query matches everything with no spans - a filter nobody
    // has typed into is not a filter, and returning null would empty the
    // list on first paint.
    const empty = fuzzy.match('', 'anything');
    assert.notEqual(empty, null);
    assert.equal(empty.spans.length, 0);
});

await test('ranking prefers a CONSECUTIVE run and a word boundary', () => {
    const run = fuzzy.match('abcd', 'abcd-zzzz').score;
    const scattered = fuzzy.match('abcd', 'a-b-c-d-z').score;
    assert.ok(run > scattered,
        `a contiguous run (${run}) must outrank a scattered match (${scattered})`);

    const boundary = fuzzy.match('df', 'ab-df').score;
    const mid = fuzzy.match('df', 'abdfx').score;
    assert.ok(boundary > mid,
        `a match at a word boundary (${boundary}) must outrank one mid-token (${mid})`);
});

await test('spans index the ORIGINAL string, so a UUID highlights unrewritten', () => {
    const hay = 'EE039F7F-cfac';
    const m = fuzzy.match('ee03', hay);
    assert.notEqual(m, null, 'matching is not case-insensitive');
    const segs = fuzzy.segments(hay, m.spans);
    // The segments reassemble the source EXACTLY - case folded for
    // matching, never for display.
    assert.equal(segs.map((s) => s.text).join(''), hay);
    assert.equal(segs.filter((s) => s.hit).map((s) => s.text).join(''), 'EE03');
});

await test('a rendered row MARKS the matched characters', () => {
    const m = fuzzy.match('e483bb', named().session_ref);
    const r = render(named(), { ref: m.spans });
    const marks = r.ref.querySelectorAll('.archive-tlist__hit');
    assert.ok(marks.length > 0, 'the match is not highlighted anywhere');
    assert.equal(r.ref.textContent, named().session_ref,
        'highlighting altered the text it was highlighting');
});

await test('rank() ANDs the columns and is stable under equal scores', () => {
    const rows = [named(), guessed(), unnamed()];
    const both = fuzzy.rank(rows, { title: 'fleet', ref: 'zzzzz' }, row.rowValue);
    assert.equal(both.length, 0,
        'the columns ORed instead of ANDing, so a row matching one filter ' +
        'survives both');
    const one = fuzzy.rank(rows, { title: 'fleet' }, row.rowValue);
    assert.equal(one.length, 1);
    assert.equal(one[0].row.title, 'Fleet digest liveness');

    // Equal scores keep the input order rather than reshuffling.
    const all = fuzzy.rank(rows, {}, row.rowValue);
    assert.equal(all.length, 3);
    // JOINED, NOT deepEqual'd. `rank()` runs inside a vm realm, so the
    // array it returns has that realm's Array.prototype, and
    // `assert/strict`'s deepEqual is deepStrictEqual - which compares
    // prototypes and fails on two structurally identical arrays with the
    // unhelpful "same structure but not reference-equal". This test
    // failed exactly that way first.
    assert.equal(all.map((m) => m.rank).join(','), '0,1,2',
        'equal scores reshuffled the rows instead of keeping input order');
});

await test('isActive tells a typed-but-unmatched filter from an untouched one', () => {
    // These are two completely different things to render and merging
    // them is how "your filter matched nothing" becomes a blank pane.
    assert.equal(fuzzy.isActive({ title: '', ref: '', date: '' }), false);
    assert.equal(fuzzy.isActive({ title: 'x' }), true);
    assert.equal(fuzzy.isActive(null), false);
});

await test('rowValue reads the string that is actually ON SCREEN', () => {
    // Filtering a value the person cannot see makes a filter that fails
    // for reasons nobody can inspect.
    const r = named();
    assert.equal(row.rowValue(r, 'title'), 'Fleet digest liveness');
    assert.equal(row.rowValue(r, 'ref'), r.session_ref);
    const shown = render(r).li.querySelector('.archive-tlist__ingested').textContent;
    assert.equal(row.rowValue(r, 'date'), shown,
        'the date column filters on a different string than it displays');
});

// ---- 7. THE FUZZY FILTER IS HONEST ABOUT ITS SCOPE ---------------------

await test('the fuzzy note says it covers only LOADED rows, and names the unknown', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'archive-tlist-filter.js'), 'utf8');
    assert.match(src, /LOADED SO FAR/,
        'the note does not state that the filter covers only fetched rows');
    assert.match(src, /NOT KNOWN/,
        'an unknown has_more is not rendered as an unknown');

    // And the SERVER-side note is untouched - the two scopes are
    // different statements and merging them makes one of them a lie.
    const rowSrc = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'archive-tlist-row.js'), 'utf8');
    assert.match(rowSrc, /filtered the WHOLE scope/,
        'the server-side filter note was regressed into implying a ' +
        'client-side scope');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
