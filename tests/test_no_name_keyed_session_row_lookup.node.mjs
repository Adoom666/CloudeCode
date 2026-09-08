// Structural guard, JS half: no NEW client-side lookup that resolves a
// SESSION ROW by matching its tmux name against another array, in order
// to establish identity or write state onto it.
//
// This is the client-side sibling of
// tests/test_no_name_keyed_session_identity.py. Read that file's header
// first - it explains the class of bug in full: a tmux NAME is not
// durable identity, because tmux reuses a name the instant a session is
// recreated after its pane dies, so two SESSIONS can legitimately share
// one name. Server-side, the bug shape is a SQL query keyed on
// `tmux_name = ?` with no exact epoch to anchor it. Client-side, the
// exact same bug takes a different shape: `someArray.find(row =>
// row.name === tmuxName)` (or `.findIndex(...)`) to locate the row that
// represents a session, then reading or writing durable-feeling fields
// (status, unread, session_id, label) onto whatever it found. This
// file's own docstring in mergeLiveRow() names it directly: "the home
// screen joined live sessions to stored rows by name" is exactly this
// shape, in client/js/session-sidebar-fetch.js.
//
// WHY A REGEX SCAN RATHER THAN A REAL JS PARSER. This repo has no build
// step and no JS parser dependency (see CLAUDE.md: "no bundler, no
// transpile"), and test_csp_no_inline_handlers.node.mjs already
// establishes the precedent of a per-line regex scan over stripped
// source as the right tool at this cost/benefit point for a structural
// JS check here. The same tradeoff applies: a real parser would resolve
// data-flow precisely, but adding one is a bigger, riskier change than
// this guard needs, and the pattern this bug takes - `.find(` /
// `.findIndex(` whose predicate compares `.name`, `tmux_name`,
// `tmuxName`, or `tmux_session` with `===` - is syntactically narrow
// enough that a regex finds it reliably.
//
// WHAT IS DELIBERATELY ALLOWED. Not every `.find()` comparing `.name` is
// this bug - a "name" is also what PROJECTS and GROUPS are keyed on in
// this app by design, and DOM nodes carry a `dataset.name` for
// drag-reorder that has nothing to do with session identity. Excluding
// those by pattern would be guessing; instead every hit found by the
// scan below - including the legitimate ones - is required to be a
// REGISTERED entry in ALLOWED_MATCHES with a reason, exactly like the
// Python guard's EXEMPTIONS. A reason of "not a session identity: X" is
// as valid an entry as "known bug, unfixed here" - the point is that
// nothing passes through unreviewed, and a NEW site (or an existing site
// whose matched text changes) fails the build until a human looks at it
// and writes down which kind it is.
//
// KEYED BY MATCHED TEXT, NOT BY LINE NUMBER, ON PURPOSE. A line number
// drifts every time someone edits anything above the match in the same
// file - and several of the real files this scan touches
// (client/js/launchpad.js in particular) are edited often. Keying on the
// exact matched snippet means the allowlist only goes stale when the
// expression itself changes, which is precisely when a human should look
// at it again.
//
// Run with: node tests/test_no_name_keyed_session_row_lookup.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
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
        console.error(`  ${err && err.message ? err.message : err}`);
    }
}

/**
 * `.find(` / `.findIndex(` whose predicate compares `.name`,
 * `tmux_name`, `tmuxName`, or `tmux_session` with `===` against
 * something. This is the client-side shape of "pick the row for this
 * name" - the exact operation the Python sibling guard forbids on the
 * server's `sessions` table without an exact epoch.
 * @type {RegExp}
 */
const NAME_KEYED_FIND = /\.find(?:Index)?\(\s*\(?[\w$]*\)?\s*=>[^;]*?(?:\.name|tmux_name|tmuxName|tmux_session)\s*===\s*[\w$]+/;

/**
 * Every `client/js/*.js` file, recursively, excluding vendored code.
 *
 * Description: mirrors clientJsFiles() in
 *   test_csp_no_inline_handlers.node.mjs so both structural JS guards
 *   walk the exact same tree the same way.
 * Output: string[] - absolute paths.
 */
function clientJsFiles() {
    const out = [];
    const walk = (dir) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            if (entry.name === 'node_modules' || entry.name === 'vendor'
                || entry.name.startsWith('.')) continue;
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) walk(full);
            else if (entry.name.endsWith('.js')) out.push(full);
        }
    };
    walk(path.join(ROOT, 'client'));
    return out;
}

/**
 * Strip line and block comments so prose describing this exact bug class
 * (of which client/js now has plenty, including inside this file's own
 * header) is never read as a hit.
 * @param {string} text  Source.
 * @returns {string} Source with comments blanked.
 */
