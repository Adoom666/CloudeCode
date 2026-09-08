// Node test: no user-facing string in client/index.html or client/js/**
// says delete/deleted/deletion any more.
//
// WHY. The owner's model, verbatim (2026-09-08): "sessions and projects
// can be archived not deleted. archived items are not visible unless the
// checkbox is checked." Nothing an app action performs is a hard delete
// from the user's point of view, so nothing the UI SAYS may claim one.
// Commit 960378b already renamed the session-record delete/archive
// wording; this test is the backstop that keeps every OTHER user-facing
// spot - and every future one - honest too.
//
// WHAT COUNTS AS "USER-FACING" HERE, AND WHY IT IS NOT A BARE SUBSTRING
// GREP. `deleteProject`, `data-wrapper-action="delete"`,
// `.ended-session-delete`, `cloude.launchpad.deletedSessionsVisible` -
// CLAUDE.md is explicit that identifiers like these (ids, class names,
// function names, endpoint verbs, localStorage keys) stay exactly as
// they are; only the copy a person reads changes. A regex over raw
// source text cannot tell "delete" the class name from "delete" the
// button label, so this file:
//   1. Tokenizes each .js file into string literals only (comments and
//      bare code identifiers - the class of thing `deleteProject` is -
//      are never captured, because they are never inside quotes).
//   2. Drops a literal outright when it is a pure identifier shape (a
//      bare kebab-case token, a dotted key, or the sole argument to a
//      console.* call - none of those reach a screen).
//   3. When what remains still looks like an HTML fragment (the
//      overwhelming majority of this codebase's UI strings, since there
//      is no template engine - see CLAUDE.md), pulls out only the parts
//      a person actually reads: text between tags, and the
//      title/aria-label/aria-description/placeholder/alt attribute
//      values. `class="ended-session-delete"` and
//      `data-command-action="delete"` are attributes on that same
//      fragment and are never extracted, by construction.
//   4. Otherwise (a plain confirm()/alert()/template-literal message)
//      checks the whole string, because there is no markup to separate
//      identifier from prose.
// Every fragment that survives all four steps is real UUI copy, and
// none of them may match delete/deleted/deletion.
//
// Run with: node tests/test_no_delete_wording.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than
 * throwing, so one bad file does not hide failures in the rest.
 * Inputs: name (string); fn (() => void).
 * Output: void.
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`FAIL - ${name}`);
        console.error(`  ${err.message}`);
    }
}

/** The word family this whole file exists to keep out of the UI. */
const DELETE_WORD = /\bdelet(?:e|ed|ion)\b/i;

/**
 * Recursively list every `.js` file under a directory.
 * Inputs: dir (string) - absolute path.
 * Output: string[] - absolute file paths, deterministic order.
 */
function listJsFiles(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            out.push(...listJsFiles(full));
        } else if (entry.isFile() && entry.name.endsWith('.js')) {
            out.push(full);
        }
    }
    return out;
}

/** Characters that, immediately before a `/`, mean the `/` opens a
 * regex literal rather than dividing two values. */
