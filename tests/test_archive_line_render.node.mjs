// One archive line to DOM: the secret rule, the NULL-role fallback, and
// the progress fold.
//
// THE SECRET RULE IS THE REASON THIS FILE EXISTS. Bodies arrive from the
// server whole and unredacted BY DESIGN - byte-exactness is the point of
// the archive, and a redacted body would not be the bytes that were on
// disk. Masking is therefore the client's job, and the renderer is the
// last place it can go wrong, because there is no outer check after the
// pixel. Two failures are asserted against here, and they fail in
// opposite directions:
//
//   1. Rendering a body that has findings but was never masked. That is
//      a credential on screen.
//   2. Rendering a body that the masker REFUSED. A refusal means the
//      credential's position is unknown, so there is no safe rendering
//      and no truncation that helps - the whole body stays off screen.
//
// A HALF-MASKED BODY DOES NOT LOOK LIKE A FAILURE. It looks like a
// success: a run of marker text with a short hex tail that reads like
// surrounding prose. Nobody reports it. That is why the assertion is
// that the credential substring is ABSENT from textContent, and why
// mini-dom's aggregate textContent is what makes it meaningful - a
// per-element check would miss a credential split across text nodes.
//
// NO REAL CREDENTIAL APPEARS IN THIS FILE. The synthetic one is
// uppercase and digits only.
//
// TRAP AVOIDED: deepStrictEqual compares prototypes across vm realms.
//
// Run with: node tests/test_archive_line_render.node.mjs

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
 * Run one named async assertion block, recording pass/fail.
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
 * Load the archive client modules into one vm sandbox sharing a window.
 * @param {object} doc - a MiniDocument.
 * @returns {object} the shared fake window
 */
function loadModules(doc) {
    const fakeWindow = { document: doc };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        setTimeout, clearTimeout,
    };
    context.globalThis = context;
    vm.createContext(context);
    for (const f of ['archive-outcome.js', 'archive-mask.js', 'archive-format.js',
        'archive-outcome-view.js', 'archive-body-gate.js', 'archive-body-cache.js',
        'archive-line-render.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/** Synthetic credential. Uppercase and digits only, never a real secret. */
const SECRET = 'AKIA7Q2W9E4R6T8Y0U1I3O5P7A9S1D3F5G7H9J1K';
/** A body with the synthetic credential at UTF-16 offset 24. */
const SECRET_BODY = '{"key":"prefix-padding--' + SECRET + '","tail":"after"}';
/** Where the credential sits, in UTF-16 code units. */
const SECRET_OFFSET = SECRET_BODY.indexOf(SECRET);

/**
 * A fake api. Bodies are supplied by id; an unlisted id throws so a
 * stray fetch fails loudly rather than resolving to undefined.
 * @param {object} bodies - id -> body payload
 * @returns {object} {api, asked}
 */
function fakeApi(bodies) {
    const asked = [];
    return {
        asked,
        api: {
            /** @param {number} id @returns {Promise<object>} envelope */
            async getArchiveBody(id) {
                asked.push(id);
                if (!(id in bodies)) throw new Error(`unexpected fetch of body ${id}`);
                return { result: bodies[id], result_status: 'ok',
                    scope_status: 'resolved', unevaluated: [], meta: {} };
            },
        },
    };
}

/**
 * Assert the synthetic credential appears nowhere in a rendered subtree,
 * and that no fragment of it long enough to be useful survives either.
 * The 4-character tail check is the specific bug measured on real body
 * 379: code-point offsets in JavaScript slide the mask window four units
 * left and leave the LAST FOUR characters of a 40-character credential
 * on screen.
 * @param {object} el - a MiniElement
 * @returns {void}
 */
function assertNoCredential(el) {
    const t = el.textContent;
    assert.ok(!t.includes(SECRET), 'the whole credential reached the DOM');
    const tail = SECRET.slice(-4);
    assert.ok(!t.includes(tail),
        `the last ${tail.length} characters of the credential reached the DOM`);
    const head = SECRET.slice(0, 8);
    assert.ok(!t.includes(head), 'the leading run of the credential reached the DOM');
}

await test('POSITIVE CONTROL: an ordinary body IS rendered, so absence assertions mean something', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi({
        1: { body_json: 'HELLOFROMTHEARCHIVE', secret_finding_count: 0, secrets: [] },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });
    const row = { line_no: 1, record_type: 'assistant', role: 'assistant',
        body_id: 1, body_chars: 19 };
    const e = await cache.request(row);
    const el = w.ArchiveLineRender.renderLine(env.document, row, e, {});
    assert.ok(el.textContent.includes('HELLOFROMTHEARCHIVE'),
        'the renderer must actually render bodies');
    assert.equal(el.querySelectorAll('pre').length, 1);
});

await test('a body with findings is masked, and the credential is nowhere in the DOM', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi({
        2: {
            body_json: SECRET_BODY,
            secret_finding_count: 1,
            secrets: [{ utf16_state: 'computed',
                match_offset_utf16: SECRET_OFFSET,
                match_length_utf16: SECRET.length,
                // The code-point fields are present and DELIBERATELY
                // different, so a renderer that read them would slide
                // the window and leave a visible tail.
                match_offset: SECRET_OFFSET - 4,
                match_length: SECRET.length }],
        },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });
    const row = { line_no: 292, record_type: 'user', role: 'user',
        body_id: 2, body_chars: SECRET_BODY.length, secret_finding_count: 1 };
    const e = await cache.request(row);
    assert.equal(e.state, w.ArchiveBodyCache.STATE_OK);
    assert.equal(e.masked, 1);

    const el = w.ArchiveLineRender.renderLine(env.document, row, e, {});
    assertNoCredential(el);
    assert.ok(el.textContent.includes(w.ArchiveMask.SECRET_MARKER),
        'the marker must be visible where the credential was');
    // The reader is told a lens was applied. Without this the marker
    // reads as stored content, and the archive is byte-exact on disk.
    const note = el.querySelector('[data-masked-count]');
    assert.ok(note, 'a masked body must carry a masked-count note');
    assert.equal(note.getAttribute('data-masked-count'), '1');
});

