// THE CONVERSATION VIEW'S RENDERING CONTRACT.
//
// FIVE THINGS ARE ASSERTED HERE AND EACH ONE IS A FAILURE THAT WOULD
// LOOK LIKE A SUCCESS ON SCREEN:
//
//   1. A turn renders its text WITHOUT the envelope. The complaint that
//      produced this whole view was "im looking at a bunch of raw json
//      so it does not read properly", so a bubble whose default state
//      leaks a uuid is the bug, not a cosmetic issue.
//   2. A secret-bearing block is NEVER rendered unmasked, and a mask
//      REFUSAL renders the refusal rather than the body. Half-masked
//      output does not look like a failure - it looks like a success
//      with a short hex tail that reads as prose - which is why the
//      assertion is that the credential substring is ABSENT from the
//      aggregate textContent, not that a marker is present.
//   3. A WITHHELD block shows as withheld WITH ITS LENGTH. "Too big to
//      send" and "empty" are different findings and must not render
//      alike.
//   4. Subagents render in TIME ORDER with VISIBLE ordinals, and an
//      ordinal is only printed when the view can actually justify it.
//   5. "No subagents" and "the subagent lookup failed" render
//      DIFFERENTLY. Collapsing those two would report "this turn spawned
//      nothing" for a turn that spawned five.
//
// EVERY FIXTURE HERE IS THE SHAPE THE LIVE SERVER ACTUALLY SENDS,
// measured against 127.0.0.1:5055 on 2026-09-01, not the shape this
// client was first written against. Two of them differ in ways that
// would have been invisible until somebody opened the screen: a spawn
// carries `transcripts[]` and `agent_ids[]` rather than a single
// `transcript_id`, and the envelope is NESTED under `info.line`,
// `info.body` and `info.usage`. A test written to the assumed shape
// passes against code written to the same assumption and proves nothing
// about the running system.
//
// NO REAL CREDENTIAL APPEARS IN THIS FILE. The synthetic one is
// uppercase and digits only.
//
// TRAP AVOIDED: deepStrictEqual compares prototypes across vm realms, so
// every structural assertion here counts keys instead.
//
// Run with: node tests/test_archive_chat_render.node.mjs

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
 * Run one named assertion block, recording pass/fail. AWAITED at every
 * call site: a harness that fires an async body without awaiting it
 * records a pass before the assertions have run.
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
 * Load the chat client modules into one vm sandbox sharing a window.
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
    for (const f of ['archive-outcome.js', 'archive-mask.js',
        'archive-outcome-view.js', 'archive-virtual-list.js',
        'archive-chat-block.js', 'archive-chat-info.js',
        'archive-chat-subagents.js', 'archive-chat-turn.js',
        'archive-chat-estimate.js', 'archive-chat-stack.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/** Synthetic credential. Uppercase and digits only, never a real secret. */
const SECRET = 'AKIA7Q2W9E4R6T8Y0U1I3O5P7A9S1D3F5G7H9J1K';
/** A block text with the synthetic credential embedded. */
const SECRET_TEXT = 'prefix-padding--' + SECRET + '--tail';
/** Where the credential sits, in UTF-16 code units. */
const SECRET_OFFSET = SECRET_TEXT.indexOf(SECRET);

/**
 * Assert the synthetic credential appears nowhere in a rendered subtree,
 * and that no fragment of it long enough to be useful survives either.
 * The 4-character tail check is the specific measured bug: code-point
 * offsets in JavaScript slide the mask window four units left and leave
 * the LAST FOUR characters of a 40-character credential on screen.
 * @param {object} el - a MiniElement
 * @returns {void}
 */
function assertNoCredential(el) {
    const t = el.textContent;
    assert.ok(!t.includes(SECRET), 'the whole credential reached the DOM');
    assert.ok(!t.includes(SECRET.slice(-4)),
        'the credential TAIL reached the DOM - this is the code-point ' +
        'offset bug, and it looks like a working mask');
    assert.ok(!t.includes(SECRET.slice(0, 8)),
        'the credential HEAD reached the DOM');
}

// ---------------------------------------------------------------------
// 1. A turn reads as chat, not as an envelope.
// ---------------------------------------------------------------------

await test('a turn renders its text with no envelope noise in the default view', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document, {
        line_no: 12, body_id: 99, role: 'user', record_type: 'user',
        ts: '2026-09-01T10:00:00Z', model: null,
        uuid: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        parent_uuid: 'ffffffff-1111-2222-3333-444444444444',
        blocks: [{ seq: 0, type: 'text', text: 'run the backup please',
            text_length: 21, text_state: 'included' }],
        subagents: [], secret_finding_count: 0
    }, { index: 0 });

    const t = el.textContent;
    assert.ok(t.includes('run the backup please'), 'the message must be readable');
    assert.ok(t.includes('You'), 'the speaker must be named in TEXT, not colour alone');
    assert.ok(!t.includes('aaaaaaaa-bbbb'),
        'the uuid must be behind the "i", not in the bubble');
    assert.ok(!t.includes('ffffffff-1111'),
        'the parent uuid must be behind the "i"');
    assert.ok(!t.includes('body_id') && !t.includes('record_type'),
        'no envelope field names may appear in the default view');
    assert.equal(el.getAttribute('data-role'), 'user');
    assert.equal(el.getAttribute('data-index'), '0');
});

