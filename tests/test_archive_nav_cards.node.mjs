// THE PROJECT CARD, ITS TWO COUNTS, AND THE INFO MODAL THE MACHINE PILLS
// FOLDED INTO.
//
// Every fixture below is a real shape captured from the live dev instance
// at 127.0.0.1:5055 on 2026-09-02:
//   - GET /api/v1/archive/projects returns 77 merged nodes whose keys are
//     exactly display_name, full_path, host_count, hosts, members,
//     observed_cwd, project_id, transcript_count. There is NO per-project
//     session count in that payload or its meta, which is why the sessions
//     figure renders NOT KNOWN today and why that is the correct answer
//     rather than a bug to paper over with the total.
//   - exactly 3 of the 77 appear on both machines (jsugamele, Media,
//     vibrant-leakey-ea30bb), so the ONE-machine case is 74 of 77 and is
//     the one most likely to read as broken if it is written as a list.
//   - the overlay contract (src/core/archive_overlay.py, landed the same
//     day) puts {status, group, hidden, applied} on each node and keeps
//     the archive's own name as archive_display_name.
//
// THE HARNESS AWAITS EVERY BODY. A `test()` that calls fn() without
// awaiting records a pass the instant the promise is created; the
// assertions then run after the verdict and throw into an unhandled
// rejection, leaving the suite green. That is a verification step that
// cannot fail, which is the single worst place for one.
//
// NOTHING HERE USES deepStrictEqual ON AN OBJECT. These modules are
// evaluated inside vm.runInContext, so their objects have a DIFFERENT
// Object.prototype from this realm's and two identical empty objects
// compare unequal. Assertions are on Object.keys(...).length and on
// primitives.
//
// Run with: node tests/test_archive_nav_cards.node.mjs

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
 * Run one named assertion block. Awaits, so an async body's assertions
 * are inside the verdict rather than after it.
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
 * Load the rail, the card, the modal and the modal stack into one vm
 * context.
 *
 * `document.body.querySelectorAll` is shimmed for the one selector
 * ModalStack uses, `:scope > .modal-overlay`. mini-dom's selector parser
 * has no `:scope` or child-combinator support, so without this the module
 * would enumerate nothing and its "is a foreign overlay on top" guard
 * would silently answer false for every keypress - a check that cannot
 * fail, in the middle of the thing under test.
 * @returns {object} The loaded modules and the environment.
 */
function load() {
    const env = createEnvironment();
    const document = env.document;

    const realQsa = document.body.querySelectorAll.bind(document.body);
    document.body.querySelectorAll = function (selector) {
        if (selector === ':scope > .modal-overlay') {
            return document.body.childNodes.filter(
                (n) => n.classList && n.classList.contains('modal-overlay'));
        }
        return realQsa(selector);
    };

    const fakeWindow = { document };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['modal-stack.js', 'archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-nav-row.js',
                        'archive-nav-card.js', 'archive-nav-info.js',
                        'archive-nav-tree.js', 'archive-nav-fuzzy.js',
                        'archive-nav-merged.js', 'archive-nav.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file });
    }
    return {
        card: context.window.ArchiveNavCard,
        info: context.window.ArchiveNavInfo,
        nav: context.window.ArchiveNav,
        merged: context.window.ArchiveNavMerged,
        row: context.window.ArchiveNavRow,
        stack: context.window.ModalStack,
        document,
        context,
    };
}

const M = load();

/**
 * A merged project node in the exact shape the live endpoint returns.
 * @param {object} over - Fields to override.
 * @returns {object} A project node.
 */
function node(over = {}) {
    return Object.assign({
        project_id: 2,
        display_name: '.claude2',
        full_path: '-Users-jsugamele--claude2',
        observed_cwd: '/Users/jsugamele/.claude2',
        hosts: ['Joe-MBP-M1'],
        host_count: 1,
        transcript_count: 53,
        members: [{
            project_id: 2, corpus_id: 1, host_id: 1,
            host_display_name: 'Joe-MBP-M1',
            slug: '-Users-jsugamele--claude2', transcript_count: 53,
        }],
    }, over);
}

