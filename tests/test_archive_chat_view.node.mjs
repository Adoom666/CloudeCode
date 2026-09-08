// THE CONVERSATION VIEW'S BEHAVIOUR: toggling, drilling, getting back,
// and what it does when the endpoint it reads is not there.
//
// THE MISSING ENDPOINT IS THE ASSERTION THAT MATTERS MOST HERE. This
// client shipped alongside the route it reads, so "the route answers
// 404" is a real, expected state rather than a hypothetical - and the
// wrong handling of it is not a crash, it is an EMPTY CHAT PANE, which
// silently asserts that a transcript with 30,805 lines contains no
// messages. A blank pane is a verdict nobody measured. It must render a
// named could-not-determine instead, and it must not be confused with a
// transcript that genuinely does not exist, which the archive answers
// with a complete envelope carrying result_status 'not_found'.
//
// THE DRILL CHAIN IS ASSERTED BOTH WAYS. Going down is easy to get
// right; coming back up from four levels to the first in ONE click is
// the part that breaks, and the owner said he will nest.
//
// TRAP AVOIDED: deepStrictEqual compares prototypes across vm realms, so
// structural assertions count keys.
// TRAP AVOIDED: every async body is AWAITED. A harness that fires one
// without awaiting records a pass before the assertions run.
//
// Run with: node tests/test_archive_chat_view.node.mjs

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
 * Run one named assertion block, recording pass/fail.
 * @param {string} name @param {() => (void|Promise<void>)} fn
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

/** Let the view's rAF fallback (setTimeout 16) run. @returns {Promise} */
function frame() {
    return new Promise((r) => setTimeout(r, 30));
}

/**
 * Load the chat modules into one vm sandbox sharing a window.
 * @param {object} doc @returns {object} the shared fake window
 */
