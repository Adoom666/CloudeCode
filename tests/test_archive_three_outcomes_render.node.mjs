// THE MOST IMPORTANT TEST IN THE ARCHIVE UI.
//
// The archive server pays real cost to distinguish "I looked and found
// nothing" from "I could not look" from "I ran out of budget partway".
// That property is worth exactly nothing unless it survives to the
// pixel, and the renderer is the last place it can be lost - there is no
// outer check after it. A blank region rendered for a cannot_determine
// is a false green generated inside the UI itself.
//
// EVERY PAYLOAD HERE IS A REAL CAPTURED RESPONSE, not a hand-written
// approximation. tests/fixtures/archive/*.json were fetched from the
// live dev server at 127.0.0.1:5055 on 2026-08-31 and are stored
// verbatim:
//
//   ok_hosts.json            GET /archive/hosts
//   ok_search_hits.json      GET /archive/search?q=restic&project_id=12&limit=3
//   ok_empty_search.json     GET /archive/search?q=zzqqxyznotfoundatall&transcript_id=4
//   partial_search.json      GET /archive/search?q=zzzqqqxyznotfound&project_id=12
//   cannot_cursor.json       GET /archive/projects/12/transcripts?cursor=@@@notbase64@@@
//   not_found_transcript.json GET /archive/transcripts/99999
//
// THE POSITIVE CONTROL IS NOT OPTIONAL. An "everything renders
// differently" assertion passes trivially for a renderer that stamps a
// nonce, a timestamp or a request sequence number into every block. It
// would then be green while proving nothing - which is the exact defect
// class this whole screen exists to prevent, sitting inside the test
// that exists to prevent it. Assertion 2 asserts the CONVERSE: two
// payloads with the same outcome must render the SAME on every channel
// that is not the verbatim reason text.
//
// Run with: node tests/test_archive_three_outcomes_render.node.mjs

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
 * Load archive-outcome.js and archive-outcome-view.js into one vm
 * sandbox sharing a MiniDocument, and return the view module.
 *
 * Both modules go into the SAME context because the view calls
 * window.ArchiveOutcome.classify - loading them separately would leave
 * the view reaching into an undefined global and failing for a reason
 * that has nothing to do with what is under test.
 *
 * @returns {{view: object, document: object}} The view module and its document.
 */