await test('an assistant turn is distinguishable from a user turn without colour', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const mk = (role) => w.ArchiveChatTurn.renderTurn(env.document,
        { role: role, blocks: [{ seq: 0, type: 'text', text: 'x' }], subagents: [] },
        { index: 0 });
    const u = mk('user');
    const a = mk('assistant');
    assert.notEqual(u.getAttribute('data-role'), a.getAttribute('data-role'));
    assert.ok(u.textContent.includes('You'));
    assert.ok(a.textContent.includes('Claude'));
});

await test('tool calls, tool results and thinking are collapsed; prose is not', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'assistant', subagents: [],
        blocks: [
            { seq: 0, type: 'text', text: 'I will read the file.', text_length: 21 },
            { seq: 1, type: 'thinking', text: 'internal reasoning', text_length: 18 },
            { seq: 2, type: 'tool_use', tool_name: 'Read',
              text: '{"file_path":"/etc/hosts"}', text_length: 26 },
            { seq: 3, type: 'tool_result', tool_name: 'Read', is_error: true,
              text: 'permission denied', text_length: 17 }
        ]
    }, { index: 0 });

    const blocks = el.querySelectorAll('.archive-chat-block');
    assert.equal(blocks.length, 4);
    assert.equal(blocks[0].querySelectorAll('details').length, 0,
        'prose must not be behind a disclosure');
    for (const i of [1, 2, 3]) {
        assert.equal(blocks[i].querySelectorAll('details').length, 1,
            'block ' + i + ' must be collapsed by default');
    }
    assert.ok(el.textContent.includes('Read'), 'the tool NAME must be visible unopened');
    assert.ok(el.textContent.includes('ERROR'),
        'a tool error must be named in TEXT, not signalled by colour alone');
});

// ---------------------------------------------------------------------
// 2. Secrets.
// ---------------------------------------------------------------------

await test('a block with a usable finding is masked, never rendered raw', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: SECRET_TEXT, text_length: SECRET_TEXT.length,
        text_state: 'included',
        secrets: [{ utf16_state: 'computed',
            match_offset_utf16: SECRET_OFFSET, match_length_utf16: SECRET.length }],
        secret_finding_count: 1
    }, null, null);
    assertNoCredential(el);
    assert.ok(el.textContent.includes(w.ArchiveMask.SECRET_MARKER),
        'the masked window must be visibly marked');
    assert.ok(el.textContent.includes('prefix-padding'),
        'the surrounding text must survive - this is a lens, not a redaction');
});