/** The real dual-machine project, captured 2026-09-02. */
function dualNode() {
    return node({
        project_id: 41, display_name: 'Media', full_path: '-Users-j-Media',
        observed_cwd: '/Users/j/Media',
        hosts: ['Joe-MBP-M1', 'Joseph’s Mac mini (2)'],
        host_count: 2, transcript_count: 2896,
        members: [
            { project_id: 41, corpus_id: 1, host_id: 1,
              host_display_name: 'Joe-MBP-M1', transcript_count: 2800 },
            { project_id: 88, corpus_id: 3, host_id: 2,
              host_display_name: 'Joseph’s Mac mini (2)', transcript_count: 96 },
        ],
    });
}

/** Render one card and return its <li>. */
function cardFor(over = {}, opts = {}) {
    return M.card.renderCard(M.document, node(over), opts);
}

/** The text of one selector inside a card, or null. */
function textIn(li, selector) {
    const hit = li.querySelector(selector);
    return hit ? hit.textContent : null;
}

// =====================================================================
// TASK 1 - THE CARD
// =====================================================================

await test('a project renders as a CARD carrying the app card structure', () => {
    const li = cardFor();
    assert.equal(li.tagName.toLowerCase(), 'li');
    assert.equal(li.getAttribute('data-node-kind'), 'project');
    const card = li.querySelector('.archive-nav__card');
    assert.ok(card, 'the project node must contain a .archive-nav__card');
    // The face is still a .archive-nav__row button, so the rail's
    // selection and keyboard code keeps working unchanged.
    const face = card.querySelector('.archive-nav__row');
    assert.ok(face, 'the card must contain the selectable row button');
    assert.equal(face.tagName.toLowerCase(), 'button');
    assert.equal(face.getAttribute('data-action'), 'select');
});

await test('the merged list paints cards, not bare rows', () => {
    const slot = M.document.createElement('ul');
    M.merged.paint(M.document, slot, {
        nodes: [node(), dualNode()],
        unattributed: [], hostId: null, filterText: '', onActivate() {},
    });
    const cards = slot.querySelectorAll('.archive-nav__card');
    assert.equal(cards.length, 2, 'both projects must be cards');
});

await test('a long name TRUNCATES with a title rather than wrapping', () => {
    const long = '-Users-jsugamele-Development-Assistants-Infrastructure-projects-monitoring';
    const li = cardFor({ display_name: null, full_path: long, observed_cwd: null });
    const label = li.querySelector('.archive-nav__label');
    // One text node, no <br>, no per-word elements: wrapping would have
    // to come from CSS, and the CSS pins white-space: nowrap.
    assert.equal(label.childNodes.length, 1, 'the label must be a single text node');
    assert.equal(label.textContent, long);
    const title = li.querySelector('.archive-nav__row').getAttribute('title');
    assert.ok(title && title.includes(long),
        'the full text must survive in the title attribute');
});

// =====================================================================
// TASK 2 - THE TWO COUNTS
// =====================================================================

await test('the SESSIONS figure is present and comes FIRST', () => {
    const li = cardFor({ session_count: 27 });
    const counts = li.querySelectorAll('.archive-nav__count');
    assert.equal(counts.length, 2, 'exactly two counts');
    assert.equal(counts[0].getAttribute('data-count'), 'sessions',
        'the sessions figure must be the first one rendered');
    assert.equal(counts[1].getAttribute('data-count'), 'transcripts');
});

await test('the two counts are distinguishable by WORDS, not only by colour', () => {
    const li = cardFor({ session_count: 27 });
    const sessions = li.querySelector('[data-count="sessions"]');
    const total = li.querySelector('[data-count="transcripts"]');
    assert.equal(textIn(sessions, '.archive-nav__count-value'), '27');
    assert.equal(textIn(sessions, '.archive-nav__count-noun'), 'sessions');
    assert.equal(textIn(total, '.archive-nav__count-value'), '53');
    // `total`, not `transcripts`: measured in a real browser, the longer
    // word did not fit the rail's ~198px counts line and ellipsised away
    // on all 77 cards, so it was a noun nobody could read. The full word
    // is in the block's title and in the modal.
    assert.equal(textIn(total, '.archive-nav__count-noun'), 'total');
    assert.notEqual(textIn(sessions, '.archive-nav__count-noun'),
        textIn(total, '.archive-nav__count-noun'),
        'the two halves must carry DIFFERENT nouns, not just different colours');
    const tip = li.querySelector('.archive-nav__counts').getAttribute('title');
    assert.ok(tip.includes('transcripts'),
        'the word the face has no room for must survive in the tooltip');
    // The connective is what makes them ONE sentence - 27 sessions OF 53
    // transcripts - rather than two rival measures of the same thing.
    assert.equal(textIn(total, '.archive-nav__count-of'), 'of');
});