function loadView() {
    const env = createEnvironment();
    const fakeWindow = { document: env.document };
    const context = {
        window: fakeWindow,
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-outcome-view.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { view: context.window.ArchiveOutcomeView, document: env.document };
}

const { view, document } = loadView();

/**
 * Render one envelope into a detached outcome block.
 * @param {object} envelope - A captured archive response.
 * @returns {object} The root MiniElement.
 */
function render(envelope) {
    return view.renderOutcomeBlock(envelope, { document });
}

// ---- CHANNEL EXTRACTORS -------------------------------------------------
// Four channels independent BY CONSTRUCTION: one reads visible words, one
// reads a class list, one reads an attribute, one reads which buttons
// exist. A renderer cannot satisfy all four by accident. Colour is NOT a
// channel, and neither is border-radius: three of this app's 23 themes
// deliberately zero every radius token, so a meaning carried by a
// rounded corner is a meaning those themes cannot express.
const channels = {
    // 1. TEXT. What a person actually reads. Whitespace collapsed so
    //    formatting churn is not a false difference.
    text: (el) => el.textContent.replace(/\s+/g, ' ').trim(),

    // 2. CLASS. The styling hook.
    classes: (el) => el.className.split(/\s+/).filter(Boolean).sort().join(' '),

    // 3. DATA ATTRIBUTE. The machine-readable outcome token.
    dataOutcome: (el) => el.getAttribute('data-outcome'),

    // 4. ACTION PRESENCE. Which affordances exist. Hardest to fake,
    //    because it is a structural fact about the subtree rather than a
    //    string the renderer chose.
    actions: (el) => el.querySelectorAll('[data-action]')
                       .map(b => b.getAttribute('data-action'))
                       .sort().join(','),
};

const PAYLOADS = {
    ok: fixture('ok_search_hits'),
    empty: fixture('ok_empty_search'),
    partial: fixture('partial_search'),
    cannotDetermine: fixture('cannot_cursor'),
    notFound: fixture('not_found_transcript'),
};

// ---- ASSERTION 0: THE FIXTURES ARE WHAT THIS TEST CLAIMS ---------------
// A fixture that silently drifted to a different result_status would
// make every assertion below pass while testing something else.

test('fixtures carry the result_status each one is named for', () => {
    assert.equal(PAYLOADS.ok.result_status, 'ok');
    assert.ok(PAYLOADS.ok.result.length > 0, 'the ok fixture must carry rows');
    assert.equal(PAYLOADS.empty.result_status, 'ok');
    assert.equal(PAYLOADS.empty.result.length, 0);
    assert.equal(PAYLOADS.partial.result_status, 'partial');
    assert.equal(PAYLOADS.cannotDetermine.result_status, 'cannot_determine');
    assert.equal(PAYLOADS.notFound.result_status, 'not_found');
});

// ---- ASSERTION 1: FIVE OUTCOMES DIFFER ON EVERY CHANNEL ----------------
// For each channel, all five rendered outcomes must be pairwise
// distinct. Not "at least one channel differs" - EVERY channel. A single
// shared channel is a path by which two outcomes look alike to someone.

test('five outcomes are pairwise distinct on all four channels', () => {
    const rendered = {};
    for (const [name, payload] of Object.entries(PAYLOADS)) {
        rendered[name] = render(payload);
    }
    for (const [chName, extract] of Object.entries(channels)) {
        const seen = new Map();
        for (const [name, el] of Object.entries(rendered)) {
            const v = extract(el);
            assert.ok(v !== null && v !== undefined && String(v).length > 0,
                `channel ${chName} produced nothing for outcome ${name}`);
            if (seen.has(v)) {
                assert.fail(
                    `outcome "${name}" and outcome "${seen.get(v)}" render ` +
                    `IDENTICALLY on channel "${chName}" (value: ${v}). ` +
                    `A person cannot tell them apart.`);
            }
            seen.set(v, name);
        }
    }
});

// ---- ASSERTION 2: THE POSITIVE CONTROL ---------------------------------
// Two DIFFERENT payloads with the SAME outcome must render the SAME on
// the class, attribute and action channels. Without this, assertion 1
// passes for a renderer that makes every block unique.

test('POSITIVE CONTROL: two different ok payloads render the same shape', () => {
    // Two genuinely different live responses: a hosts listing (2 rows,
    // meta.totals, no scan block at all) and a search hit page (3 rows,
    // meta.scan, meta.scope, meta.snippet_gate).
    const a = render(fixture('ok_hosts'));
    const b = render(fixture('ok_search_hits'));
    for (const ch of ['classes', 'dataOutcome', 'actions']) {
        assert.equal(channels[ch](a), channels[ch](b),
            `two ok payloads render DIFFERENTLY on channel "${ch}". ` +
            `Assertion 1 is therefore passing because every render is ` +
            `unique, not because the outcomes are distinguished.`);
    }
});

test('POSITIVE CONTROL: two cannot_determine payloads render the same shape', () => {
    const a = render(PAYLOADS.cannotDetermine);
    const b = render({
        result: null, result_status: 'cannot_determine', scope_status: 'resolved',
        unevaluated: [{ subject: 'datastore', reason: 'the archive database would not open' }],
        meta: {}
    });
    for (const ch of ['classes', 'dataOutcome', 'actions']) {
        assert.equal(channels[ch](a), channels[ch](b),
            `two cannot_determine payloads render DIFFERENTLY on channel "${ch}"`);
    }
    // And they DO differ on text, because the reason is rendered verbatim.
    assert.notEqual(channels.text(a), channels.text(b),
        'the unevaluated reason is not reaching the rendered output');
});

// ---- ASSERTION 3: "NO MATCHES" BELONGS TO EXACTLY ONE OUTCOME ----------
// The one collapse that matters most, asserted in both directions.

test('the empty-result wording appears for empty and for NOTHING else', () => {
    const emptyText = channels.text(render(PAYLOADS.empty));
    assert.ok(/no matches/i.test(emptyText),
        'the empty result does not say "no matches" at all');
    assert.ok(!/^no matches\.?$/i.test(emptyText),
        'the empty result is a bare "No matches." and never names the scope it searched');

    for (const name of ['ok', 'partial', 'cannotDetermine', 'notFound']) {
        const t = channels.text(render(PAYLOADS[name]));
        assert.ok(!/no matches/i.test(t),
            `${name} renders the words "no matches", which belong only to a completed empty search`);
        assert.notEqual(t, emptyText, `${name} renders the same words as an empty result`);
    }
});

// ---- ASSERTION 4: EVERY UNMEASURED OUTCOME NAMES WHAT WENT UNMEASURED --
// A blank cell is not an answer. The server's own subject and reason
// must reach the screen verbatim.

test('cannot_determine and not_found render the server subject and reason verbatim', () => {
    const cd = channels.text(render(PAYLOADS.cannotDetermine));
    assert.ok(cd.includes(PAYLOADS.cannotDetermine.unevaluated[0].subject),
        'the cannot_determine subject is missing from the rendered block');
    assert.ok(cd.includes('did not decode as base64url'),
        'the cannot_determine reason is missing from the rendered block');

    const nf = channels.text(render(PAYLOADS.notFound));
    assert.ok(nf.includes('transcript:99999'), 'the not_found subject is missing');
    assert.ok(nf.includes('no row in message_transcripts with id 99999'),
        'the not_found reason is missing');
});

test('an unmeasured outcome carrying NO reasons still names the gap', () => {
    const el = render({
        result: null, result_status: 'cannot_determine',
        scope_status: 'resolved', unevaluated: [], meta: {}
    });
    const t = channels.text(el);
    assert.ok(/NOT KNOWN/.test(t),
        'a cannot_determine with an empty unevaluated[] renders no statement about the gap, ' +
        'which is a blank cell wearing a label');
});

// ---- ASSERTION 5: PARTIAL SHOWS UNFINISHED WORK AND OFFERS RESUME ------

test('partial states how much was NOT scanned and offers a resume affordance', () => {
    const el = render(PAYLOADS.partial);
    const t = channels.text(el);
    const scan = PAYLOADS.partial.meta.scan;
    assert.ok(t.includes(String(scan.transcripts_not_scanned)),
        `partial does not say that ${scan.transcripts_not_scanned} transcripts were never read`);
    assert.ok(t.includes(String(scan.transcripts_scanned)),
        'partial does not say how many transcripts WERE read');
    const resume = el.querySelector('[data-action="resume"]');
    assert.ok(resume, 'partial offers no resume control');
    assert.ok(!resume.hasAttribute('disabled'),
        'the resume control is disabled even though this response carries a resume_cursor');
});

test('a partial with no resume_cursor still shows resume, DISABLED and with a reason', () => {
    // The actions channel is what keeps the outcomes structurally
    // distinct, so the control is always present. A control that
    // silently fails is worse than a stated blocker, so it says why.
    const el = render({
        result: [], result_status: 'partial', scope_status: 'resolved',
        unevaluated: [{ subject: 'project:12', reason: 'the scan stopped' }],
        meta: { scan: { transcripts_scanned: 1, transcripts_not_scanned: 9,
                        resume_cursor: null },
                scope: { kind: 'project', project_id: 12, transcripts_in_scope: 10 } }
    });
    const resume = el.querySelector('[data-action="resume"]');
    assert.ok(resume, 'the resume control vanished, taking the actions channel with it');
    assert.ok(resume.hasAttribute('disabled'), 'an unresumable scan offers a live resume button');
    assert.ok(String(resume.getAttribute('data-blocked-reason')).includes('resume_cursor'),
        'the disabled resume control does not say why it is disabled');
});

// ---- ASSERTION 6: PROGRESS IS NEVER A BYTE FRACTION --------------------
// Measured: bytes_scanned reported 551,648,566 against a budget_bytes of
// 536,870,912, 2.75% OVER its own budget, and 91,950,363 bytes in
// 0.0756 s. A quantity that exceeds its own budget is a charge, not a
// metered consumption, and cannot be a numerator or a denominator.

test('bytes_scanned is labelled a charge and is never rendered as a fraction', () => {
    const t = channels.text(render(PAYLOADS.partial));
    const scan = PAYLOADS.partial.meta.scan;
    assert.ok(t.includes('charge'),
        'bytes_scanned is rendered without saying it is a charge');
    assert.ok(!t.includes(`${scan.bytes_scanned} of ${scan.budget_bytes}`),
        'bytes_scanned is rendered as a fraction of its own budget');
    assert.ok(!new RegExp(`${scan.bytes_scanned}\\s*/`).test(t),
        'bytes_scanned appears as the numerator of a ratio');
});

// ---- ASSERTION 7: AN `ok` DOES NOT IMPLY A COMPLETE SEARCH -------------
// Measured 2026-08-31: the ok_search_hits fixture is result_status "ok"
// with scan.status "limit_reached" and 3,415 of 3,416 transcripts never
// read. An ok block that hides that claims a search nobody ran.

test('an ok search states its coverage rather than implying completeness', () => {
    const scan = PAYLOADS.ok.meta.scan;
    assert.ok(scan.transcripts_not_scanned > 0,
        'fixture drift: this ok response no longer has unscanned transcripts, ' +
        'so it can no longer prove what this test is for');
    const t = channels.text(render(PAYLOADS.ok));
    assert.ok(t.includes(String(scan.transcripts_not_scanned)),
        `an ok response that never read ${scan.transcripts_not_scanned} transcripts ` +
        'renders without saying so');
});

// ---- ASSERTION 8: NO HTML INJECTION VIA A SERVER STRING ----------------
// Reasons are rendered verbatim by requirement, and host display names
// carry real non-ASCII. Both must reach the DOM as text.

test('a reason containing markup is text, not markup', () => {
    const el = render({
        result: null, result_status: 'cannot_determine', scope_status: 'resolved',
        unevaluated: [{ subject: '<img src=x onerror=1>',
                        reason: 'Joseph’s Mac mini (2) <b>bold</b>' }],
        meta: {}
    });
    assert.ok(channels.text(el).includes('<b>bold</b>'),
        'the markup-bearing reason did not survive as literal text');
    assert.equal(el.querySelectorAll('b').length, 0,
        'a server-supplied string produced a real element - this is innerHTML somewhere');
    assert.equal(el.querySelectorAll('img').length, 0,
        'a server-supplied subject produced a real element');
    assert.ok(channels.text(el).includes('Joseph’s'),
        'the U+2019 in a real host display name did not survive rendering');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
