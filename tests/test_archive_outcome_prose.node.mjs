// THE OUTCOME BLOCKS SAY EACH FACT ONCE, AND STILL SAY ALL OF THEM.
//
// WHAT THIS EXISTS TO CATCH, AND WHAT IT MUST NOT BREAK.
//
// The three outcomes are correctly DISTINCT and that model is not what
// changed. The WORD COUNT was the bug. Measured before the trim, the
// partial state stated one fact - 801 of 3,416 read - five times in five
// registers, then repeated it in a second panel carrying a SECOND,
// identical "Resume the scan" button, all of it over diagonal barber-pole
// hatching that ran behind the body text. The empty state said "no
// matches" four times and volunteered
//
//   Scan charge: 91950363 bytes (a charge the server levies, not a
//   measure of work done)
//
// which is the server's internal billing accounting, in a result panel,
// to a person looking for a conversation they had.
//
// So this file asserts BOTH directions, because trimming prose is
// exactly the change that quietly deletes a fact:
//
//   * the leading sentence is ONE sentence and names an action;
//   * the audit trail moved behind a disclosure and is STILL PRESENT in
//     the block's textContent, verbatim, every number of it;
//   * no outcome renders two controls for the same action;
//   * the five outcomes are still pairwise distinct on four independent
//     channels, which is the property the whole screen exists to have.
//
// Run with: node tests/test_archive_outcome_prose.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures', 'archive');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name - Test description.
 * @param {() => void} fn - Body; throwing marks it failed.
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
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
 * @param {string} name - Basename without .json under tests/fixtures/archive.
 * @returns {object} The parsed envelope.
 */
function fixture(name) {
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

/**
 * Load the classifier and the view into one sandbox sharing a MiniDocument.
 * @returns {{view: object, document: object}} The view module and its document.
 */
function loadView() {
    const env = createEnvironment();
    const context = {
        window: { document: env.document },
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-outcome-view.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file });
    }
    return { view: context.window.ArchiveOutcomeView, document: env.document };
}

const { view, document } = loadView();

/**
 * Render one envelope into a detached outcome block.
 * @param {object} envelope - A captured archive response.
 * @param {object} [opts] - Extra renderOutcomeBlock options.
 * @returns {object} The root MiniElement.
 */
function render(envelope, opts) {
    return view.renderOutcomeBlock(envelope, { document, ...(opts || {}) });
}

/**
 * Whitespace-collapsed visible text of a block.
 * @param {object} el - A MiniElement.
 * @returns {string} Collapsed textContent.
 */
function text(el) {
    return el.textContent.replace(/\s+/g, ' ').trim();
}

/**
 * The block's leading sentence: the headline paragraph, not the label.
 * @param {object} el - A MiniElement.
 * @returns {string} The headline text.
 */
function headline(el) {
    const h = el.querySelector('.archive-outcome__headline');
    return h ? h.textContent.replace(/\s+/g, ' ').trim() : '';
}

const PAYLOADS = {
    ok: fixture('ok_search_hits'),
    empty: fixture('ok_empty_search'),
    partial: fixture('partial_search'),
    cannotDetermine: fixture('cannot_cursor'),
    notFound: fixture('not_found_transcript'),
};

// ---- POSITIVE CONTROL --------------------------------------------------

test('POSITIVE CONTROL: the renderer produces blocks with headlines', () => {
    for (const [name, p] of Object.entries(PAYLOADS)) {
        assert.ok(headline(render(p)).length > 10,
            `${name} rendered no headline; every assertion below is vacuous`);
    }
});

// ---- 1. ONE SENTENCE, AND IT NAMES AN ACTION ---------------------------

test('each non-ok headline is ONE sentence', () => {
    for (const name of ['empty', 'partial', 'cannotDetermine', 'notFound']) {
        const h = headline(render(PAYLOADS[name]));
        // Count sentence terminators that are followed by a space and a
        // capital - a decimal point or an abbreviation is not a sentence
        // break. Exactly one terminator, at the very end.
        const mid = h.slice(0, -1).match(/[.?!]\s+[A-Z]/g) || [];
        assert.deepEqual(mid, [],
            `the ${name} headline is ${mid.length + 1} sentences, not one: "${h}"`);
        assert.ok(/[.]$/.test(h), `the ${name} headline does not end in a period`);
    }
});