await test('a MISSING session count renders NOT KNOWN and never 0', () => {
    // This is the live shape today: the merged endpoint carries no
    // session count at all.
    const li = cardFor();
    const sessions = li.querySelector('[data-count="sessions"]');
    assert.equal(sessions.getAttribute('data-session-state'), 'not-reported');
    assert.equal(textIn(sessions, '.archive-nav__count-value'), 'NOT KNOWN');
    // The total must NOT have leaked into the sessions slot.
    assert.notEqual(textIn(sessions, '.archive-nav__count-value'), '53');
    assert.notEqual(textIn(sessions, '.archive-nav__count-value'), '0');
});

await test('an explicitly UNCOUNTABLE session count is its own third state', () => {
    const li = cardFor({ session_counted: false });
    const sessions = li.querySelector('[data-count="sessions"]');
    assert.equal(sessions.getAttribute('data-session-state'), 'cannot-determine');
    assert.equal(textIn(sessions, '.archive-nav__count-value'), 'NOT KNOWN');
    // Same rendering, different recorded finding: "nobody asked" and "I
    // asked and could not tell" are not the same fact.
    const other = cardFor();
    assert.notEqual(
        other.querySelector('[data-count="sessions"]').getAttribute('data-session-state'),
        sessions.getAttribute('data-session-state'));
});

await test('sessionCountFor has exactly three outcomes and never invents a number', () => {
    assert.equal(M.card.sessionCountFor({ session_count: 27 }).state, 'known');
    assert.equal(M.card.sessionCountFor({ session_count: 27 }).value, 27);
    assert.equal(M.card.sessionCountFor({ session_count: 0 }).state, 'known');
    assert.equal(M.card.sessionCountFor({ session_count: 0 }).value, 0);
    assert.equal(M.card.sessionCountFor({ transcript_count: 718 }).state, 'not-reported');
    assert.equal(M.card.sessionCountFor({ transcript_count: 718 }).value, null);
    assert.equal(M.card.sessionCountFor({ session_count: null }).state, 'cannot-determine');
    assert.equal(M.card.sessionCountFor({ session_counted: false }).state, 'cannot-determine');
    assert.equal(M.card.sessionCountFor(null).state, 'not-reported');
});

await test('the counts tooltip names what each number means', () => {
    const known = M.card.countsTitle({ state: 'known', value: 27 }, 718);
    assert.ok(known.includes('27'), known);
    assert.ok(known.includes('718'), known);
    assert.ok(known.includes('sidechain'), 'the tooltip must explain the gap');
    const unknown = M.card.countsTitle({ state: 'not-reported', value: null }, 718);
    assert.ok(unknown.includes('NOT KNOWN'), unknown);
    assert.ok(!/\b27\b/.test(unknown), 'no invented figure in the unknown case');
});

await test('a missing TOTAL is NOT KNOWN too, preserving the existing rule', () => {
    const li = cardFor({ transcript_count: null });
    assert.equal(textIn(li.querySelector('[data-count="transcripts"]'),
        '.archive-nav__count-value'), 'NOT KNOWN');
});

// =====================================================================
// TASK 3 - THE PILLS ARE GONE AND THE MODAL HAS THEM
// =====================================================================

await test('NO machine pills on the card face', () => {
    const li = M.card.renderCard(M.document, dualNode(), {});
    assert.equal(li.querySelectorAll('.archive-nav__host-badge').length, 0);
    assert.equal(li.querySelectorAll('.archive-nav__hosts').length, 0);
    // Not merely absent: replaced by the affordance they folded into.
    const info = li.querySelector('.archive-nav__info-btn');
    assert.ok(info, 'the card must carry an info affordance');
    assert.equal(info.getAttribute('data-action'), 'info');
    assert.ok((info.getAttribute('aria-label') || '').includes('Media'));
});