await test('a mask REFUSAL renders the refusal and none of the body', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The measured live shape: a declared count with NO findings array.
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: SECRET_TEXT, text_length: SECRET_TEXT.length,
        text_state: 'included', secret_finding_count: 3
    }, null, null);
    assertNoCredential(el);
    assert.ok(!el.textContent.includes('prefix-padding'),
        'a refusal must withhold the WHOLE body, not just the window');
    assert.equal(el.querySelectorAll('[data-text-state="mask-refused"]').length, 1);
    assert.ok(el.textContent.includes('NOT SHOWN'));
    assert.ok(el.textContent.includes('3'), 'the refusal must state how many were flagged');
});

await test('a cannot_determine finding poisons the whole block, not just its window', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: SECRET_TEXT, text_length: SECRET_TEXT.length,
        text_state: 'included',
        secrets: [{ utf16_state: 'cannot_determine',
            match_offset_utf16: null, match_length_utf16: null }],
        secret_finding_count: 1
    }, null, null);
    assertNoCredential(el);
    assert.ok(el.textContent.includes('cannot_determine'),
        'the refusal must name WHAT could not be evaluated');
});

await test('a turn-level secret count reaches a block that carries no count of its own', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'assistant', subagents: [], secret_finding_count: 2,
        blocks: [{ seq: 0, type: 'text', text: SECRET_TEXT,
            text_length: SECRET_TEXT.length, text_state: 'included' }]
    }, { index: 0 });
    assertNoCredential(el);
    assert.equal(el.querySelectorAll('[data-text-state="mask-refused"]').length, 1,
        'the pessimistic path must engage when only the TURN declares secrets');
});

// ---------------------------------------------------------------------
// 3. Withheld.
// ---------------------------------------------------------------------

await test('a withheld block renders as withheld WITH its length, never as empty', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: null, text_length: 4194304,
        text_state: 'withheld_too_large'
    }, null, null);
    assert.equal(el.getAttribute('data-text-state'), 'withheld');
    assert.ok(el.textContent.includes('WITHHELD'));
    assert.ok(el.textContent.includes('4194304'), 'the size must be stated');
    assert.ok(el.textContent.includes('withheld_too_large'),
        "the server's own reason must be rendered verbatim");
    assert.ok(el.textContent.includes('Not empty'),
        'withheld and empty must not read alike');
});

await test('a TRUNCATED block says so, and says how much is missing', () => {
    // Measured live 2026-09-01 on transcript 4: 143 of 400 blocks came
    // back truncated at the server's 400-character preview gate. Text
    // that simply stops reads as the whole thing, and the reader draws a
    // conclusion from an excerpt believing they saw all of it.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: 'the first four hundred characters',
        text_length: 1046, text_truncated: true, text_state: 'included'
    }, null, null);
    assert.ok(el.textContent.includes('the first four hundred characters'),
        'the excerpt itself must still be readable');
    assert.equal(el.querySelectorAll('[data-truncated="true"]').length, 1);
    assert.ok(el.textContent.includes('TRUNCATED BY THE SERVER'));
    assert.ok(el.textContent.includes('1046'),
        'the full length must be stated, or the shortfall is invisible');
    assert.ok(el.textContent.includes('raw view'),
        'the reader must be told where the whole record is');
});

await test('a block that is NOT truncated carries no truncation note', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: 'text', text: 'all of it', text_length: 9,
        text_truncated: false, text_state: 'included'
    }, null, null);
    assert.equal(el.querySelectorAll('[data-truncated]').length, 0);
    assert.ok(!el.textContent.includes('TRUNCATED'),
        'a note on a complete block would be a warning about nothing');
});

await test('_string_content renders as prose, not under its internal name', () => {
    // The server's name for a message whose `content` was a bare JSON
    // string. It is the most common shape of a user turn in this corpus.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document, {
        seq: 0, type: '_string_content', text: 'help me manage my config',
        text_length: 24, text_state: 'included'
    }, null, null);
    assert.equal(el.querySelectorAll('details').length, 0,
        'prose must not be folded behind a disclosure');
    assert.ok(el.textContent.includes('help me manage my config'));
    assert.ok(!el.textContent.includes('_string_content'),
        'an internal type name must not reach the reader');
});