test('each non-ok headline LEADS with its own primary action', () => {
    // The lead phrase must be the label of the block's first default
    // action, so the sentence tells you what to do before it tells you
    // what happened. Read from DEFAULT_ACTIONS rather than hardcoded, so
    // changing an action label cannot leave the prose pointing at a
    // control that no longer exists.
    const cases = {
        empty: 'broaden-scope', partial: 'resume',
        cannotDetermine: 'retry', notFound: 'go-up',
    };
    const tokenOf = { empty: 'empty', partial: 'partial',
                      cannotDetermine: 'cannot-determine', notFound: 'not-found' };
    for (const [name, action] of Object.entries(cases)) {
        const defs = view.DEFAULT_ACTIONS[tokenOf[name]];
        const def = defs.find((d) => d.action === action);
        assert.ok(def, `${name} no longer offers the ${action} action`);
        const h = headline(render(PAYLOADS[name]));
        assert.ok(h.toLowerCase().startsWith(def.label.toLowerCase()),
            `the ${name} headline does not lead with its action "${def.label}": "${h}"`);
    }
});

// ---- 2. NOTHING WAS DELETED --------------------------------------------
// The whole risk of a prose trim. Every number the block used to shout
// must still be reachable - it moved behind a disclosure, which is still
// textContent, still Ctrl-F-able and still read by a screen reader.

test('the partial block still carries every scan number, verbatim', () => {
    const t = text(render(PAYLOADS.partial));
    const scan = PAYLOADS.partial.meta.scan;
    // The fields the renderer surfaces, named explicitly. Asserting over
    // EVERY numeric field in meta.scan was tried first and is wrong: it
    // demands budget_transcripts and budget_bytes, which this block has
    // never rendered and MUST NOT - the sibling suite asserts that
    // bytes_scanned is never shown as a fraction of its own budget,
    // because it was measured 2.75% OVER that budget and is therefore a
    // charge, not a metered consumption. A test that demands a field the
    // code is required not to render is a false regression report.
    for (const field of ['transcripts_scanned', 'transcripts_not_scanned',
                         'bytes_scanned']) {
        assert.ok(typeof scan[field] === 'number',
            `fixture drift: meta.scan.${field} is no longer a number`);
        assert.ok(t.includes(String(scan[field])),
            `meta.scan.${field} (${scan[field]}) was lost in the trim`);
    }
    assert.ok(t.includes(String(PAYLOADS.partial.meta.scope.transcripts_in_scope)),
        'the in-scope total was lost in the trim');
    for (const [subject] of PAYLOADS.partial.unevaluated.map((u) => [u.subject])) {
        assert.ok(t.includes(String(subject)),
            `the server's own subject "${subject}" was lost in the trim`);
    }
});

test('the byte charge is still stated, and still labelled a charge', () => {
    // It is internal billing accounting and does not belong in the lead,
    // but deleting it would remove the one line that stops the number
    // being mistaken for progress. Measured: bytes_scanned reported 2.75%
    // OVER its own budget, so it cannot be a metered consumption.
    const el = render(PAYLOADS.partial);
    const t = text(el);
    assert.ok(t.includes('charge'), 'the byte charge lost its "charge" wording');
    assert.ok(!headline(el).includes('charge'),
        'the byte charge is back in the lead sentence, where it reads as ' +
        'progress to anyone who does not already know it is billing');
});

test('the audit trail is a real disclosure holding the moved content', () => {
    for (const name of ['partial', 'cannotDetermine', 'notFound', 'ok', 'empty']) {
        const el = render(PAYLOADS[name]);
        const audit = el.querySelector('.archive-outcome__audit');
        if (!audit) continue;   // nothing to disclose is a valid state
        assert.equal(audit.tagName.toLowerCase(), 'details',
            'the audit trail is not a native disclosure, so it cannot be opened');
        const summary = audit.querySelector('summary');
        assert.ok(summary, `${name}'s audit trail has no summary to click`);
        // It NAMES its contents. "Details" tells nobody whether it is
        // worth opening.
        assert.ok(/coverage|reason|count/i.test(summary.textContent),
            `${name}'s audit summary does not say what is inside: ` +
            `"${summary.textContent}"`);
        assert.ok(audit.childNodes.length > 1,
            `${name} renders an EMPTY disclosure - a control that does nothing`);
    }
});

test('the reasons and coverage live INSIDE the disclosure, not beside it', () => {
    const el = render(PAYLOADS.partial);
    const audit = el.querySelector('.archive-outcome__audit');
    assert.ok(audit, 'the partial block has no audit disclosure');
    assert.ok(audit.querySelector('.archive-outcome__coverage'),
        'coverage did not move behind the disclosure');
    assert.ok(audit.querySelector('.archive-outcome__reasons'),
        'the reason list did not move behind the disclosure');
});

// ---- 3. NEVER TWO CONTROLS FOR ONE ACTION ------------------------------

test('no outcome renders the same action twice', () => {
    for (const [name, p] of Object.entries(PAYLOADS)) {
        const actions = render(p).querySelectorAll('[data-action]')
            .map((b) => b.getAttribute('data-action'));
        const dupes = actions.filter((a, i) => actions.indexOf(a) !== i);
        assert.deepEqual(dupes, [],
            `${name} renders duplicate controls for: ${dupes.join(', ')}`);
    }
});