await test('the info affordance opens the modal WITHOUT selecting the project', () => {
    let selected = 0;
    let opened = null;
    const li = M.card.renderCard(M.document, dualNode(), {
        onActivate() { selected++; },
        onInfo(row) { opened = row; },
    });
    let stopped = false;
    li.querySelector('.archive-nav__info-btn').dispatchEvent('click', {
        stopPropagation() { stopped = true; this._stopped = true; },
        preventDefault() {},
    });
    assert.ok(opened, 'the info handler must fire');
    assert.equal(opened.display_name, 'Media');
    assert.ok(stopped, 'the click must not bubble to the card face');
    assert.equal(selected, 0, 'opening details must not select the project');
});

await test('the modal lists BOTH machines of a dual-machine project, each a link', () => {
    const handle = M.info.open({ document: M.document, row: dualNode() });
    const section = handle.element.querySelector('[data-section="machines"]');
    assert.ok(section, 'a machines section must exist');
    assert.equal(section.getAttribute('data-machines'), '2');
    const links = section.querySelectorAll('[data-action="filter-host"]');
    assert.equal(links.length, 2, 'both machines must be links');
    assert.equal(links[0].getAttribute('data-host-id'), '1');
    assert.equal(links[1].getAttribute('data-host-id'), '2');
    assert.ok(section.textContent.includes('Joe-MBP-M1'));
    assert.ok(section.textContent.includes('Mac mini'));
    handle.close();
});

await test('a machine link filters BACK to that machine and closes the modal', () => {
    let got = null;
    const handle = M.info.open({
        document: M.document, row: dualNode(),
        onFilterHost(hostId, name) { got = { hostId, name }; },
    });
    handle.element.querySelector('[data-action="filter-host"]').dispatchEvent('click');
    assert.ok(got, 'the machine link must call back');
    assert.equal(got.hostId, 1);
    assert.equal(got.name, 'Joe-MBP-M1');
    assert.equal(handle.isOpen(), false, 'following a link must close the dialog');
});

await test('a ONE-machine project reads as a statement, not a list of one', () => {
    // 74 of the 77 real projects are in this case, so it is the ordinary
    // one and must not look like a list with items missing.
    const heading = M.info.machinesHeading(['Joe-MBP-M1']);
    assert.equal(heading.known, true);
    assert.equal(heading.text, 'Collected from one machine.');
    assert.ok(!heading.text.includes('1 machine'),
        'the singular case must be words, not a count of one');

    const handle = M.info.open({ document: M.document, row: node() });
    const section = handle.element.querySelector('[data-section="machines"]');
    assert.equal(section.getAttribute('data-machines'), '1');
    assert.ok(section.textContent.includes('Collected from one machine.'));
    assert.equal(section.querySelectorAll('[data-action="filter-host"]').length, 1,
        'the single machine is still a working link');
    handle.close();
});

await test('a plural heading counts the machines', () => {
    const h = M.info.machinesHeading(['a', 'b']);
    assert.equal(h.known, true);
    assert.ok(h.text.includes('2 machines'), h.text);
});

await test('a row with NO host list is COULD NOT EVALUATE, never an empty section', () => {
    // This is the by-machine drill-down's per-corpus row, which carries
    // no `hosts` at all. Rendering "no machines" would claim a project
    // belongs to nothing, which is a verdict nobody measured.
    const heading = M.info.machinesHeading(undefined);
    assert.equal(heading.known, false);
    const handle = M.info.open({
        document: M.document,
        row: { project_id: 9, display_name: 'Solo', transcript_count: 4 },
    });
    const section = handle.element.querySelector('[data-section="machines"]');
    assert.equal(section.getAttribute('data-machines'), 'cannot-determine');
    const block = section.querySelector('[data-outcome]');
    assert.ok(block, 'the refusal must go through ArchiveOutcomeView');
    assert.equal(block.getAttribute('data-outcome'), 'cannot-determine');
    assert.ok(section.textContent.includes('COULD NOT EVALUATE'));
    assert.equal(section.querySelectorAll('[data-action="filter-host"]').length, 0);
    handle.close();
});