const CODE_CHAR_BEFORE_REGEX = /[([{,;:=!&|?+\-*%^~<>]$/;
/** Keywords that, immediately before a `/`, also mean regex - none of
 * these can be followed by a value to divide. */
const KEYWORD_BEFORE_REGEX = /^(?:return|typeof|instanceof|in|of|new|delete|void|yield|case|do|else|throw)$/;

/**
 * Decide whether the `/` at `src[i]` opens a regex literal. Looks
 * backward past whitespace to the previous significant character (or
 * keyword); a `/` after one of those cannot be division, because
 * nothing to divide precedes it. This is the exact confusion a naive
 * quote-tracking scanner hits on this file's `replace(/[<>&"']/g, ...)`
 * - the quote characters INSIDE that character class corrupt every
 * string boundary found afterward unless the regex is recognised and
 * skipped whole.
 * Inputs: src (string); i (number) - index of the `/`.
 * Output: boolean.
 */
function isRegexLiteralStart(src, i) {
    let k = i - 1;
    while (k >= 0 && /\s/.test(src[k])) k--;
    if (k < 0) return true;
    if (CODE_CHAR_BEFORE_REGEX.test(src[k])) return true;
    let wordStart = k;
    while (wordStart >= 0 && /[a-zA-Z_$]/.test(src[wordStart])) wordStart--;
    return KEYWORD_BEFORE_REGEX.test(src.slice(wordStart + 1, k + 1));
}

/**
 * Tokenize JS source into string-literal records, skipping line
 * comments, block comments, and regex literals. Template literals are
 * handled RECURSIVELY: a `${...}` interpolation is walked by the same
 * function, so a template literal nested inside another one's
 * interpolation - this codebase has exactly that shape, a conditional
 * `${hints ? `<ul>...</ul>` : ''}` inside a bigger row template - closes
 * on its OWN backtick rather than the outer literal's. A naive
 * find-the-next-backtick scan treats the inner literal's opening
 * backtick as the outer literal's close and corrupts every string
 * boundary found afterward; this does not.
 * Inputs: src (string) - raw file contents.
 * Output: {text: string, start: number}[] - one entry per string
 *   literal, `start` is the offset of the opening quote (used only to
 *   recover the preceding ~40 chars for the console.* exclusion).
 */
function tokenizeStringLiterals(src) {
    const out = [];
    const n = src.length;

    /**
     * Walk source from `start`, collecting string literals into `out`.
     * Inputs: start (number); insideInterpolation (boolean) - true when
     *   called to consume a `${...}` body, in which case an unmatched
     *   `}` (one not opened by a `{` seen within this same call) ends
     *   the walk instead of being treated as ordinary code.
     * Output: number - the index just past the point this walk stopped
     *   at (the char after the matching `}` when insideInterpolation).
     */
    function walk(start, insideInterpolation) {
        let i = start;
        let braceDepth = 0;
        while (i < n) {
            const c = src[i];
            const c2 = i + 1 < n ? src[i + 1] : '';
            if (c === '/' && c2 === '/') {
                i += 2;
                while (i < n && src[i] !== '\n') i++;
                continue;
            }
            if (c === '/' && c2 === '*') {
                i += 2;
                while (i < n && !(src[i] === '*' && src[i + 1] === '/')) i++;
                i += 2;
                continue;
            }
            if (c === '/' && isRegexLiteralStart(src, i)) {
                let j = i + 1;
                let inClass = false;
                while (j < n) {
                    if (src[j] === '\\') { j += 2; continue; }
                    if (src[j] === '[') { inClass = true; j++; continue; }
                    if (src[j] === ']') { inClass = false; j++; continue; }
                    if (src[j] === '/' && !inClass) break;
                    if (src[j] === '\n') break; // malformed guard, never valid JS
                    j++;
                }
                j++; // consume the closing '/'
                while (j < n && /[a-zA-Z]/.test(src[j])) j++; // flags
                i = j;
                continue;
            }
            if (c === '"' || c === "'") {
                const quote = c;
                const litStart = i;
                let j = i + 1;
                let buf = '';
                while (j < n && src[j] !== quote) {
                    if (src[j] === '\\') { buf += src[j + 1] || ''; j += 2; continue; }
                    buf += src[j];
                    j++;
                }
                out.push({ text: buf, start: litStart });
                i = j + 1;
                continue;
            }
            if (c === '`') {
                const litStart = i;
                let j = i + 1;
                let buf = '';
                while (j < n && src[j] !== '`') {
                    if (src[j] === '\\') { buf += src[j + 1] || ''; j += 2; continue; }
                    if (src[j] === '$' && src[j + 1] === '{') {
                        j = walk(j + 2, true);
                        continue;
                    }
                    buf += src[j];
                    j++;
                }
                out.push({ text: buf, start: litStart });
                i = j + 1;
                continue;
            }
            if (insideInterpolation && c === '}' && braceDepth === 0) {
                return i + 1;
            }
            if (c === '{') { braceDepth++; i++; continue; }
            if (c === '}') { braceDepth--; i++; continue; }
            i++;
        }
        return i;
    }

    walk(0, false);
    return out;
}

/** A bare identifier: a CSS selector/class token (optional leading `.`
 * or `#`, hyphens allowed to double up for BEM modifiers like
 * `recent-session-row--deleted`), or a dotted key like the localStorage
 * keys this app namespaces under `cloude.`. Neither is ever shown to a
 * user. */
const IDENTIFIER_SHAPE = /^[.#]?[a-zA-Z][a-zA-Z0-9]*(?:[.-]+[a-zA-Z0-9]+)*$/;

/** A bare `attr="value"` (or `attr='value'`) fragment built by string
 * concatenation on its own (e.g. `` ` data-deleted="1"` ``, with the
 * leading space this codebase's class/attr builders commonly carry) -
 * never HTML a user reads, because there is no surrounding tag or text.
 * Restricted to attribute NAMES that are never user-facing (class, id,
 * data-*), never a generic catch-all, so a real standalone user-facing
 * attribute value is not accidentally waved through. */
const BARE_INTERNAL_ATTR = /^(?:class|id|data-[a-zA-Z0-9-]+)=(["'])[^"']*\1$/;

/** Attributes whose VALUE a person actually reads on screen. Anything
 * else (class, id, data-*, href, src...) is an internal identifier. */
const USER_FACING_ATTRS = ['title', 'aria-label', 'aria-description', 'placeholder', 'alt'];

/**
 * Pull the user-legible fragments out of one raw string. See the file
 * header for the four-step rationale.
 * Inputs: text (string); precedingSrc (string) - up to 40 chars of the
 *   source immediately before this literal's opening quote, used only
 *   to recognise a console.* call.
 * Output: string[] - fragments worth checking; empty when the whole
 *   literal is identifier-shaped or console-only.
 */
function userFacingFragmentsOf(text, precedingSrc) {
    if (/console\s*\.\s*(?:log|warn|error|debug|info)\s*\($/.test(precedingSrc.trimEnd())) {
        return [];
    }
    const trimmed = text.trim();
    if (IDENTIFIER_SHAPE.test(trimmed) || BARE_INTERNAL_ATTR.test(trimmed)) {
        return [];
    }
    if (/<[a-zA-Z]/.test(text)) {
        const fragments = [];
        for (const m of text.matchAll(/>([^<>]+)</g)) {
            const inner = m[1].trim();
            if (inner) fragments.push(inner);
        }
        for (const attr of USER_FACING_ATTRS) {
            const re = new RegExp(`${attr}="([^"]*)"`, 'g');
            for (const m of text.matchAll(re)) fragments.push(m[1]);
        }
        return fragments;
    }
    return [text];
}

/**
 * Scan one .js file for delete-family words in user-facing string
 * fragments.
 * Inputs: filePath (string) - absolute path.
 * Output: {fragment: string, line: number}[] - every violation found.
 */
function scanJsFile(filePath) {
    const src = fs.readFileSync(filePath, 'utf8');
    const violations = [];
    for (const { text, start } of tokenizeStringLiterals(src)) {
        const preceding = src.slice(Math.max(0, start - 40), start);
        for (const fragment of userFacingFragmentsOf(text, preceding)) {
            if (DELETE_WORD.test(fragment)) {
                const line = src.slice(0, start).split('\n').length;
                violations.push({ fragment, line });
            }
        }
    }
    return violations;
}

/**
 * Scan client/index.html for delete-family words in text nodes and
 * user-facing attributes. `<!-- -->` comments are stripped first; there
 * is no inline `<script>`/`<style>` content in this file to also strip
 * (verified: both tags appear only as `<script src=...>` / external
 * stylesheet `<link>`s), so none is special-cased here.
 * Inputs: filePath (string) - absolute path.
 * Output: {fragment: string}[] - every violation found.
 */
function scanIndexHtml(filePath) {
    const raw = fs.readFileSync(filePath, 'utf8');
    const stripped = raw.replace(/<!--[\s\S]*?-->/g, '');
    const violations = [];
    for (const m of stripped.matchAll(/>([^<>]+)</g)) {
        const inner = m[1].trim();
        if (inner && DELETE_WORD.test(inner)) violations.push({ fragment: inner });
    }
    for (const attr of USER_FACING_ATTRS) {
        const re = new RegExp(`${attr}="([^"]*)"`, 'g');
        for (const m of stripped.matchAll(re)) {
            if (DELETE_WORD.test(m[1])) violations.push({ fragment: m[1] });
        }
    }
    return violations;
}

// ---------------------------------------------------------------------

test('client/index.html has no delete-family text node or user-facing attribute', () => {
    const violations = scanIndexHtml(path.join(ROOT, 'client', 'index.html'));
    assert.equal(
        violations.length, 0,
        `found: ${violations.map((v) => JSON.stringify(v.fragment)).join(', ')}`,
    );
});

const jsFiles = listJsFiles(path.join(ROOT, 'client', 'js'));

test('client/js contains more than a token handful of files (sanity: the walk is not silently empty)', () => {
    assert.ok(jsFiles.length > 100, `expected 100+ files, found ${jsFiles.length}`);
});

for (const filePath of jsFiles) {
    const rel = path.relative(ROOT, filePath);
    test(`${rel}: no delete-family word in a user-facing string`, () => {
        const violations = scanJsFile(filePath);
        assert.equal(
            violations.length, 0,
            `line(s) ${violations.map((v) => `${v.line}: ${JSON.stringify(v.fragment)}`).join('; ')}`,
        );
    });
}

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