function stripJsComments(text) {
    return text
        // Block comments are replaced by their own newline count, not
        // deleted outright - deleting them would shift every later
        // line's reported number, which would make a failure message
        // point at the wrong line.
        .replace(/\/\*[\s\S]*?\*\//g, (m) => '\n'.repeat((m.match(/\n/g) || []).length))
        .replace(/^\s*\/\/.*$/gm, '');
}

/**
 * Every name-keyed find/findIndex hit across client/js, normalized so a
 * hit's KEY survives incidental whitespace changes but not a change to
 * the expression itself.
 *
 * Description: the client-side counterpart of the Python guard's
 *   `find_violations()`. Scans each file line by line (every real hit in
 *   this codebase is single-line, matching how the file is actually
 *   written) and returns one entry per match.
 * Inputs: root (string) - directory to walk; defaults to `client/`.
 * Output: Array<{file: string, line: number, snippet: string, key: string}>
 *   - `file` relative to the repo root, `snippet` the exact matched text,
 *   `key` the allowlist key (`${file}::${normalized snippet}`).
 * Example: findHits()  // -> [] once every site is registered or fixed
 */
function findHits(root) {
    const files = root
        ? (() => {
            const out = [];
            const walk = (dir) => {
                for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                    const full = path.join(dir, entry.name);
                    if (entry.isDirectory()) walk(full);
                    else if (entry.name.endsWith('.js')) out.push(full);
                }
            };
            walk(root);
            return out;
        })()
        : clientJsFiles();

    const hits = [];
    for (const file of files) {
        const code = stripJsComments(fs.readFileSync(file, 'utf8'));
        const relFile = path.relative(ROOT, file);
        code.split('\n').forEach((line, i) => {
            const m = line.match(NAME_KEYED_FIND);
            if (m) {
                const snippet = m[0].replace(/\s+/g, ' ').trim();
                hits.push({
                    file: relFile,
                    line: i + 1,
                    snippet,
                    key: `${relFile}::${snippet}`,
                });
            }
        });
    }
    return hits;
}

// ---------------------------------------------------------------------
// THE ALLOWLIST. Every entry below is a REAL, CURRENT hit (verified
// against the live files when this guard was written). A reason
// starting "KNOWN BUG" is unfixed debt in the same class this file
// exists to stop the next instance of - not fixed here, on purpose (see
// this task's constraints). A reason starting "NOT a session identity"
// is a legitimate use this scan's regex cannot structurally distinguish
// from the real bug, reviewed once and recorded so the next reviewer
// does not have to re-derive it from scratch.
// ---------------------------------------------------------------------
const ALLOWED_MATCHES = {
    'client/js/launchpad.js::.find(s => s.name === tmuxName': [
        'KNOWN BUG, unfixed by this guard. this.runningSessions.find by',
        'name - launchpad.js is excluded from edits by this task (another',
        'agent is in it); flagged here, not fixed here.',
    ].join(' '),
    'client/js/launchpad.js::.find(x => x.name === name': [
        'KNOWN BUG, unfixed by this guard. Same shape as the entry above,',
        'a second call site in the same file. launchpad.js is excluded',
        'from edits by this task; flagged here, not fixed here.',
    ].join(' '),
    'client/js/launchpad.js::.find((s) => s && s.name === tmuxName': [
        'KNOWN BUG, unfixed by this guard. Same shape, a third call site.',
        'launchpad.js is excluded from edits by this task; flagged here,',
        'not fixed here.',
    ].join(' '),
    'client/js/launchpad.js::.find(x => x && x.tmux_session === tmuxName': [
        'KNOWN BUG, unfixed by this guard. Same shape via the',
        'tmux_session field instead of .name. launchpad.js is excluded',
        'from edits by this task; flagged here, not fixed here.',
    ].join(' '),
    'client/js/launchpad.js::.find(s => s.name === name': [
        'KNOWN BUG, unfixed by this guard. Same shape, a fifth call site',
        '(note: distinct from the s.name === tmuxName entry above -',
        'compares against a differently-named local variable, which is',
        'why both survive as separate allowlist keys).',
        'launchpad.js is excluded from edits by this task; flagged here,',
        'not fixed here.',
    ].join(' '),
    'client/js/launchpad.js::.find(p => p.name === projectName': [
        'NOT a session identity lookup: this.projects is keyed by project',
        'name by design in this app (a launcher project has no other',
        'identity), not a sessions row. Different entity, same regex',
        'shape.',
    ].join(' '),
    'client/js/launchpad.js::.find((p) => p.name === chosen': [
        'NOT a session identity lookup: same projects-by-name lookup as',
        'the entry above, a second call site.',
    ].join(' '),
    'client/js/session-sidebar-fetch.js::.find((r) => r.name === tmuxName': [
        'KNOWN BUG, unfixed by this guard. This is mergeLiveRow(), and',
        'its own module docstring names this exact function as the',
        '"home screen joined live sessions to stored rows by name" bug',
        "from this task's background - it writes session_id, status,",
        'unread and created_by_cloude onto whatever row matches the',
        "name. This file is not in this task's do-not-touch list, but",
        "this task's constraints forbid fixing any flagged site - guard",
        'only.',
    ].join(' '),
    'client/js/session-sidebar-group-actions.js::.find((g) => g.name === String': [
        'NOT a session identity lookup: matches a GROUP by its user-given',
        'name right after creating it, to read back the server-assigned',
        "uuid. Groups are named entities in this app's data model, not",
        'sessions.',
    ].join(' '),
    'client/js/session-sidebar-reorder.js::.find((el) => el.dataset.name === name': [
        'NOT a session identity lookup: matches a DOM element by its',
        'dataset attribute to place a dragged row during manual reorder.',
        'Operates on rendered nodes, not on stored/live session state.',
    ].join(' '),
};