test('omitActions removes exactly the named action and nothing else', () => {
    // This is the mechanism by which archive-search.js stops the partial
    // state painting "Resume the scan" twice - once here and once in its
    // own kind-aware panel a few hundred pixels below.
    const withAll = render(PAYLOADS.partial)
        .querySelectorAll('[data-action]').map((b) => b.getAttribute('data-action'));
    const without = render(PAYLOADS.partial, { omitActions: ['resume'] })
        .querySelectorAll('[data-action]').map((b) => b.getAttribute('data-action'));
    assert.ok(withAll.includes('resume'),
        'the partial block no longer offers resume at all, so this proves nothing');
    assert.ok(!without.includes('resume'), 'omitActions did not remove resume');
    assert.deepEqual(without, withAll.filter((a) => a !== 'resume'),
        'omitActions removed something it was not asked to remove');
});

test('archive-search.js actually passes omitActions', () => {
    // The option is useless unless the one caller with a competing
    // control uses it, and that call site is in a different file.
    // Comments are stripped FIRST. The call site carries a comment that
    // quotes the option verbatim, so a raw-source match stayed green
    // after the real option was deleted - proven by mutation. An
    // assertion about what code DOES has to read code, not prose.
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'archive-search.js'), 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
    assert.match(src, /omitActions:\s*\['resume'\]/,
        'the search view still lets the generic block draw a second, ' +
        'identical "Resume the scan" button beside its own kind-aware one');
});

// ---- 4. THE MODEL IS UNCHANGED -----------------------------------------
// The trim must not have collapsed any two outcomes into each other.
// Four channels independent by construction: words, classes, an
// attribute, and which controls exist. Colour is not a channel and
// neither is border-radius - three of the 23 themes zero every radius
// token, so a meaning carried by a rounded corner is one those themes
// cannot express.

test('the five outcomes are STILL pairwise distinct on all four channels', () => {
    const channels = {
        text: (el) => text(el),
        classes: (el) => el.className.split(/\s+/).filter(Boolean).sort().join(' '),
        dataOutcome: (el) => el.getAttribute('data-outcome'),
        actions: (el) => el.querySelectorAll('[data-action]')
            .map((b) => b.getAttribute('data-action')).sort().join(','),
    };
    const rendered = {};
    for (const [name, p] of Object.entries(PAYLOADS)) rendered[name] = render(p);
    for (const [chName, extract] of Object.entries(channels)) {
        const seen = new Map();
        for (const [name, el] of Object.entries(rendered)) {
            const v = extract(el);
            assert.ok(String(v).length > 0,
                `channel ${chName} produced nothing for ${name}`);
            assert.ok(!seen.has(v),
                `the prose trim made "${name}" and "${seen.get(v)}" identical on ` +
                `channel "${chName}" (${v})`);
            seen.set(v, name);
        }
    }
});

test('the headlines themselves are pairwise distinct', () => {
    // Assertion above reads the whole block, so a shared headline could
    // hide behind a differing reason list. The LEAD is what people read.
    const seen = new Map();
    for (const [name, p] of Object.entries(PAYLOADS)) {
        const h = headline(render(p));
        assert.ok(!seen.has(h),
            `"${name}" and "${seen.get(h)}" now open with the same sentence`);
        seen.set(h, name);
    }
});

test('"no matches" still belongs to the empty outcome and to nothing else', () => {
    const emptyText = text(render(PAYLOADS.empty)).toLowerCase();
    assert.ok(emptyText.includes('no matches'),
        'the empty outcome stopped saying "no matches" during the trim');
    for (const name of ['ok', 'partial', 'cannotDetermine', 'notFound']) {
        assert.ok(!text(render(PAYLOADS[name])).toLowerCase().includes('no matches'),
            `${name} now claims "no matches", which belongs only to a ` +
            'completed search that found nothing');
    }
});

test('the empty outcome says "no matches" ONCE in its lead, not four times', () => {
    // It used to say it in the label, the headline, the coverage and the
    // has-more line. The label is the one that must keep saying it,
    // because the label is the outcome's name.
    const el = render(PAYLOADS.empty);
    const lead = (el.querySelector('.archive-outcome__label').textContent + ' ' +
                  headline(el)).toLowerCase();
    const hits = (lead.match(/no matches|none matched|nothing (was )?found/g) || []).length;
    assert.ok(hits <= 2,
        `the lead states "nothing was found" ${hits} times before the reader ` +
        `reaches anything new: "${lead}"`);
    assert.ok(hits >= 1, 'the lead never states that nothing was found');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