await test('the modal carries the FULL PATH the card face has no room for', () => {
    const handle = M.info.open({ document: M.document, row: node() });
    const where = handle.element.querySelector('[data-section="path"]');
    assert.ok(where.textContent.includes('/Users/jsugamele/.claude2'),
        'the observed_cwd must be in the dialog');
    assert.ok(where.textContent.includes('-Users-jsugamele--claude2'),
        'the slug must be in the dialog');
    handle.close();
});

await test('an absent path in the modal is NOT KNOWN, never blank', () => {
    const handle = M.info.open({
        document: M.document,
        row: node({ observed_cwd: null, full_path: null, slug: null }),
    });
    const values = handle.element
        .querySelector('[data-section="path"]').querySelectorAll('[data-known]');
    assert.equal(values.length, 2);
    assert.equal(values[0].getAttribute('data-known'), 'false');
    assert.ok(values[0].textContent.includes('NOT KNOWN'));
    handle.close();
});

await test('the modal restates the counts and never substitutes one for the other', () => {
    const handle = M.info.open({ document: M.document, row: node() });
    const counts = handle.element.querySelector('[data-section="counts"]');
    assert.ok(counts.textContent.includes('Your sessions'));
    assert.ok(counts.textContent.includes('NOT KNOWN'));
    assert.ok(counts.textContent.includes('53'), 'the real total is shown');
    handle.close();
});

// =====================================================================
// TASK 3 - MODAL STACK AND ESCAPE ORDER
// =====================================================================

await test('the modal registers with ModalStack and Escape pops only IT', () => {
    const before = M.stack.depth();
    const handle = M.info.open({ document: M.document, row: node() });
    assert.equal(M.stack.depth(), before + 1, 'opening must push one entry');
    assert.equal(M.stack.isTop(handle.element), true);

    // A second modal over the top: Escape must take the TOP one and leave
    // the first standing. That is the whole reason this goes through
    // ModalStack rather than owning a document listener of its own.
    const second = M.info.open({ document: M.document, row: dualNode() });
    assert.equal(M.stack.depth(), before + 2);
    assert.equal(M.stack.isTop(second.element), true);

    M.document.dispatchEvent('keydown', { key: 'Escape' });
    assert.equal(second.isOpen(), false, 'Escape closes the TOP modal');
    assert.equal(handle.isOpen(), true, 'and leaves the one underneath open');
    assert.equal(M.stack.depth(), before + 1);

    M.document.dispatchEvent('keydown', { key: 'Escape' });
    assert.equal(handle.isOpen(), false, 'a second Escape closes the next one');
    assert.equal(M.stack.depth(), before);
});

await test('closing removes the overlay from the DOM and pops the stack once', () => {
    const before = M.stack.depth();
    const handle = M.info.open({ document: M.document, row: node() });
    assert.ok(M.document.body.contains(handle.element));
    handle.close();
    assert.equal(M.document.body.contains(handle.element), false);
    assert.equal(M.stack.depth(), before);
    // Idempotent: a double close must not pop somebody else's entry.
    handle.close();
    assert.equal(M.stack.depth(), before);
});

await test('the close button closes, and a click inside the dialog does NOT', () => {
    const handle = M.info.open({ document: M.document, row: node() });
    handle.element.querySelector('.archive-nav-info__title').dispatchEvent('click');
    assert.equal(handle.isOpen(), true, 'a click on the dialog body must not close it');
    handle.element.querySelector('.archive-nav-info__close').dispatchEvent('click');
    assert.equal(handle.isOpen(), false);
});

await test('opening a second info modal from the rail REPLACES the first', () => {
    const opener = M.info.wire({ document: M.document });
    const first = opener.open(node());
    const second = opener.open(dualNode());
    assert.equal(first.isOpen(), false, 'the previous one must be taken down');
    assert.equal(second.isOpen(), true);
    assert.equal(opener.current(), second);
    second.close();
});

// =====================================================================
// TASK 4 - THE OVERLAY CONTRACT
// =====================================================================