await test('the server withholding the secret block does NOT poison the turn\'s prose', () => {
    // Measured live 2026-09-01: this server sets
    // `text_state: withheld_secret_bearing` on the flagged block and
    // reports its full length. Having located the secrets for us, it has
    // left none unlocated, so the turn's other blocks are safe to show.
    // Refusing them anyway would withhold prose for no safety gain.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const turn = {
        role: 'assistant', subagents: [], subagents_state: 'none_spawned',
        secret_finding_count: 2,
        blocks: [
            { seq: 0, type: 'text', text: 'Here is what I found.',
              text_length: 21, text_state: 'included' },
            { seq: 1, type: 'tool_result', text: null, text_length: 2756,
              text_state: 'withheld_secret_bearing' }
        ]
    };
    const el = w.ArchiveChatTurn.renderTurn(env.document, turn, { index: 0 });
    assert.ok(el.textContent.includes('Here is what I found.'),
        'prose in a secret-bearing turn must survive when the server has ' +
        'already located and withheld the secret');
    assert.equal(el.querySelectorAll('[data-text-state="mask-refused"]').length, 0);
    // Counted by CLASS, not by the data attribute: the block root and
    // the withheld box both carry `data-text-state`, so the attribute
    // count is 2 for one withheld block and would read as two blocks.
    assert.equal(el.querySelectorAll('.archive-chat-block__withheld').length, 1);
    assert.ok(el.textContent.includes('2756'));
    assert.ok(el.textContent.includes('withheld_secret_bearing'));
});

await test('a declared secret with NOTHING withheld still refuses every block', () => {
    // The POSITIVE CONTROL for the refinement above: remove the
    // withheld block and the pessimistic path must come straight back,
    // because the count then names a credential at an unknown position.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'assistant', subagents: [], subagents_state: 'none_spawned',
        secret_finding_count: 2,
        blocks: [{ seq: 0, type: 'text', text: SECRET_TEXT,
            text_length: SECRET_TEXT.length, text_state: 'included' }]
    }, { index: 0 });
    assertNoCredential(el);
    assert.equal(el.querySelectorAll('[data-text-state="mask-refused"]').length, 1);
});

await test('a block whose state cannot be determined is not rendered as withheld or as text', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatBlock.renderBlock(env.document,
        { seq: 0, type: 'text' }, null, null);
    assert.equal(el.getAttribute('data-text-state'), 'cannot-determine');
    assert.equal(el.querySelectorAll('[data-outcome="cannot-determine"]').length, 1,
        'the third outcome must go through the shared outcome view');
});

// ---------------------------------------------------------------------
// 4 and 5. Subagents.
// ---------------------------------------------------------------------

await test('subagents render in declared order with visible ordinals', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatSubagents.renderSubagents(env.document, {
        line_no: 5,
        subagents_state: 'resolved',
        subagents: [
            { order: 3, order_basis: 'start_ts', link_state: 'resolved',
              agent_ids: ['c'], start_ts: '2026-09-01T10:03:00Z',
              transcripts: [{ transcript_id: 30, session_ref: 'third',
                  line_count: 9 }], transcript_count: 1 },
            { order: 1, order_basis: 'start_ts', link_state: 'resolved',
              agent_ids: ['a'], start_ts: '2026-09-01T10:01:00Z',
              transcripts: [{ transcript_id: 10, session_ref: 'first',
                  line_count: 7 }], transcript_count: 1 },
            { order: 2, order_basis: 'start_ts', link_state: 'resolved',
              agent_ids: ['b'], start_ts: '2026-09-01T10:02:00Z',
              transcripts: [{ transcript_id: 20, session_ref: 'second',
                  line_count: 8 }], transcript_count: 1 }
        ]
    });
    assert.equal(el.getAttribute('data-order-basis'), 'start_ts',
        "the SERVER's basis is rendered, not this view's guess about it");
    assert.ok(el.textContent.includes('ordered by START TIME'),
        'the basis must be explained in words a reader can weigh');
    const ords = el.querySelectorAll('[data-ordinal]');
    assert.equal(ords.length, 3);
    assert.equal(ords[0].textContent, '1st');
    assert.equal(ords[1].textContent, '2nd');
    assert.equal(ords[2].textContent, '3rd');
    const names = el.querySelectorAll('.archive-chat-subagents__name');
    assert.equal(names[0].textContent, 'first');
    assert.equal(names[2].textContent, 'third');
});