await test('a mask REFUSAL renders the refusal, not the body', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The measured /lines gap shape: findings declared, no offsets. Also
    // the utf16_state=cannot_determine shape. Both mean the credential's
    // position is unknown, and there is no partial masking that is safe.
    const cases = [
        { name: 'no findings array',
          payload: { body_json: SECRET_BODY, secret_finding_count: 3 } },
        { name: 'utf16_state cannot_determine',
          payload: { body_json: SECRET_BODY, secret_finding_count: 1,
              secrets: [{ utf16_state: 'cannot_determine',
                  match_offset: SECRET_OFFSET, match_length: SECRET.length }] } },
        { name: 'fewer findings than declared',
          payload: { body_json: SECRET_BODY, secret_finding_count: 2,
              secrets: [{ utf16_state: 'computed',
                  match_offset_utf16: SECRET_OFFSET,
                  match_length_utf16: SECRET.length }] } },
        { name: 'window runs past the end of the string',
          payload: { body_json: SECRET_BODY, secret_finding_count: 1,
              secrets: [{ utf16_state: 'computed',
                  match_offset_utf16: SECRET_BODY.length - 2,
                  match_length_utf16: 40 }] } },
    ];
    for (const c of cases) {
        const { api } = fakeApi({ 3: c.payload });
        const cache = w.ArchiveBodyCache.createCache({ api });
        const row = { line_no: 5, record_type: 'user', role: 'user',
            body_id: 3, body_chars: SECRET_BODY.length,
            secret_finding_count: c.payload.secret_finding_count };
        const e = await cache.request(row);
        assert.equal(e.state, w.ArchiveBodyCache.STATE_MASK_REFUSED,
            `${c.name}: expected a refusal, got ${e.state}`);
        assert.equal(e.text, null, `${c.name}: a refusal must carry null text`);
        // A refusal that returned '' would satisfy a loose falsy check
        // while `render(r.text || body)` would render the UNMASKED body.
        assert.notEqual(typeof e.text, 'string');
        assert.equal(e.chars, 0,
            `${c.name}: a refused body must charge nothing to the cache`);

        const el = w.ArchiveLineRender.renderLine(env.document, row, e, {});
        assertNoCredential(el);
        assert.equal(el.querySelectorAll('pre').length, 0,
            `${c.name}: a refused body must render no body element`);
        const box = el.querySelector('.archive-row__body');
        assert.equal(box.getAttribute('data-body-state'), 'mask-refused');
        assert.ok(el.textContent.includes('BODY WITHHELD BY THIS VIEW'),
            `${c.name}: the refusal must say so in words`);
        // The reason is rendered so the refusal is actionable rather
        // than a blank cell.
        assert.ok(el.textContent.includes('Reason:'), `${c.name}: no reason rendered`);
    }
});