function loadModules(doc) {
    const fakeWindow = { document: doc };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        Float64Array, parseInt, isFinite, RegExp,
        setTimeout, clearTimeout,
    };
    context.globalThis = context;
    vm.createContext(context);
    for (const f of ['archive-outcome.js', 'archive-mask.js',
        'archive-outcome-view.js', 'archive-virtual-list.js',
        'archive-chat-block.js', 'archive-chat-info.js',
        'archive-chat-subagents.js', 'archive-chat-turn.js',
        'archive-chat-estimate.js', 'archive-chat-stack.js',
        'archive-chat-clicks.js', 'archive-chat-view.js',
        'archive-chat-screen.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/**
 * A minimal turn.
 * @param {number} n @param {object} extra @returns {object}
 */
function turn(n, extra) {
    return Object.assign({
        line_no: n, body_id: n, role: n % 2 ? 'assistant' : 'user',
        record_type: n % 2 ? 'assistant' : 'user',
        ts: '2026-09-01T10:00:0' + (n % 10) + 'Z',
        blocks: [{ seq: 0, type: 'text', text: 'message ' + n,
            text_length: 9, text_state: 'included' }],
        subagents: [], subagents_state: 'none_spawned', secret_finding_count: 0
    }, extra || {});
}

/**
 * A fake api whose messages route can be told how to answer.
 * @param {function} answer - (id) => a callEnvelope result
 * @returns {object} {api, asked}
 */
function fakeApi(answer) {
    const asked = [];
    return {
        asked,
        api: {
            /** @param {*} id @returns {Promise<object>} */
            async listArchiveMessages(id) {
                asked.push(id);
                return answer(id);
            }
        }
    };
}

/** An ok envelope carrying turns. @param {Array} rows @returns {object} */
function okEnvelope(rows) {
    return {
        envelope: {
            result: { turns: rows }, result_status: 'ok',
            scope_status: 'resolved', unevaluated: [],
            meta: { paging: { has_more: false } }
        },
        httpStatus: 200, headers: null, transportError: null
    };
}

// ---------------------------------------------------------------------
// The missing endpoint.
// ---------------------------------------------------------------------

await test('a MISSING messages endpoint renders could-not-determine, not an empty chat', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // What an unrouted FastAPI path actually answers: no result_status.
    const { api } = fakeApi(() => ({
        envelope: { detail: 'Not Found' }, httpStatus: 404,
        headers: null, transportError: null
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(4, 'main');
    await frame();

    assert.equal(tok, 'cannot-determine');
    const root = screen.view.root();
    assert.equal(root.getAttribute('data-chat-state'), 'cannot-determine');
    assert.equal(root.querySelectorAll('[data-outcome="cannot-determine"]').length, 1);
    const t = root.textContent;
    assert.ok(t.includes('/archive/transcripts/4/messages'),
        'the refusal must NAME the endpoint that did not answer');
    assert.ok(t.includes('HTTP 404'));
    assert.ok(t.includes('NOT a claim that the transcript is empty'));
    assert.equal(root.querySelectorAll('.archive-chat-turn').length, 0);
});

await test('a missing TRANSCRIPT is not-found, told apart from a missing ROUTE', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The archive's own 404: a COMPLETE envelope with a result_status.
    const { api } = fakeApi(() => ({
        envelope: { result: null, result_status: 'not_found',
            scope_status: 'not_found',
            unevaluated: [{ subject: 'transcript:99999',
                reason: 'no row in message_transcripts' }],
            meta: {} },
        httpStatus: 404, headers: null, transportError: null
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(99999, null);
    await frame();

    assert.equal(tok, 'not-found',
        'the same HTTP code must not collapse two different findings');
    const root = screen.view.root();
    assert.equal(root.querySelectorAll('[data-outcome="not-found"]').length, 1);
    assert.ok(root.textContent.includes('no row in message_transcripts'),
        "the server's own reason must be rendered verbatim");
});

await test('a transport failure with no response is could-not-determine and says so', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi(() => ({
        envelope: null, httpStatus: null, headers: null,
        transportError: 'no response in 15s'
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(4, null);
    await frame();
    assert.equal(tok, 'cannot-determine');
    assert.ok(screen.view.root().textContent.includes('no response in 15s'));
});

await test('an ok envelope with NO turns array is could-not-determine, not an empty chat', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi(() => ({
        envelope: { result: { count: 3 }, result_status: 'ok',
            scope_status: 'resolved', unevaluated: [], meta: {} },
        httpStatus: 200, headers: null, transportError: null
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(4, null);
    await frame();
    assert.equal(tok, 'cannot-determine');
    assert.ok(screen.view.root().textContent.includes('carried no turns array'));
});

await test('an api with no listArchiveMessages at all is named, not silently blank', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const screen = w.ArchiveChatScreen.create({ document: env.document, api: {} });
    screen.mount(env.document.body);
    const tok = await screen.open(4, null);
    await frame();
    assert.equal(tok, 'cannot-determine');
    assert.ok(screen.view.root().textContent.includes('listArchiveMessages'));
});

// ---------------------------------------------------------------------
// The happy path and the panels.
// ---------------------------------------------------------------------

await test('a real conversation renders as chat with no raw JSON in the default view', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const rows = [turn(1), turn(2), turn(3)];
    const { api, asked } = fakeApi(() => okEnvelope(rows));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(4, 'my session');
    await frame();

    assert.equal(tok, 'ok');
    assert.equal(asked.length, 1);
    const root = screen.view.root();
    assert.equal(root.querySelectorAll('.archive-chat-turn').length, 3);
    const t = root.textContent;
    assert.ok(t.includes('message 1') && t.includes('message 3'));
    assert.ok(!t.includes('"role"') && !t.includes('{"'),
        'no raw JSON envelope may appear in the default view');
    assert.ok(t.includes('my session'), 'the chain names the level being read');
});

await test('the "i" opens and closes, and the geometry follows it in the same frame', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    // The MEASURED envelope shape: the uuid is nested under
    // `info.message_uuid`, not on the turn.
    const rows = [turn(1, { info: { message_uuid: 'uuid-for-one' } }), turn(2)];
    const { api } = fakeApi(() => okEnvelope(rows));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();

    const root = screen.view.root();
    const before = screen.view.list.heightOf(0);
    assert.ok(!root.textContent.includes('uuid-for-one'));

    const btn = root.querySelector('[data-action="toggle-turn-info"]');
    btn.dispatchEvent('click');
    await frame();
    assert.ok(screen.view.root().textContent.includes('uuid-for-one'),
        'the envelope must be one click away');
    assert.equal(screen.view.openState()[0].infoOpen, true);
    assert.ok(screen.view.list.heightOf(0) > before,
        'an opened panel must grow the row in the GEOMETRY too, or the ' +
        'rows below it leap on the next scroll');

    const btn2 = screen.view.root().querySelector('[data-action="toggle-turn-info"]');
    btn2.dispatchEvent('click');
    await frame();
    assert.ok(!screen.view.root().textContent.includes('uuid-for-one'));
    assert.equal(screen.view.openState()[0].infoOpen, false);
});

await test('a turn with subagents expands IN PLACE and lists them ordered', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const rows = [turn(1, { subagents_state: 'resolved', subagents: [
        { order: 2, order_basis: 'start_ts', link_state: 'resolved',
          agent_ids: ['b'], start_ts: '2026-09-01T10:02:00Z',
          transcript_count: 1,
          transcripts: [{ transcript_id: 220, session_ref: 'second' }] },
        { order: 1, order_basis: 'start_ts', link_state: 'resolved',
          agent_ids: ['a'], start_ts: '2026-09-01T10:01:00Z',
          transcript_count: 1,
          transcripts: [{ transcript_id: 110, session_ref: 'first' }] }
    ] })];
    const { api } = fakeApi(() => okEnvelope(rows));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();

    let root = screen.view.root();
    const exp = root.querySelector('[data-action="toggle-turn-subagents"]');
    assert.ok(exp, 'a turn with subagents must carry an expander');
    assert.ok(exp.textContent.includes('2 subagents'));
    assert.equal(root.querySelectorAll('[data-panel="subagents"]').length, 0,
        'the list must be closed by default - not information overload');

    exp.dispatchEvent('click');
    await frame();
    root = screen.view.root();
    const panel = root.querySelector('[data-panel="subagents"]');
    assert.ok(panel);
    const names = panel.querySelectorAll('.archive-chat-subagents__name');
    assert.equal(names[0].textContent, 'first');
    assert.equal(names[1].textContent, 'second');
    const ords = panel.querySelectorAll('[data-ordinal]');
    assert.equal(ords[0].textContent, '1st');
    assert.equal(ords[1].textContent, '2nd');
});

// ---------------------------------------------------------------------
// Drilling, and getting back.
// ---------------------------------------------------------------------

await test('drilling into a subagent renders it the SAME way, and the chain grows', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const parentRows = [turn(1, { subagents_state: 'resolved', subagents: [
        { order: 1, order_basis: 'start_ts', link_state: 'resolved',
          agent_ids: ['a'], start_ts: '2026-09-01T10:01:00Z',
          transcript_count: 1,
          transcripts: [{ transcript_id: 110, session_ref: 'Explore',
              line_count: 7 }] }
    ] })];
    const childRows = [turn(7), turn(8)];
    const { api, asked } = fakeApi((id) =>
        okEnvelope(String(id) === '110' ? childRows : parentRows));

    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'parent session');
    await frame();

    let root = screen.view.root();
    const exp = root.querySelector('[data-action="toggle-turn-subagents"]');
    exp.dispatchEvent('click');
    await frame();

    root = screen.view.root();
    const open = root.querySelector('[data-action="open-subagent"]');
    assert.equal(open.getAttribute('data-openable'), 'true');
    open.dispatchEvent('click');
    // The drill fetches; give the promise and the frame time to land.
    await frame();
    await frame();

    assert.equal(asked.length, 2);
    assert.equal(String(asked[1]), '110');
    assert.equal(screen.view.stack().depth(), 2);
    root = screen.view.root();
    assert.equal(root.querySelectorAll('.archive-chat-turn').length, 2,
        'the subagent renders through the SAME turn renderer');
    assert.ok(root.textContent.includes('message 7'));
    assert.ok(root.textContent.includes('Explore'),
        'the chain must name the level drilled into');
    assert.ok(root.textContent.includes('parent session'),
        'the way back must be visible, not implied');
});

await test('the chain goes back up MANY levels in one click, and never below the root', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const st = w.ArchiveChatStack.create();
    st.reset({ transcriptId: 1, label: 'root' });
    st.push({ transcriptId: 2, label: 'a', ordinal: 1 });
    st.push({ transcriptId: 3, label: 'b', ordinal: 1 });
    st.push({ transcriptId: 4, label: 'c', ordinal: 2 });
    assert.equal(st.depth(), 4);

    const nav = w.ArchiveChatStack.renderChain(env.document, st);
    assert.equal(nav.getAttribute('data-depth'), '4');
    assert.equal(nav.querySelectorAll('[data-action="chain-up"]').length, 3,
        'every level except the current one must be a control');
    assert.equal(nav.querySelectorAll('[aria-current="true"]').length, 1);

    const back = st.truncateTo(0);
    assert.equal(back.transcriptId, 1);
    assert.equal(st.depth(), 1, 'four levels to the top in ONE step');

    assert.equal(st.pop(), null, 'the root can never be popped away');
    assert.equal(st.depth(), 1);
    assert.equal(st.truncateTo(9), null, 'an out-of-range index is a no-op');
    assert.equal(st.depth(), 1);
});

await test('a chain level the server never named renders NOT KNOWN, not a plausible label', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const st = w.ArchiveChatStack.create();
    st.reset({ transcriptId: 1, label: 'root' });
    st.push({ transcriptId: 2, label: null, ordinal: 1 });
    const nav = w.ArchiveChatStack.renderChain(env.document, st);
    assert.ok(nav.textContent.includes('NOT KNOWN'));
    assert.ok(!nav.textContent.includes('Subagent'),
        'inventing a name here would be a fact manufactured by navigation');
});

await test('a click whose owning turn has no index toggles NOTHING', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi(() => okEnvelope([turn(1)]));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();

    const stray = env.document.createElement('button');
    stray.setAttribute('data-action', 'toggle-turn-info');
    screen.view.root().appendChild(stray);
    stray.dispatchEvent('click');
    await frame();
    assert.equal(Object.keys(screen.view.openState()).length, 0,
        'a guessed index would open somebody else\'s panel, which looks ' +
        'exactly like the feature working');
});

// ---------------------------------------------------------------------
// Virtualization.
// ---------------------------------------------------------------------

await test('a 30,805-turn conversation windows to a handful of painted bubbles', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const rows = [];
    for (let i = 0; i < 30805; i++) rows.push(turn(i));
    const { api } = fakeApi(() => okEnvelope(rows));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const started = Date.now();
    await screen.open(5767, 'huge');
    await frame();
    const elapsed = Date.now() - started;

    const root = screen.view.root();
    const painted = root.querySelectorAll('.archive-chat-turn').length;
    assert.ok(painted > 0 && painted < 60,
        `windowing must paint a handful, painted ${painted}`);
    assert.equal(screen.view.list.count(), 30805);
    assert.ok(screen.view.list.totalHeight() > 0);
    assert.ok(elapsed < 4000,
        `laying out 30,805 turns took ${elapsed}ms, which is not a first paint`);
});