await test('with no declared order the ordinals are DERIVED from start time and say so', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatSubagents.renderSubagents(env.document, {
        subagents_state: 'resolved',
        subagents: [
            { link_state: 'resolved', start_ts: '2026-09-01T10:05:00Z',
              transcripts: [{ transcript_id: 20, session_ref: 'later' }] },
            { link_state: 'resolved', start_ts: '2026-09-01T10:01:00Z',
              transcripts: [{ transcript_id: 10, session_ref: 'earlier' }] }
        ]
    });
    assert.equal(el.getAttribute('data-order-basis'), 'derived-from-start-time');
    const names = el.querySelectorAll('.archive-chat-subagents__name');
    assert.equal(names[0].textContent, 'earlier');
    assert.ok(el.textContent.includes('derived'),
        'a derived ordinal must say it was derived rather than stated');
});

await test('with neither an order nor a start time the ordinals are WITHHELD, not invented', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatSubagents.renderSubagents(env.document, {
        subagents_state: 'resolved',
        subagents: [
            { link_state: 'resolved',
              transcripts: [{ transcript_id: 10, session_ref: 'one' }] },
            { link_state: 'resolved',
              transcripts: [{ transcript_id: 20, session_ref: 'two' }] }]
    });
    assert.equal(el.getAttribute('data-order-basis'), 'cannot-determine');
    const ords = el.querySelectorAll('[data-ordinal]');
    assert.equal(ords[0].textContent, 'NOT KNOWN');
    assert.equal(ords[0].getAttribute('data-ordinal'), 'unknown');
    assert.ok(el.textContent.includes('RUN ORDER NOT KNOWN'));
    assert.ok(!el.textContent.includes('1st'),
        'printing an ordinal here would be a claim nobody measured');
});

await test('no subagents and a FAILED subagent lookup render differently', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);

    const none = w.ArchiveChatSubagents.renderSubagents(env.document,
        { line_no: 5, subagents: [], subagents_state: 'none_spawned' });
    assert.equal(none, null,
        'a turn that spawned nothing carries no subagent affordance at all');
    assert.equal(w.ArchiveChatSubagents.expanderFor(
        { subagents: [], subagents_state: 'none_spawned' }), null);

    const failed = w.ArchiveChatSubagents.renderSubagents(env.document,
        { line_no: 5 });
    assert.ok(failed, 'a failed lookup must render SOMETHING');
    assert.equal(failed.getAttribute('data-subagent-state'), 'cannot-determine');
    assert.equal(failed.querySelectorAll('[data-outcome="cannot-determine"]').length, 1);
    assert.ok(failed.textContent.includes('NOT KNOWN'));
    assert.ok(failed.textContent.includes('not the same as it having spawned none'),
        'the two findings must be told apart IN WORDS, not only structurally');

    const exp = w.ArchiveChatSubagents.expanderFor({ line_no: 5 });
    assert.ok(exp && exp.label.includes('NOT KNOWN'));
});

await test('an explicit subagent_status of not-resolved overrides an empty array', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The dangerous shape: the server sends [] AND says it could not
    // look. The array must not be read as evidence.
    assert.equal(w.ArchiveChatSubagents.lookupState(
        { subagents: [], subagents_state: 'cannot_determine' }), 'cannot-determine');
    assert.equal(w.ArchiveChatSubagents.lookupState(
        { subagents: [], subagents_state: 'a_state_invented_later' }),
        'cannot-determine',
        'an unrecognised state must fail toward the third outcome');
    const el = w.ArchiveChatSubagents.renderSubagents(env.document,
        { subagents: [], subagents_state: 'cannot_determine' });
    assert.ok(el, 'an unevaluated lookup must never render as "spawned nothing"');
});