await test('the renderer cannot render an unmasked secret even if handed one', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // A hand-built entry claiming `included` while carrying raw text is
    // the shape a future bug would take. The renderer renders
    // `entry.text` and nothing else, so this asserts the CONTRACT: there
    // is no path from body_json to the DOM that does not pass the cache.
    const row = { line_no: 9, record_type: 'user', role: 'user', body_id: 4,
        body_chars: SECRET_BODY.length, secret_finding_count: 1,
        body_json: SECRET_BODY, secrets: [] };
    const el = w.ArchiveLineRender.renderLine(env.document, row, null, {});
    assertNoCredential(el);
    assert.equal(el.querySelectorAll('pre').length, 0,
        'a not-requested row renders a placeholder, never body_json off the row');
    assert.equal(el.querySelector('.archive-row__body')
        .getAttribute('data-body-state'), 'not-requested');
});

await test('NULL role falls back to record_type, then to the literal string', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const LR = w.ArchiveLineRender;

    // role is NULL on 44.93% of bodies, measured 2026-08-31.
    const a = LR.roleLabel({ role: null, record_type: 'progress' });
    assert.equal(a.text, 'progress');
    assert.equal(a.source, 'record_type');

    const b = LR.roleLabel({ role: null, record_type: null });
    assert.equal(b.text, LR.NO_ROLE_TEXT);
    assert.equal(b.text, 'no role recorded');
    assert.equal(b.source, 'none');

    const c = LR.roleLabel({ role: 'assistant', record_type: 'assistant' });
    assert.equal(c.source, 'role');

    // An empty string is not a role. Rendering it would produce the blank
    // cell the fallback chain exists to prevent.
    assert.equal(LR.roleLabel({ role: '', record_type: '' }).source, 'none');

    const el = LR.renderLine(env.document,
        { line_no: 3, role: null, record_type: null, body_chars: 4 }, null, {});
    assert.equal(el.getAttribute('data-role-source'), 'none');
    assert.ok(el.textContent.includes('no role recorded'),
        'a roleless row must SAY so, never render a blank cell');
});

await test('a NULL ts renders the not-known token, never a blank', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // ts is NULL on 33,480 bodies, measured 2026-08-31.
    const el = w.ArchiveLineRender.renderLine(env.document,
        { line_no: 4, role: 'user', record_type: 'user', ts: null, body_chars: 3 },
        null, {});
    const ts = el.querySelector('.archive-row__ts');
    assert.ok(ts, 'the timestamp cell must exist even when ts is null');
    assert.ok(ts.textContent.length > 0, 'a null ts must not render as whitespace');
    assert.equal(ts.textContent, w.ArchiveFormat.NOT_KNOWN);
});