await test('with NO overlay block the card shows the archive name, state absent', () => {
    const pres = M.card.presentationFor(node(), null);
    assert.equal(pres.name, '.claude2');
    assert.equal(pres.serverName, '.claude2');
    assert.equal(pres.renamed, false);
    assert.equal(pres.group, null);
    assert.equal(pres.hidden, false);
    // 'absent' is NOT 'none': one means the owner said nothing, the other
    // means this build never asked.
    assert.equal(pres.overlayStatus, 'absent');
});

await test('an APPLIED overlay renames the card and keeps the real name', () => {
    const over = node({
        display_name: 'Second Claude',
        archive_display_name: '.claude2',
        overlay: { status: 'applied', group: 'Infra', hidden: false,
                   applied: ['display_name', 'group'],
                   identity_key: 'cwd:/Users/jsugamele/.claude2',
                   identity_kind: 'cwd' },
    });
    const pres = M.card.presentationFor(over, null);
    assert.equal(pres.name, 'Second Claude');
    assert.equal(pres.serverName, '.claude2');
    assert.equal(pres.renamed, true);
    assert.equal(pres.group, 'Infra');
    assert.equal(pres.overlayStatus, 'applied');

    const li = M.card.renderCard(M.document, over, {});
    assert.equal(textIn(li, '.archive-nav__label'), 'Second Claude');
    assert.equal(li.getAttribute('data-project-group'), 'Infra');
    assert.equal(li.getAttribute('data-project-renamed'), 'true');

    // The modal must show BOTH, so a rename never becomes the second
    // place you cannot find out what a thing really is.
    const handle = M.info.open({ document: M.document, row: over, presentation: pres });
    const sec = handle.element.querySelector('[data-section="overlay"]');
    assert.ok(sec.textContent.includes('Second Claude'));
    assert.ok(sec.textContent.includes('.claude2'));
    assert.ok(sec.textContent.includes('Infra'));
    handle.close();
});

await test('renaming a project to its OWN name still reads as renamed', () => {
    // The server's own contract note warns about exactly this: a client
    // must not infer "nothing set" from an unchanged name. `renamed` is
    // read off `applied`, never off a string comparison.
    const over = node({
        display_name: '.claude2', archive_display_name: '.claude2',
        overlay: { status: 'applied', group: null, hidden: false,
                   applied: ['display_name'] },
    });
    assert.equal(M.card.presentationFor(over, null).renamed, true);
});

await test('a status of none is not confused with an applied one', () => {
    const over = node({
        archive_display_name: '.claude2',
        overlay: { status: 'none', group: null, hidden: false, applied: [] },
    });
    const pres = M.card.presentationFor(over, null);
    assert.equal(pres.overlayStatus, 'none');
    assert.equal(pres.renamed, false);
});

await test('an unaddressable project reports cannot_determine, not "nothing set"', () => {
    const over = node({
        overlay: { status: 'cannot_determine', group: null, hidden: false,
                   applied: [], identity_key: null,
                   reason: 'project has neither observed_cwd nor a project id' },
    });
    const pres = M.card.presentationFor(over, null);
    assert.equal(pres.overlayStatus, 'cannot_determine');
    const handle = M.info.open({ document: M.document, row: over, presentation: pres });
    const sec = handle.element.querySelector('[data-section="overlay"]');
    assert.ok(sec, 'an unaddressable project still gets the section');
    assert.equal(sec.getAttribute('data-overlay-status'), 'cannot_determine');
    assert.ok(sec.textContent.includes('NOT KNOWN'));
    handle.close();
});

await test('a HIDDEN project is marked on the card, never silently dropped', () => {
    const over = node({
        overlay: { status: 'applied', group: null, hidden: true, applied: ['hidden'] },
    });
    const li = M.card.renderCard(M.document, over, {});
    assert.equal(li.getAttribute('data-project-hidden'), 'true');
});

// =====================================================================
// THE FUZZY FILTER STILL WORKS ON NAME AND PATH
// =====================================================================

await test('the fuzzy filter still matches on the NAME', () => {
    const slot = M.document.createElement('ul');
    const r = M.merged.paint(M.document, slot, {
        nodes: [node(), dualNode()],
        unattributed: [], hostId: null, filterText: 'medi', onActivate() {},
    });
    assert.equal(r.rendered, 1);
    assert.equal(textIn(slot, '.archive-nav__label'), 'Media');
});

