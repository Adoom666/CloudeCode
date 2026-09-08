// The "no project" node must read as ANSWERED, not as an open question.
//
// It used to render the bare phrase 'transcripts with no project' plus a
// count of 5, in the warning colour, which is indistinguishable from an
// attribution failure somebody ought to chase. Those 5 are understood:
// their source path has no `.claude/projects` layer, so the slug deriver
// correctly returned "none declared". That is documented, tested
// behaviour.
//
// Two things are pinned here and they pull against each other, which is
// the point. The label must SAY what these are, and it must not assert
// "audit logs" as a permanent fact about the category - today all five
// are, tomorrow's corpus may hold something else, and a label that
// asserted it would be lying while looking authoritative. So the
// characterisation is DERIVED from the show/hide verdict, and the
// dated observation lives in the tooltip where it can be read as the
// dated observation it is.
//
// The show/hide rule itself is asserted UNCHANGED, because this change
// is about words and colour and must not touch behaviour.

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
 * Run one named assertion block.
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
 * Load archive-nav-row.js alone into a vm context.
 * @returns {object} {row, document}
 */
function load() {
    const env = createEnvironment();
    const document = env.document;
    const fakeWindow = { document };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-nav-row.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file });
    }
    return { row: context.window.ArchiveNavRow, document };
}

const M = load();
const KIND = 'unattributed';

/** @param {object} row @returns {Element} the rendered node */
function renderNode(row) {
    return M.row.renderRow(M.document, KIND, row, { onActivate() {} });
}

// --- the label says what these ARE -----------------------------------

test('the label names the structural fact, not just the absence', () => {
    const label = M.row.labelFor(KIND, { unattributed_transcript_count: 5 });
    assert.equal(label, 'transcripts with no project layer');
    assert.ok(label.includes('no project'),
        'the phrase the rest of the app uses must survive');
});

test('the label does NOT assert "audit logs" about the category', () => {
    // A different kind of project-less transcript would make this a lie,
    // and the label is the one string a reader cannot avoid.
    const label = M.row.labelFor(KIND, { unattributed_transcript_count: 5 });
    assert.ok(!/audit/i.test(label), `label must not claim audit: ${label}`);
    assert.ok(!/\b5\b/.test(label),
        'the label must not bake in a count that will move');
});

test('the note explains it as ANSWERED when the count is measured', () => {
    const note = M.row.unattributedNote({ unattributed_transcript_count: 5,
                                          counted: true });
    assert.equal(note.answered, true);
    assert.ok(note.text.includes('belongs to no project'),
        'the established phrase must survive for the callers asserting it');
    assert.ok(/complete answer|not a failed attribution/.test(note.text),
        `the note must read as answered: ${note.text}`);
    // Pinned in BOTH branches: an older suite asserts this exact phrase,
    // and it is the sentence that says why the node is here at all.
    assert.ok(note.text.includes('invisible from the project tree'), note.text);
    assert.ok(!/audit/i.test(note.text),
        'the note must not assert audit logs either');
});

test('an UNMEASURED count is NOT dressed up as answered', () => {
    // The node is shown for three different reasons and only one of them
    // is settled. Calling the other two "a complete answer" would be the
    // exact false green this rail exists to avoid.
    for (const row of [{ counted: false }, {}, { unattributed_transcript_count: 'x' }]) {
        const note = M.row.unattributedNote(row);
        assert.equal(note.answered, false, JSON.stringify(row));
        assert.ok(/not established/.test(note.text), note.text);
        assert.ok(note.text.includes('invisible from the project tree'), note.text);
        assert.ok(!/complete answer/.test(note.text), note.text);
    }
});

test('the dated measurement lives in the TOOLTIP, dated', () => {
    const tip = M.row.titleFor(KIND, { unattributed_transcript_count: 5 });
    assert.ok(/audit logs/.test(tip), 'the tooltip may characterise today');
    assert.ok(/Measured 2026-09-02/.test(tip),
        'and it must carry the date that makes it honest');
    assert.ok(/\.claude\/projects/.test(tip),
        'the tooltip must name the structural cause');
});

// --- what actually renders --------------------------------------------

test('the rendered node carries the label, the note and the answered tone', () => {
    const li = renderNode({ unattributed_transcript_count: 5, counted: true });
    assert.equal(li.querySelector('.archive-nav__label').textContent,
        'transcripts with no project layer');
    const note = li.querySelector('.archive-nav__note');
    assert.ok(note, 'the note must render');
    assert.ok(note.textContent.includes('belongs to no project'));
    assert.ok(note.classList.contains('archive-nav__note--answered'),
        'a settled bucket must not wear the warning class alone');
});

test('an unmeasured node renders WITHOUT the answered tone', () => {
    const li = renderNode({ counted: false });
    const note = li.querySelector('.archive-nav__note');
    assert.ok(!note.classList.contains('archive-nav__note--answered'),
        'an unestablished count must keep the warning tone');
});

// --- the rule is untouched --------------------------------------------

test('shouldShowUnattributed is EXACTLY as it was', () => {
    assert.equal(M.row.shouldShowUnattributed({ unattributed_transcript_count: 0 }).show,
        false, 'hidden ONLY on a known zero');
    assert.equal(M.row.shouldShowUnattributed({ unattributed_transcript_count: 5 }).show,
        true);
    assert.equal(M.row.shouldShowUnattributed({}).show, true);
    assert.equal(M.row.shouldShowUnattributed({ counted: false }).show, true);
    assert.equal(
        M.row.shouldShowUnattributed({ unattributed_transcript_count: 0, counted: false }).show,
        true, 'an uncounted zero is NOT a known zero');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