await test('a single 37 MB block does not eat the whole scrollbar', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const monster = w.ArchiveChatEstimate.estimateTurn({
        role: 'user', blocks: [{ seq: 0, type: 'text', text_length: 37000000,
            text_state: 'included' }]
    }, null);
    assert.equal(monster, w.ArchiveChatEstimate.TURN_MAX_PX,
        'an uncapped estimate here is roughly seven million pixels for ONE ' +
        'row, and every other row becomes unreachable by dragging');
    assert.ok(Number.isFinite(monster) && monster > 0);
});

await test('a collapsed tool payload costs a summary line, not its character count', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const E = w.ArchiveChatEstimate;
    assert.equal(E.blockHeight({ type: 'tool_result', text_length: 200000,
        text_state: 'included' }), E.COLLAPSED_BLOCK_PX);
    assert.ok(E.blockHeight({ type: 'text', text_length: 2000,
        text_state: 'included' }) > E.COLLAPSED_BLOCK_PX,
        'prose IS measured by its length; only the envelope is folded');
    assert.equal(E.estimateTurn({ kind: 'progress-run', from: 1, to: 9 }, null),
        E.PROGRESS_ROW_PX);
});

await test('an open panel is part of the estimate, not a correction paid after the jump', () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const E = w.ArchiveChatEstimate;
    const t = turn(1, { subagents_state: 'resolved', subagents: [
        { order: 1, order_basis: 'start_ts', link_state: 'resolved',
          transcripts: [{ transcript_id: 9 }] }] });
    const shut = E.estimateTurn(t, null);
    assert.ok(E.estimateTurn(t, { infoOpen: true }) > shut);
    assert.ok(E.estimateTurn(t, { subOpen: true }) > shut);
});