await test('the fuzzy filter still matches on the FULL PATH', () => {
    const slot = M.document.createElement('ul');
    // 'jsugamele' appears only in .claude2's path and slug, not in the
    // display name of either node, so a hit proves the path field is
    // still being searched.
    const r = M.merged.paint(M.document, slot, {
        nodes: [node(), dualNode()],
        unattributed: [], hostId: null, filterText: 'jsugamele', onActivate() {},
    });
    assert.equal(r.rendered, 1);
    assert.equal(textIn(slot, '.archive-nav__label'), '.claude2');
});

await test('a fuzzy hit is highlighted inside the CARD label', () => {
    const slot = M.document.createElement('ul');
    M.merged.paint(M.document, slot, {
        nodes: [dualNode()],
        unattributed: [], hostId: null, filterText: 'med', onActivate() {},
    });
    assert.ok(slot.querySelectorAll('.archive-nav__hit').length > 0,
        'the card must still render fuzzy highlights');
});

// =====================================================================
// THE UNATTRIBUTED RULE IS UNCHANGED
// =====================================================================

await test('the unattributed rule is EXACTLY as it was: hidden only on a known zero', () => {
    assert.equal(M.row.shouldShowUnattributed({ unattributed_transcript_count: 0 }).show,
        false);
    assert.equal(M.row.shouldShowUnattributed({ unattributed_transcript_count: 5 }).show,
        true);
    assert.equal(M.row.shouldShowUnattributed({}).show, true);
    assert.equal(M.row.shouldShowUnattributed({ counted: false }).show, true);
    assert.equal(
        M.row.shouldShowUnattributed({ unattributed_transcript_count: 0, counted: false }).show,
        true, 'an uncounted zero is NOT a known zero');
});

await test('the unattributed NODE is still a row, not a card', () => {
    const slot = M.document.createElement('ul');
    M.merged.paint(M.document, slot, {
        nodes: [node()],
        unattributed: [{ corpus_id: 2, transcript_count: 5, counted: true }],
        hostId: null, filterText: '', onActivate() {},
    });
    const un = slot.querySelector('.archive-nav__node--unattributed');
    assert.ok(un, 'the unattributed node must still render');
    assert.equal(un.querySelectorAll('.archive-nav__card').length, 0,
        'only a project is a card');
    assert.ok(un.textContent.includes('belongs to no project'));
});

// =====================================================================
// THE RAIL WIRES IT ALL TOGETHER
// =====================================================================

await test('the rail opens the modal from a card click and filters from a machine', async () => {
    const nav = M.nav.create({
        document: M.document,
        api: {
            listArchiveMergedProjects: () => Promise.resolve({
                envelope: {
                    result: [node(), dualNode()],
                    result_status: 'ok', scope_status: 'resolved',
                    unevaluated: [], meta: { hosts: [], unattributed: { by_corpus: [] } },
                },
                httpStatus: 200, headers: null, transportError: null,
            }),
        },
        onSelect() {},
    });
    M.document.body.appendChild(nav.element);
    const token = await nav.loadMergedProjects();
    assert.equal(token, 'ok');
    assert.equal(nav.element.querySelectorAll('.archive-nav__card').length, 2);

    const dualCard = nav.element.querySelectorAll('.archive-nav__node--project')[1];
    dualCard.querySelector('.archive-nav__info-btn').dispatchEvent('click', {
        stopPropagation() { this._stopped = true; }, preventDefault() {},
    });
    const modal = nav.infoModal();
    assert.ok(modal && modal.isOpen(), 'the rail must open the modal');

    modal.element.querySelectorAll('[data-action="filter-host"]')[1]
        .dispatchEvent('click');
    assert.equal(modal.isOpen(), false, 'the modal closes on follow');
    // Host 2 holds only the dual project, so the rail must now show one.
    assert.equal(nav.lastPaint().rendered, 1,
        'following a machine link must narrow the rail to that machine');
    assert.equal(textIn(nav.element, '.archive-nav__label'), 'Media');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