await test('an UNLINKED spawn is listed, counted, and named as a real run', () => {
    // The measured shape, and the one this assertion exists for: the
    // server's own meta says "an unresolved entry means the run is real
    // and unidentified, never that no run happened". Rendering it as
    // absent would drop a run that happened.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatSubagents.renderSubagents(env.document, {
        subagents_state: 'resolved',
        subagents: [{ order: 1, order_basis: 'file_position',
            link_state: 'tool_result_carries_no_agent_id',
            agent_ids: [], start_ts: null, transcripts: [],
            transcript_count: 0,
            spawned_by: { tool_name: 'Agent', line_no: 4 } }]
    });
    const btn = el.querySelector('[data-action="open-subagent"]');
    assert.equal(btn.getAttribute('data-openable'), 'false');
    assert.equal(btn.getAttribute('disabled'), 'disabled');
    assert.equal(btn.getAttribute('data-link-state'),
        'tool_result_carries_no_agent_id');
    assert.ok(el.textContent.includes('tool_result_carries_no_agent_id'),
        "the server's own reason must be rendered verbatim");
    assert.ok(el.textContent.includes('not the same as no run happening'));
    assert.ok(el.textContent.includes('1 of these 1 run(s)'),
        'unopenable runs must be COUNTED out loud, not left to be noticed');
    assert.ok(el.textContent.includes('Agent spawn at line 4'),
        'an unidentified run is still named by what spawned it');
    assert.ok(el.textContent.includes('NOT a measured clock'),
        'a file-position ordering must not read as a chronological one');
});

// ---------------------------------------------------------------------
// The "i" panel.
// ---------------------------------------------------------------------

await test('the info panel states every envelope field, and NAMES the ones it lacks', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The MEASURED envelope shape: three nests, and the uuid under
    // `message_uuid` rather than `uuid`.
    const el = w.ArchiveChatInfo.renderInfo(env.document, {
        line_no: 12, body_id: 99, role: 'assistant', record_type: 'assistant',
        ts: '2026-09-01T10:00:00Z', model: 'claude-opus-5',
        role_state: 'role', secret_finding_count: 0,
        info: {
            message_uuid: 'aaaa-bbbb',
            line: { line_no: 12, line_status: 'ok',
                fidelity_outcome: 'fidelity_verified' },
            body: { body_id: 99, body_chars: 235 },
            usage: { state: 'recorded', input_tokens: 1200, output_tokens: 340 }
        }
    });
    const t = el.textContent;
    assert.ok(t.includes('aaaa-bbbb'), 'the uuid belongs HERE and only here');
    assert.ok(t.includes('claude-opus-5'));
    assert.ok(t.includes('1200') && t.includes('340'), 'token usage must be shown');
    assert.ok(t.includes('fidelity_verified') && t.includes('235'),
        'the NESTED envelope fields must be found, not reported as gaps');

    const parent = el.querySelector('[data-field="parent uuid"]');
    assert.equal(parent.getAttribute('data-known'), 'false');
    assert.equal(parent.textContent, 'NOT KNOWN',
        'an absent field must be NAMED, never rendered as a blank cell');

    const cacheRow = el.querySelector('[data-field="cache read tokens"]');
    assert.ok(cacheRow, 'every declared field must be present even when absent');
});

await test('the info panel surfaces server fields it has no name for', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatInfo.renderInfo(env.document,
        { role: 'user', info: { some_new_server_field: 'value-42' } });
    assert.ok(el.textContent.includes('some_new_server_field'));
    assert.ok(el.textContent.includes('value-42'),
        'a field this view does not know must be shown, never silently dropped');
});