await test('setTurns resets the scroll, because a new conversation is a new question', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const view = w.ArchiveChatView.create({ document: env.document });
    view.mount(env.document.body);
    view.setTurns([turn(1), turn(2)]);
    view.setToken('ok', null);
    await frame();
    const scroller = view.root().querySelector('.archive-chat__scroller');
    scroller.scrollTop = 4000;
    view.setTurns([turn(3)]);
    assert.equal(scroller.scrollTop, 0);
    assert.equal(Object.keys(view.openState()).length, 0,
        'open panels belong to the OLD turns and must not carry over');
});

// ---------------------------------------------------------------------
// Paging. A conversation that just stops looks finished.
// ---------------------------------------------------------------------

/**
 * An ok envelope that says there is more.
 * @param {Array} rows @param {string|null} cur @returns {object}
 */
function moreEnvelope(rows, cur) {
    return {
        envelope: {
            result: rows, result_status: 'ok', scope_status: 'resolved',
            unevaluated: [],
            meta: { paging: { limit: 400, returned: rows.length,
                has_more: true, next_cursor: cur } }
        },
        httpStatus: 200, headers: null, transportError: null
    };
}

await test('has_more true renders a sentinel and a pager, and the pager APPENDS', async () => {
    // Measured live 2026-09-01: transcript 4 answers has_more true at a
    // 400-turn page, so a reader would hit turn 400 of a much longer
    // session with nothing on screen saying so.
    const env = createEnvironment();
    const w = loadModules(env.document);
    let call = 0;
    const { api } = fakeApi(() => {
        call++;
        return call === 1
            ? moreEnvelope([turn(1), turn(2)], 'CUR1')
            : okEnvelope([turn(3)]);
    });
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();

    let root = screen.view.root();
    assert.equal(screen.view.complete(), false);
    const sentinel = root.querySelector('.archive-chat__sentinel');
    assert.ok(sentinel, 'a conversation that is not finished must say so');
    assert.ok(sentinel.textContent.includes('THIS IS NOT THE END'));
    assert.ok(sentinel.textContent.includes('2 turn(s) loaded'));

    const pager = root.querySelector('[data-action="chat-load-more"]');
    assert.ok(pager, 'a further page exists, so a control to get it must too');
    pager.dispatchEvent('click');
    await frame();
    await frame();

    assert.equal(call, 2);
    assert.equal(screen.view.turns().length, 3, 'the page must APPEND, not replace');
    assert.equal(screen.view.turns()[2].line_no, 3);
    assert.equal(screen.view.complete(), true);
    root = screen.view.root();
    assert.equal(root.querySelectorAll('.archive-chat__sentinel').length, 0,
        'a finished conversation carries no sentinel');
});