// NOT caught by NAME_KEYED_FIND, and worth recording rather than
// pretending the regex is exhaustive: launchpad.js also resolves a
// deep-link target by DERIVED DISPLAY NAME
// (`rows.find(s => this._deriveRunningSessionDisplayName(s.name) === slug)`,
// two call sites around line 4843) - the same "resolve a session by
// name" class, but the compared value sits behind a function call rather
// than a bare `.name === x`, which this regex does not follow. Left as a
// known gap in this guard's own coverage rather than silently declared
// complete; see the report this guard's author wrote for the human
// reader.

test('the client JS scanner finds files to scan', () => {
    assert.ok(clientJsFiles().length > 0,
        'no client .js files found - the scan is vacuous');
});

test('the guard can actually fail (positive control)', () => {
    // Plant the exact real bug shape in a throwaway directory - never
    // touches client/ - and prove the scanner flags it. A guard nobody
    // has seen fail is indistinguishable from a guard that cannot; this
    // repo's own history has several of those.
    const tmpDir = fs.mkdtempSync(path.join(ROOT, '.tmp_js_guard_proof_'));
    try {
        fs.writeFileSync(
            path.join(tmpDir, 'planted.js'),
            "function mergeLiveRow(rows, info) {\n"
            + "    const tmuxName = info && info.tmux_session;\n"
            + "    const existing = rows.find((r) => r.name === tmuxName);\n"
            + "    if (existing) existing.status = info.activity_status;\n"
            + "}\n",
        );
        const hits = findHits(tmpDir);
        assert.equal(hits.length, 1,
            `expected exactly one planted hit, scanner found ${hits.length}`);
        assert.match(hits[0].snippet, /\.find\(/);
    } finally {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    }
});

test('a plain equality unrelated to session rows is not flagged (negative control)', () => {
    const tmpDir = fs.mkdtempSync(path.join(ROOT, '.tmp_js_guard_proof_'));
    try {
        fs.writeFileSync(
            path.join(tmpDir, 'safe.js'),
            "function findByColor(items, color) {\n"
            + "    return items.find((x) => x.color === color);\n"
            + "}\n",
        );
        const hits = findHits(tmpDir);
        assert.deepEqual(hits, []);
    } finally {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    }
});

test('every name-keyed session-row find/findIndex in client/js is a registered, reasoned exemption', () => {
    const hits = findHits();
    const unregistered = hits.filter((h) => !(h.key in ALLOWED_MATCHES));
    assert.deepEqual(unregistered.map((h) => `${h.file}:${h.line}  ${h.snippet}`), [],
        'new (or newly regressed) session-row lookup(s) keyed on a tmux '
        + 'name via .find()/.findIndex() - a tmux name is reused whenever a '
        + 'session is recreated after its pane dies, so matching "the row '
        + 'whose name equals this one" is a guess about identity, not a '
        + 'fact. Resolve the row through something durable instead (the '
        + 'session id the server already sends, or session_uuid), or if '
        + 'this hit is genuinely NOT a session lookup (a project, a group, '
        + 'a DOM node), add it to ALLOWED_MATCHES in this file with a '
        + 'one-line reason:\n  '
        + unregistered.map((h) => `${h.file}:${h.line}  ${h.snippet}`).join('\n  '));

    const foundKeys = new Set(hits.map((h) => h.key));
    const stale = Object.keys(ALLOWED_MATCHES).filter((k) => !foundKeys.has(k));
    assert.deepEqual(stale, [],
        'ALLOWED_MATCHES entry registered for a match this scan no longer '
        + 'finds - the code was fixed, rewritten, or removed, and the '
        + 'entry is now dead weight that will mislead the next reader:\n  '
        + stale.join('\n  '));
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