await test('a turn shows the info panel only when the view says it is open', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const turn = { role: 'user', subagents: [], subagents_state: 'none_spawned',
        info: { message_uuid: 'zzzz-9999' },
        blocks: [{ seq: 0, type: 'text', text: 'hi' }] };
    const closed = w.ArchiveChatTurn.renderTurn(env.document, turn, { index: 0 });
    assert.equal(closed.querySelectorAll('[data-panel="info"]').length, 0);
    assert.ok(!closed.textContent.includes('zzzz-9999'));
    assert.equal(closed.querySelector('[data-action="toggle-turn-info"]')
        .getAttribute('aria-expanded'), 'false');

    const open = w.ArchiveChatTurn.renderTurn(env.document, turn,
        { index: 0, infoOpen: true });
    assert.equal(open.querySelectorAll('[data-panel="info"]').length, 1);
    assert.ok(open.textContent.includes('zzzz-9999'));
    assert.equal(open.querySelector('[data-action="toggle-turn-info"]')
        .getAttribute('aria-expanded'), 'true');
});

// ---------------------------------------------------------------------
// Blocks: the three-outcome shape of an absent array.
// ---------------------------------------------------------------------

await test('an unrecognised blocks_state is a could-not-evaluate, not an empty turn', () => {
    // The empty array is REAL here - the danger is trusting it. A state
    // this view cannot interpret means it does not know what that empty
    // array represents.
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'user', subagents: [], subagents_state: 'none_spawned',
        blocks: [], blocks_state: 'a_state_invented_later'
    }, { index: 0 });
    assert.equal(el.querySelector('.archive-chat-turn__body')
        .getAttribute('data-blocks'), 'cannot-determine');
    assert.ok(el.textContent.includes('a_state_invented_later'));

    // ...and the three MEASURED states are all trusted.
    for (const bs of ['extracted', 'content_string', 'no_message_content']) {
        const ok = w.ArchiveChatTurn.renderTurn(env.document, {
            role: 'user', subagents: [], subagents_state: 'none_spawned',
            blocks: [], blocks_state: bs
        }, { index: 0 });
        assert.equal(ok.querySelector('.archive-chat-turn__body')
            .getAttribute('data-blocks'), '0', bs + ' must be trusted');
    }
});

await test('a role INFERRED from the record type is marked as inferred', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const inferred = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'system', role_state: 'record_type_fallback',
        subagents: [], subagents_state: 'none_spawned', blocks: []
    }, { index: 0 });
    assert.ok(inferred.textContent.includes('(inferred)'),
        'a derived speaker must not read as a declared one');
    const declared = w.ArchiveChatTurn.renderTurn(env.document, {
        role: 'user', role_state: 'role',
        subagents: [], subagents_state: 'none_spawned', blocks: []
    }, { index: 0 });
    assert.ok(!declared.textContent.includes('(inferred)'));
});

await test('a turn with NO blocks array is a could-not-evaluate, not an empty turn', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const missing = w.ArchiveChatTurn.renderTurn(env.document,
        { role: 'user', subagents: [] }, { index: 0 });
    assert.equal(missing.querySelector('.archive-chat-turn__body')
        .getAttribute('data-blocks'), 'cannot-determine');
    assert.ok(missing.textContent.includes('not the same as it having said nothing'));

    const empty = w.ArchiveChatTurn.renderTurn(env.document,
        { role: 'user', subagents: [], subagents_state: 'none_spawned',
          blocks: [], blocks_state: 'no_message_content' }, { index: 0 });
    assert.equal(empty.querySelector('.archive-chat-turn__body')
        .getAttribute('data-blocks'), '0');
    assert.ok(empty.textContent.includes('The server looked'));
    assert.equal(empty.querySelectorAll('[data-outcome]').length, 0,
        'a genuinely empty turn is not a failure and must not render as one');
});

await test('progress runs stay collapsed behind a count chip', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const el = w.ArchiveChatTurn.renderTurn(env.document,
        { kind: 'progress-run', from: 100, to: 480, count: 381 }, { index: 4 });
    assert.equal(el.getAttribute('data-kind'), 'progress-run');
    assert.ok(el.textContent.includes('381 progress records'));
    assert.ok(el.textContent.includes('lines 100 to 480'));
    const btn = el.querySelector('[data-action]');
    assert.equal(btn.getAttribute('data-action'), 'expand-progress');
    assert.equal(btn.getAttribute('aria-expanded'), 'false');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