await test('has_more NULL is NOT the end, and says it does not know', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi(() => ({
        envelope: { result: [turn(1)], result_status: 'ok',
            scope_status: 'resolved', unevaluated: [],
            meta: { paging: { has_more: null } } },
        httpStatus: 200, headers: null, transportError: null
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();
    assert.equal(screen.view.complete(), null,
        'null is not false - treating it as the end is a claim nobody made');
    const sentinel = screen.view.root().querySelector('.archive-chat__sentinel');
    assert.ok(sentinel);
    assert.ok(sentinel.textContent.includes('WHETHER THERE IS MORE: NOT KNOWN'));
    assert.equal(screen.view.root()
        .querySelectorAll('[data-action="chat-load-more"]').length, 0,
        'no cursor was offered, so a pager here would be a dead control');
});

await test('a FAILED further page keeps the loaded turns and does not claim the end', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    let call = 0;
    const { api } = fakeApi(() => {
        call++;
        return call === 1
            ? moreEnvelope([turn(1), turn(2)], 'CUR1')
            : { envelope: null, httpStatus: null, headers: null,
                transportError: 'no response in 15s' };
    });
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();
    screen.view.root().querySelector('[data-action="chat-load-more"]')
        .dispatchEvent('click');
    await frame();
    await frame();

    assert.equal(screen.view.turns().length, 2,
        'a failed page must not wipe the conversation already on screen');
    assert.equal(screen.view.complete(), null,
        'a failed page must not be read as the end of the conversation');
    assert.equal(screen.view.isLoadingMore(), false,
        'the pager must not stay wedged after a failure');
});

await test('the pager cannot be pressed twice into two overlapping appends', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    let call = 0;
    let release = null;
    const { api } = fakeApi(() => {
        call++;
        if (call === 1) return moreEnvelope([turn(1)], 'CUR1');
        return new Promise((r) => { release = () => r(okEnvelope([turn(2)])); });
    });
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    await screen.open(4, 'x');
    await frame();

    const pager = screen.view.root().querySelector('[data-action="chat-load-more"]');
    pager.dispatchEvent('click');
    await frame();
    assert.equal(screen.view.isLoadingMore(), true);
    // The second press lands while the first is still out.
    const again = screen.view.root().querySelector('[data-action="chat-load-more"]');
    assert.equal(again.getAttribute('disabled'), 'disabled',
        'the in-flight pager must be visibly disabled, not only guarded');
    again.dispatchEvent('click');
    await frame();
    assert.equal(call, 2, 'two overlapping pages would interleave two responses');

    release();
    await frame();
    await frame();
    assert.equal(screen.view.turns().length, 2);
    assert.equal(screen.view.isLoadingMore(), false);
});

await test('a partial answer renders its turns AND its banner', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi(() => ({
        envelope: { result: { turns: [turn(1), turn(2)] },
            result_status: 'partial', scope_status: 'resolved',
            unevaluated: [{ subject: 'transcript 4',
                reason: 'byte budget exhausted after 2 turns' }],
            meta: { paging: { has_more: null } } },
        httpStatus: 200, headers: null, transportError: null
    }));
    const screen = w.ArchiveChatScreen.create({ document: env.document, api });
    screen.mount(env.document.body);
    const tok = await screen.open(4, 'x');
    await frame();
    assert.equal(tok, 'partial');
    const root = screen.view.root();
    assert.equal(root.querySelectorAll('.archive-chat-turn').length, 2,
        'a partial answer has real turns in it and they must be shown');
    assert.equal(root.querySelectorAll('[data-outcome="partial"]').length, 1,
        'and the banner naming what was NOT reached must be shown too');
    assert.ok(root.textContent.includes('byte budget exhausted'));
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