await test('a non-Claude model renders verbatim, angle brackets and all', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The 13 measured model values include nemotron-3-super and a
    // literal <synthetic>. Any "starts with claude-" assumption is wrong.
    for (const m of ['<synthetic>', 'nemotron-3-super', 'claude-opus-5']) {
        const el = w.ArchiveLineRender.renderLine(env.document,
            { line_no: 1, role: 'assistant', record_type: 'assistant',
                model: m, body_chars: 1 }, null, {});
        assert.equal(el.querySelector('.archive-row__model').textContent, m,
            `model ${m} was not rendered verbatim`);
    }
});

await test('progress runs collapse to one counted, expandable, never-hidden row', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const LR = w.ArchiveLineRender;

    const spine = [
        { line_no: 7109, record_type: 'assistant', role: 'assistant', body_chars: 10 },
    ];
    for (let i = 7110; i <= 7123; i++) {
        spine.push({ line_no: i, record_type: 'progress', role: null, body_chars: 5 });
    }
    spine.push({ line_no: 7124, record_type: 'assistant', role: 'assistant',
        body_chars: 10 });

    const items = LR.groupRows(spine);
    assert.equal(items.length, 3, 'the 14 progress rows must fold into one item');
    const run = items[1];
    assert.equal(run.kind, 'progress-run');
    assert.equal(run.count, 14);
    assert.equal(run.from, 7110);
    assert.equal(run.to, 7123);
    // NEVER hidden: every folded row is still carried on the item, so
    // expanding cannot lose one. A filter that silently removed 37% of a
    // byte-exact archive would be a client-side lie about the file.
    assert.equal(run.rows.length, 14);

    const collapsed = LR.renderProgressRun(env.document, run, { expanded: false });
    assert.equal(collapsed.getAttribute('data-progress-count'), '14');
    assert.equal(collapsed.getAttribute('data-expanded'), 'false');
    assert.ok(collapsed.textContent.includes('progress x 14'),
        'the chip must state the count');
    assert.ok(collapsed.textContent.includes('7110-7123'),
        'the chip must state the line range');
    assert.equal(collapsed.querySelectorAll('[data-action="expand-progress"]').length, 1,
        'the fold must be one action from being undone');
    assert.equal(collapsed.querySelectorAll('.archive-row__progress-children').length, 0);

    const open = LR.renderProgressRun(env.document, run, { expanded: true });
    assert.equal(open.getAttribute('data-expanded'), 'true');
    assert.equal(
        open.querySelector('.archive-row__progress-children')
            .querySelectorAll('[data-line-no]').length, 14,
        'expanding must render all 14 real rows in place');
    assert.equal(open.querySelectorAll('[data-action="collapse-progress"]').length, 1);
});

await test('a lone progress line stays a normal row', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const items = w.ArchiveLineRender.groupRows([
        { line_no: 1, record_type: 'assistant' },
        { line_no: 2, record_type: 'progress' },
        { line_no: 3, record_type: 'assistant' },
    ]);
    assert.equal(items.length, 3);
    // A chip reading "progress x 1" costs a row and says less than the
    // row it replaced.
    assert.equal(items[1].kind, undefined);
    assert.equal(items[1].line_no, 2);
});

await test('sidechain, agent lineage and compact boundary are distinct, in text', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveLineRender.renderLine(env.document, {
        line_no: 11, record_type: 'assistant', role: 'assistant', body_chars: 9,
        is_sidechain: true, agent_id: 'sub-7', is_compact_boundary: true,
        compact_subtype: 'auto',
    }, null, {});
    // Each badge is a SEPARATE element with its own data-badge value and
    // its own words. None of the three is carried by colour.
    assert.equal(el.querySelectorAll('[data-badge="sidechain"]').length, 1);
    assert.equal(el.querySelectorAll('[data-badge="agent"]').length, 1);
    assert.equal(el.querySelectorAll('[data-badge="compact-boundary"]').length, 1);
    assert.ok(el.textContent.includes('sidechain'));
    assert.ok(el.textContent.includes('agent sub-7'));
    assert.ok(el.textContent.includes('compact boundary'));

    const plain = w.ArchiveLineRender.renderLine(env.document,
        { line_no: 12, record_type: 'assistant', role: 'assistant',
            body_chars: 9 }, null, {});
    assert.equal(plain.querySelectorAll('[data-badge]').length, 0,
        'an ordinary row must carry no lineage badges at all');
});

await test('every one of the 26 record types renders, and unknown types do not crash', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const LR = w.ArchiveLineRender;
    // The 26 values measured in message_record_types, 2026-08-31.
    const TYPES = ['agent-name', 'ai-title', 'artifact-autoreact-ledger',
        'artifact-comment-monitor', 'assistant', 'atis-latch', 'attachment',
        'bridge-session', 'cost-state', 'custom-title', 'file-history-delta',
        'file-history-snapshot', 'frame-link', 'last-prompt', 'mode',
        'permission-mode', 'pr-link', 'progress', 'queue-operation',
        'rate_limit_event', 'result', 'started', 'summary', 'system',
        'tool_use_summary', 'user'];
    assert.equal(TYPES.length, 26);
    const seen = new Set();
    for (const rt of TYPES) {
        const el = LR.renderLine(env.document,
            { line_no: 1, record_type: rt, role: null, body_chars: 3 }, null, {});
        assert.equal(el.getAttribute('data-record-type'), rt);
        seen.add(el.getAttribute('data-family'));
        assert.ok(el.textContent.includes(rt), `${rt} not named in its own row`);
    }
    assert.equal(seen.size, 5, `expected 5 families, saw ${[...seen].join(',')}`);
    // A 27th record type appearing upstream must render plainly, not
    // crash and not silently look like a conversation turn.
    const unknown = LR.renderLine(env.document,
        { line_no: 1, record_type: 'brand-new-thing', body_chars: 3 }, null, {});
    assert.equal(unknown.getAttribute('data-family'), 'meta');
    assert.equal(LR.familyFor(null), 'meta');
    assert.equal(LR.familyFor(undefined), 'meta');
});

await test('a failed body fetch renders the shared outcome block, not a blank row', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const cache = w.ArchiveBodyCache.createCache({
        api: {
            /** @returns {Promise<object>} a cannot_determine envelope */
            async getArchiveBody() {
                return { result: null, result_status: 'cannot_determine',
                    scope_status: 'resolved',
                    unevaluated: [{ subject: 'body:5', reason: 'disk read failed' }],
                    meta: {} };
            },
        },
    });
    const row = { line_no: 6, record_type: 'user', role: 'user',
        body_id: 5, body_chars: 20 };
    const e = await cache.request(row);
    assert.equal(e.state, w.ArchiveBodyCache.STATE_CANNOT_DETERMINE);

    const el = w.ArchiveLineRender.renderLine(env.document, row, e, {});
    // Routed through archive-outcome-view.js, so it cannot drift away
    // from how every other failure in this screen looks.
    const block = el.querySelector('[data-outcome]');
    assert.ok(block, 'a failed body must render an outcome block');
    assert.equal(block.getAttribute('data-outcome'), 'cannot-determine');
    assert.ok(el.textContent.includes('COULD NOT EVALUATE'));
    assert.ok(el.textContent.includes('disk read failed'),
        "the server's own reason must be rendered verbatim");
    assert.ok(el.querySelectorAll('[data-action]').length > 0,
        'a could-not-evaluate must offer an action, never be a dead end');
});

await test('a not-requested row shows a sized placeholder, never a spinner', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveLineRender.renderLine(env.document,
        { line_no: 20, record_type: 'user', role: 'user', body_id: 9,
            body_chars: 1234 }, null, {});
    const box = el.querySelector('.archive-row__body');
    assert.equal(box.getAttribute('data-body-state'), 'not-requested');
    assert.equal(el.querySelectorAll('.archive-row__loading').length, 0,
        'nothing was asked for, so there is nothing to wait on');
    assert.ok(el.textContent.includes('not loaded yet'));
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
